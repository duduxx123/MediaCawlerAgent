# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1

"""小红书网页私信机器人。

只使用 Playwright + CDP 操作已登录的 Chrome 页面，不调用未公开接口。开发版可连接
用户现有 Chrome，打包客户端自动启动 exe 专用 profile。机器人只负责打开目标、填入
草稿、校验并发送；“准备/确认”两阶段约束由 ``agent.tools.xhs_dm_tools`` 管理。
"""

from __future__ import annotations

import asyncio
import os
import re
import time
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urljoin, urlparse

from playwright.async_api import BrowserContext, Locator, Page, async_playwright

import config
from media_platform.xhs.help import parse_creator_info_from_url
from tools.cdp_browser import CDPBrowserManager


XHS_ORIGIN = "https://www.xiaohongshu.com"
CAPTCHA_SELECTORS = (
    ".captcha-container",
    "[class*='captcha']",
    "[class*='verify']",
    "iframe[src*='captcha']",
)
DM_INPUT_SELECTORS = (
    "[class*='chat'] [contenteditable='true']",
    "[class*='message'] [contenteditable='true']",
    "[class*='im-'] [contenteditable='true']",
    "[class*='chat'] textarea",
    "[class*='message'] textarea",
    "[class*='im-'] textarea",
)
PROFILE_NAME_SELECTORS = (
    "[class*='user-name']",
    "[class*='username']",
    "[class*='userName']",
    "[class*='nickname']",
    "h1",
)
_ZERO_WIDTH_RE = re.compile(r"[\u200b\u200c\u200d\ufeff]")
_USER_ID_RE = re.compile(r"^[0-9a-f]{24}$")
_PUBLIC_XHS_ID_RE = re.compile(r"^[0-9A-Za-z_-]{3,64}$")
_NOT_FOUND_RE = re.compile(r"用户不存在|账号已注销|页面不存在|内容不存在|访问的页面不见了")


def _normalize_text(value: str) -> str:
    return " ".join(_ZERO_WIDTH_RE.sub("", value or "").split())


def build_profile_url(user_id: str, profile_url: Optional[str] = None) -> str:
    """校验用户 ID/可选主页 URL，防止 CDP 被工具参数导航到任意站点。"""
    user_id = (user_id or "").strip()
    if not _USER_ID_RE.fullmatch(user_id):
        raise ValueError("小红书内部 user_id 格式无效；请传 24 位评论记录 creator_hash 原始值")
    if not profile_url:
        return f"{XHS_ORIGIN}/user/profile/{user_id}"

    profile_url = profile_url.strip()
    parsed = urlparse(profile_url)
    if parsed.scheme != "https" or parsed.hostname not in {"www.xiaohongshu.com", "xiaohongshu.com"}:
        raise ValueError("profile_url 必须是 https://www.xiaohongshu.com/user/profile/... 主页链接")
    try:
        info = parse_creator_info_from_url(profile_url)
    except ValueError as exc:
        raise ValueError("profile_url 不是有效的小红书用户主页链接") from exc
    if info.user_id != user_id:
        raise ValueError("profile_url 中的用户 ID 与 user_id 不一致")
    return profile_url


class XiaohongshuDmBot:
    """在一个 Agent 进程内保持 CDP 连接，退出时释放，不持久化任何 Cookie。"""

    def __init__(self) -> None:
        self._playwright = None
        self._cdp_manager: Optional[CDPBrowserManager] = None
        self._browser_context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self._bot_page: Optional[Page] = None
        self._aux_pages: list[Page] = []
        self.target_user_id = ""
        self.target_nickname = ""

    @staticmethod
    def _devtools_ws_url() -> Optional[str]:
        """读取 Chrome 136+ 的 DevToolsActivePort 精确 WebSocket 地址。"""
        local_app_data = os.environ.get("LOCALAPPDATA", "")
        port_file = Path(local_app_data) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
        try:
            if not port_file.is_file():
                return None
            lines = port_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) < 2 or not lines[0].strip().isdigit():
                return None
            return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        except OSError:
            return None

    async def setup(self) -> None:
        """按配置连接现有 Chrome 或启动客户端独立 profile。"""
        self._playwright = await async_playwright().start()
        try:
            if config.CDP_CONNECT_EXISTING:
                ws_url = self._devtools_ws_url()
                if ws_url:
                    try:
                        browser = await self._playwright.chromium.connect_over_cdp(
                            ws_url, timeout=config.BROWSER_LAUNCH_TIMEOUT * 1000
                        )
                    except Exception as exc:
                        raise RuntimeError(
                            "CDP 连接失败：请确认 Chrome 已开启远程调试，并在授权弹窗中点击『允许』"
                        ) from exc
                    self._browser_context = (
                        browser.contexts[0] if browser.contexts else await browser.new_context()
                    )
                if self._browser_context is None:
                    self._cdp_manager = CDPBrowserManager()
                    self._browser_context = await self._cdp_manager.launch_and_connect(
                        self._playwright,
                        playwright_proxy=None,
                        user_agent=None,
                        headless=False,
                    )
            else:
                saved_platform = config.PLATFORM
                try:
                    config.PLATFORM = "xhs"
                    self._cdp_manager = CDPBrowserManager()
                    self._browser_context = await self._cdp_manager.launch_and_connect(
                        self._playwright,
                        playwright_proxy=None,
                        user_agent=None,
                        headless=False,
                    )
                finally:
                    config.PLATFORM = saved_platform
            self.page = await self._browser_context.new_page()
            self._bot_page = self.page
            await self.page.goto(XHS_ORIGIN, wait_until="domcontentloaded", timeout=30000)
            await self.page.bring_to_front()
        except Exception:
            await self.close()
            raise

    async def check_alive(self) -> bool:
        try:
            if self.page is None or self.page.is_closed():
                return False
            return bool(await asyncio.wait_for(self.page.evaluate("() => true"), timeout=3))
        except Exception:
            return False

    async def close(self, close_page: bool = True) -> None:
        """释放本机器人页面和 CDP 连接，不关闭用户原有 Chrome 窗口。"""
        if close_page:
            seen: set[int] = set()
            for page in [self._bot_page, *self._aux_pages]:
                if page is None or id(page) in seen:
                    continue
                seen.add(id(page))
                try:
                    if not page.is_closed():
                        await asyncio.wait_for(page.close(), timeout=10)
                except Exception:
                    pass
        self._aux_pages.clear()
        self.page = None
        self._bot_page = None
        self._browser_context = None
        self.target_user_id = ""
        self.target_nickname = ""
        if self._cdp_manager is not None and not config.CDP_CONNECT_EXISTING:
            try:
                await asyncio.wait_for(self._cdp_manager.cleanup(), timeout=20)
            except Exception:
                pass
        if self._playwright is not None:
            try:
                await asyncio.wait_for(self._playwright.stop(), timeout=15)
            except Exception:
                pass
        self._playwright = None
        self._cdp_manager = None

    async def prepare_dm(
        self,
        user_id: str,
        content: str,
        profile_url: Optional[str] = None,
    ) -> str:
        """打开用户主页和聊天面板，填入但不发送私信，返回识别到的昵称。"""
        if self.page is None:
            raise RuntimeError("小红书私信机器人尚未连接 Chrome")
        target_url = build_profile_url(user_id, profile_url)
        await self.page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
        await self.page.bring_to_front()
        await self.page.wait_for_timeout(1500)
        await self._assert_ready_for_profile(user_id)
        nickname = await self._extract_profile_name()

        pages_before = set(self._browser_context.pages) if self._browser_context else set()
        await self._click_dm_button()
        await self.page.wait_for_timeout(1500)
        await self._switch_to_new_page(pages_before)
        box = await self._wait_dm_input()
        await self._fill_and_verify(box, content)

        self.target_user_id = user_id
        self.target_nickname = nickname
        return nickname

    async def resolve_public_xhs_id(self, xiaohongshu_id: str) -> tuple[str, str, str]:
        """通过网页“用户”搜索把公开小红书号解析成内部 user_id 和临时主页链接。

        返回 ``(user_id, nickname, profile_url)``。profile_url 中可能含页面临时生成的
        xsec_token，只在当前内存调用链中使用，不由工具结果返回或持久化。
        """
        if self.page is None:
            raise RuntimeError("小红书私信机器人尚未连接 Chrome")
        xiaohongshu_id = (xiaohongshu_id or "").strip()
        if not _PUBLIC_XHS_ID_RE.fullmatch(xiaohongshu_id):
            raise ValueError("小红书号格式无效")
        if not await self._has_login_cookie():
            raise RuntimeError("未检测到小红书登录态：请先在当前 Chrome 登录小红书")

        search_url = (
            f"{XHS_ORIGIN}/search_result?keyword={quote(xiaohongshu_id)}"
            "&source=web_search_result_notes&type=51"
        )
        await self.page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
        await self.page.bring_to_front()
        deadline = time.monotonic() + 15
        expected_re = re.compile(rf"小红书号\s*[：:]\s*{re.escape(xiaohongshu_id)}(?:\s|$)")
        while time.monotonic() < deadline:
            if await self._detect_captcha():
                raise RuntimeError("搜索用户时出现验证码/安全验证，请在 Chrome 手动处理后重试")
            links = self.page.locator('a[href*="/user/profile/"]')
            try:
                count = min(await links.count(), 100)
            except Exception:
                count = 0
            for index in range(count):
                link = links.nth(index)
                try:
                    text = ((await link.inner_text()) or "").strip()
                    if not expected_re.search(text):
                        continue
                    href = urljoin(XHS_ORIGIN, (await link.get_attribute("href")) or "")
                    info = parse_creator_info_from_url(href)
                    nickname = text.splitlines()[0].strip() if text else "(未知用户)"
                    return info.user_id, nickname, href
                except Exception:
                    continue
            await self.page.wait_for_timeout(500)
        raise RuntimeError(f"用户搜索结果中未找到小红书号 {xiaohongshu_id}")

    async def read_public_red_id(
        self,
        user_id: str,
        profile_url: Optional[str] = None,
    ) -> tuple[str, str]:
        """只读用户主页，返回 ``(red_id, nickname)``，用于历史 SQLite 身份补全。"""
        if self.page is None:
            raise RuntimeError("小红书私信机器人尚未连接 Chrome")
        await self.page.goto(
            build_profile_url(user_id, profile_url),
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await self.page.bring_to_front()
        await self.page.wait_for_timeout(1200)
        await self._assert_ready_for_profile(user_id)
        body = await self._body_text()
        match = re.search(r"小红书号\s*[：:]\s*([^\s]+)", body)
        if not match:
            raise RuntimeError(f"用户主页未显示公开小红书号（user_id={user_id}）")
        return match.group(1).strip(), await self._extract_profile_name()

    async def verify_prepared(self, user_id: str, content: str) -> None:
        """发送前重新校验页面、目标和输入框内容，防止误发或重复发送。"""
        if self.page is None or self.page.is_closed():
            raise RuntimeError("私信页面已关闭，请重新准备草稿")
        if self.target_user_id != user_id:
            raise RuntimeError("当前私信页面目标用户已变化，请重新准备草稿")
        if await self._detect_captcha():
            raise RuntimeError("检测到验证码/安全验证，请在 Chrome 手动处理后重新准备草稿")
        box = await self._find_dm_input()
        if box is None:
            raise RuntimeError("找不到已准备的私信输入框，可能页面已跳转；请重新准备草稿")
        actual = await self._read_input(box)
        if _normalize_text(actual) != _normalize_text(content):
            raise RuntimeError("私信输入框内容已变化或已被手动发送；为避免重复发送，请重新准备草稿")

    async def submit_dm(self, user_id: str, content: str) -> bool:
        """校验后发送一次，不自动重试；返回页面自检是否确认成功。"""
        await self.verify_prepared(user_id, content)
        box = await self._find_dm_input()
        if box is None:  # verify_prepared 已覆盖，保留类型收窄
            raise RuntimeError("找不到私信输入框")
        before_count = await self._count_message_text(content)

        sent = False
        for button in await self._visible_exact_text("button", "发送"):
            try:
                if await button.is_enabled():
                    await button.click()
                    sent = True
                    break
            except Exception:
                continue
        if not sent:
            # 仅在已经精确定位并校验过的聊天输入框上按 Enter，不使用全局键盘兜底。
            try:
                await box.press("Enter")
                sent = True
            except Exception as exc:
                raise RuntimeError("找不到可用的『发送』按钮，且聊天输入框 Enter 发送失败") from exc

        await self.page.wait_for_timeout(2200)
        if await self._detect_captcha():
            raise RuntimeError("发送后出现验证码/安全验证；不要自动重试，请到 Chrome 确认消息状态")
        after_count = await self._count_message_text(content)
        try:
            input_cleared = not _normalize_text(await self._read_input(box))
        except Exception:
            input_cleared = True  # 发送后组件重建也属于常见成功形态
        return input_cleared and after_count > before_count

    async def _assert_ready_for_profile(self, user_id: str) -> None:
        if await self._detect_captcha():
            raise RuntimeError("检测到验证码/安全验证，请先在 Chrome 手动完成后重试")
        if not await self._has_login_cookie():
            raise RuntimeError("未检测到小红书登录态：请先在当前 Chrome 登录小红书")
        body = await self._body_text()
        if _NOT_FOUND_RE.search(body):
            raise RuntimeError(f"用户不存在、已注销或主页不可访问（user_id={user_id}）")

    async def _has_login_cookie(self) -> bool:
        if self._browser_context is None:
            return False
        try:
            cookies = await self._browser_context.cookies([XHS_ORIGIN])
        except Exception:
            return False
        return any(c.get("name") == "web_session" and c.get("value") for c in cookies)

    async def _detect_captcha(self) -> bool:
        if self.page is None:
            return False
        for selector in CAPTCHA_SELECTORS:
            try:
                loc = self.page.locator(selector).first
                if await loc.count() and await loc.is_visible():
                    return True
            except Exception:
                pass
        try:
            return bool(re.search(r"安全验证|拖动滑块|完成验证", await self._body_text()))
        except Exception:
            return False

    async def _body_text(self) -> str:
        if self.page is None:
            return ""
        try:
            return (await self.page.locator("body").inner_text(timeout=3000)) or ""
        except Exception:
            return ""

    async def _extract_profile_name(self) -> str:
        if self.page is None:
            return "(未知用户)"
        for selector in PROFILE_NAME_SELECTORS:
            try:
                candidates = self.page.locator(selector)
                for i in range(min(await candidates.count(), 8)):
                    candidate = candidates.nth(i)
                    if await candidate.is_visible():
                        value = ((await candidate.inner_text()) or "").strip()
                        if value and value not in {"关注", "私信", "发消息"}:
                            return value[:80]
            except Exception:
                pass
        try:
            title = ((await self.page.title()) or "").strip()
            return re.split(r"\s*[-—|]\s*小红书", title, maxsplit=1)[0] or "(未知用户)"
        except Exception:
            return "(未知用户)"

    async def _visible_exact_text(self, tag: str, text: str) -> list[Locator]:
        if self.page is None:
            return []
        result: list[Locator] = []
        try:
            candidates = self.page.locator(tag).filter(has_text=re.compile(rf"^{re.escape(text)}$"))
            for i in range(min(await candidates.count(), 30)):
                item = candidates.nth(i)
                if await item.is_visible():
                    result.append(item)
        except Exception:
            pass
        return result

    async def _click_dm_button(self) -> None:
        if self.page is None:
            raise RuntimeError("用户主页已关闭")
        # 2026-09-02 真机：小红书主页使用无文字图标按钮：
        # <button class="xhs-user-im-btn" title="发消息"><svg ...></svg></button>
        for selector in (
            'button.xhs-user-im-btn[title="发消息"]',
            'button[title="发消息"]',
            'button[class*="user-im-btn"]',
        ):
            try:
                button = self.page.locator(selector).first
                if await button.count() and await button.is_visible() and await button.is_enabled():
                    await button.click()
                    return
            except Exception:
                pass
        for label in ("私信", "发消息"):
            for tag in ("button", "a", "div", "span"):
                for item in await self._visible_exact_text(tag, label):
                    try:
                        await item.click(force=tag in {"div", "span"})
                        return
                    except Exception:
                        continue
        follows = await self._visible_exact_text("button", "关注")
        if follows:
            raise RuntimeError(
                "该用户主页当前仅显示『关注』，未提供『私信/发消息』入口；"
                "可能需要先建立关注关系或对方已限制陌生人私信。工具不会擅自关注。"
            )
        raise RuntimeError("无法找到『私信/发消息』按钮：对方可能关闭私信，或小红书页面结构已更新")

    async def _switch_to_new_page(self, pages_before: set[Page]) -> None:
        if self._browser_context is None or self.page is None:
            return
        new_pages = [p for p in self._browser_context.pages if p not in pages_before]
        if new_pages:
            if self.page is not self._bot_page:
                self._aux_pages.append(self.page)
            self.page = new_pages[-1]
            self._aux_pages.append(self.page)
            await self.page.bring_to_front()
            await self.page.wait_for_timeout(1200)

    async def _find_dm_input(self) -> Optional[Locator]:
        if self.page is None:
            return None
        for selector in DM_INPUT_SELECTORS:
            try:
                candidates = self.page.locator(selector)
                for i in reversed(range(await candidates.count())):
                    item = candidates.nth(i)
                    if await item.is_visible() and await item.is_editable():
                        return item
            except Exception:
                pass
        # 最后只接受可见、可编辑的 contenteditable/textarea；不把普通搜索 input 当私信框。
        for selector in ("[contenteditable='true']", "textarea"):
            try:
                candidates = self.page.locator(selector)
                for i in reversed(range(await candidates.count())):
                    item = candidates.nth(i)
                    if await item.is_visible() and await item.is_editable():
                        return item
            except Exception:
                pass
        return None

    async def _wait_dm_input(self, timeout_ms: int = 15000) -> Locator:
        deadline = time.monotonic() + timeout_ms / 1000
        while time.monotonic() < deadline:
            if await self._detect_captcha():
                raise RuntimeError("打开私信后出现验证码/安全验证，请在 Chrome 手动处理后重试")
            box = await self._find_dm_input()
            if box is not None:
                return box
            await self.page.wait_for_timeout(400)
        raise RuntimeError("聊天输入框 15 秒内未出现：对方可能关闭私信、需先关注，或页面结构已更新")

    async def _fill_and_verify(self, box: Locator, content: str) -> None:
        await box.click()
        try:
            await box.fill(content)
        except Exception:
            await box.press("Control+A")
            await box.press("Backspace")
            await box.type(content, delay=25)
        actual = await self._read_input(box)
        if _normalize_text(actual) != _normalize_text(content):
            raise RuntimeError("私信草稿填入后校验失败，未执行发送")

    @staticmethod
    async def _read_input(box: Locator) -> str:
        try:
            tag_name = await box.evaluate("el => el.tagName.toLowerCase()")
            if tag_name in {"textarea", "input"}:
                return await box.input_value()
            return (await box.inner_text()) or ""
        except Exception:
            return ""

    async def _count_message_text(self, content: str) -> int:
        """统计输入框以外、可见文本中目标消息的出现数，用于发送前后增量自检。"""
        if self.page is None:
            return 0
        script = r"""
        (needle) => {
          const norm = s => (s || '').replace(/[\u200b\u200c\u200d\ufeff]/g, '').replace(/\s+/g, ' ').trim();
          const target = norm(needle);
          if (!target) return 0;
          let count = 0;
          const walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
          let node;
          while ((node = walker.nextNode())) {
            const el = node.parentElement;
            if (!el || el.closest('[contenteditable="true"], textarea')) continue;
            const rect = el.getBoundingClientRect();
            if (!rect.width || !rect.height) continue;
            if (norm(node.textContent) === target) count++;
          }
          return count;
        }
        """
        try:
            return int(await self.page.evaluate(script, content))
        except Exception:
            return 0
