# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1

"""快手视频评论机器人：CDP 只读取登录 Cookie，评论通过网页 GraphQL API 提交。

Cookie 只保存在当前 Python 进程内存。当前仅支持在作品下发布一级评论；网页版
没有稳定的指定评论回复能力，因此不提供回复工具。
"""

from __future__ import annotations

import asyncio
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urljoin, urlparse

from playwright.async_api import BrowserContext, async_playwright

import config
from media_platform.kuaishou.client import KuaiShouClient
from tools import utils
from tools.cdp_browser import CDPBrowserManager
from tools.httpx_util import make_async_client


KS_ORIGIN = "https://www.kuaishou.com"
_PHOTO_ID_RE = re.compile(r"^[0-9A-Za-z_-]{6,64}$")
_AUTH_ERROR_RE = re.compile(r"未登录|登录|login|passToken|token.*(?:失效|过期|invalid)", re.I)
_RISK_ERROR_RE = re.compile(r"验证码|安全验证|风控|频繁|risk|captcha|verify", re.I)
VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL_SECONDS = 2.0
VERIFY_MAX_PAGES = 5
_LOGIN_TOKEN_COOKIE_NAMES = (
    "passToken",
    "kuaishou.server.web_st",
    "kuaishou.server.webday7_st",
)


class KuaishouNotLoggedInError(RuntimeError):
    """Chrome 中没有可用的快手登录态。"""


class KuaishouCommentError(RuntimeError):
    """快手评论业务失败。"""

    def __init__(
        self,
        message: str,
        *,
        need_captcha: bool = False,
        sent_unknown: bool = False,
        raw: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.need_captcha = need_captcha
        self.sent_unknown = sent_unknown
        self.raw = raw


@dataclass(frozen=True)
class KuaishouPhotoTarget:
    photo_id: str
    photo_author_id: str
    photo_title: str
    exp_tag: str = ""


def parse_kuaishou_photo_id(target: str) -> str:
    """从真实快手作品 URL 或裸 photoId 中提取 ID，并拒绝 SEO 中文路由。"""
    text = (target or "").strip()
    if not text:
        raise ValueError("快手作品链接或 photo_id 不能为空")
    if _PHOTO_ID_RE.fullmatch(text):
        return text

    parsed = urlparse(text)
    if parsed.scheme != "https" or parsed.hostname not in {"kuaishou.com", "www.kuaishou.com"}:
        raise ValueError("只支持 https://www.kuaishou.com/short-video/{photo_id} 作品链接")
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) != 2 or parts[0] != "short-video" or not _PHOTO_ID_RE.fullmatch(parts[1]):
        raise ValueError("快手链接缺少有效 photo_id；热榜中文路由不能用于发布评论")
    return parts[1]


async def resolve_kuaishou_photo_id(target: str) -> str:
    """解析作品 ID；仅允许快手站内的 ``/f/...`` 安全重定向短链。"""
    try:
        return parse_kuaishou_photo_id(target)
    except ValueError as direct_error:
        text = (target or "").strip()
        parsed = urlparse(text)
        parts = [part for part in parsed.path.split("/") if part]
        if (
            parsed.scheme != "https"
            or parsed.hostname not in {"kuaishou.com", "www.kuaishou.com"}
            or len(parts) != 2
            or parts[0] != "f"
            or not _PHOTO_ID_RE.fullmatch(parts[1])
        ):
            raise direct_error

        current = text
        async with make_async_client(proxy=None) as client:
            for _ in range(5):
                response = await client.get(
                    current,
                    follow_redirects=False,
                    timeout=20,
                    headers={"User-Agent": utils.get_user_agent()},
                )
                if not response.is_redirect:
                    break
                location = response.headers.get("location") or ""
                next_url = urljoin(current, location)
                next_parsed = urlparse(next_url)
                if (
                    next_parsed.scheme != "https"
                    or next_parsed.hostname not in {"kuaishou.com", "www.kuaishou.com"}
                ):
                    raise ValueError("快手分享短链跳转到了非快手域名，已拒绝访问")
                current = next_url
        try:
            return parse_kuaishou_photo_id(current)
        except ValueError as exc:
            raise ValueError("快手分享短链未解析到有效作品 photo_id") from exc


def _comment_id(item: Dict[str, Any]) -> str:
    value = item.get("comment_id") or item.get("commentId")
    return str(value or "").strip()


def _comment_user_id(item: Dict[str, Any]) -> str:
    return str(item.get("author_id") or item.get("authorId") or "").strip()


class KuaishouCommentBot:
    """在 Agent 进程内缓存 Cookie/API 客户端，不长期持有 CDP 连接。"""

    def __init__(self) -> None:
        self._playwright = None
        self._cdp_manager: Optional[CDPBrowserManager] = None
        self._browser_context: Optional[BrowserContext] = None
        self.client: Optional[KuaiShouClient] = None
        self._pass_token = ""
        self._current_user_id = ""

    @staticmethod
    def _devtools_ws_url() -> Optional[str]:
        port_file = (
            Path(os.environ.get("LOCALAPPDATA", ""))
            / "Google"
            / "Chrome"
            / "User Data"
            / "DevToolsActivePort"
        )
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
        await self._refresh_memory_login()

    async def _refresh_memory_login(self) -> None:
        await self._connect_browser()
        try:
            cookie_str, cookie_dict = await self._read_cookies_from_browser()
            self._build_client_from_cookies(cookie_str, cookie_dict)
        finally:
            await self._disconnect_browser()

    async def _connect_browser(self) -> None:
        if self._browser_context is not None:
            return
        self._playwright = await async_playwright().start()
        if config.CDP_CONNECT_EXISTING:
            ws_url = self._devtools_ws_url()
            if ws_url:
                try:
                    browser = await self._playwright.chromium.connect_over_cdp(
                        ws_url, timeout=config.BROWSER_LAUNCH_TIMEOUT * 1000
                    )
                    self._browser_context = (
                        browser.contexts[0] if browser.contexts else await browser.new_context()
                    )
                except Exception as exc:
                    raise RuntimeError(
                        "CDP 连接失败：请确认 Chrome 已开启远程调试，并在授权弹窗点击『允许』"
                    ) from exc
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
                config.PLATFORM = "ks"
                self._cdp_manager = CDPBrowserManager()
                self._browser_context = await self._cdp_manager.launch_and_connect(
                    self._playwright,
                    playwright_proxy=None,
                    user_agent=None,
                    headless=False,
                )
            finally:
                config.PLATFORM = saved_platform

    async def _read_cookies_from_browser(self) -> Tuple[str, Dict[str, str]]:
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self._browser_context, urls=[KS_ORIGIN]
        )
        login_token = next(
            (cookie_dict.get(name) for name in _LOGIN_TOKEN_COOKIE_NAMES if cookie_dict.get(name)),
            "",
        )
        if not (cookie_dict.get("userId") and login_token):
            raise KuaishouNotLoggedInError(
                "未检测到快手登录态（Cookie 缺少 userId/登录令牌），请先在当前 Chrome 登录快手"
            )
        return cookie_str, cookie_dict

    def _build_client_from_cookies(self, cookie_str: str, cookie_dict: Dict[str, str]) -> None:
        self.client = KuaiShouClient(
            proxy=None,
            headers={
                "User-Agent": utils.get_user_agent(),
                "Cookie": cookie_str,
                "Origin": KS_ORIGIN,
                "Referer": f"{KS_ORIGIN}/",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=None,
            cookie_dict=cookie_dict,
        )
        self._pass_token = str(next(
            (cookie_dict.get(name) for name in _LOGIN_TOKEN_COOKIE_NAMES if cookie_dict.get(name)),
            "",
        ))
        self._current_user_id = str(cookie_dict.get("userId") or "")

    async def _disconnect_browser(self) -> None:
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
        self._browser_context = None
        self._cdp_manager = None
        self._playwright = None

    async def check_alive(self) -> bool:
        return self.client is not None and bool(self._pass_token)

    async def close(self) -> None:
        await self._disconnect_browser()
        self.client = None
        self._pass_token = ""
        self._current_user_id = ""

    async def resolve_photo_target(self, target: str) -> KuaishouPhotoTarget:
        """解析分享链接并读取可评论的作品详情。"""
        if self.client is None:
            await self._refresh_memory_login()
        photo_id = await resolve_kuaishou_photo_id(target)
        detail_response = await self.client.get_video_info(photo_id)
        detail = detail_response.get("visionVideoDetail") or {}
        photo = detail.get("photo") or {}
        author = detail.get("author") or {}
        if not photo.get("id") or not author.get("id"):
            raise KuaishouCommentError("无法读取快手作品详情，作品可能已失效或登录态失效")
        if (detail.get("commentLimit") or {}).get("canAddComment") is False:
            raise KuaishouCommentError("该快手作品已关闭评论功能")
        return KuaishouPhotoTarget(
            photo_id=photo_id,
            photo_author_id=str(author.get("id")),
            photo_title=str(photo.get("caption") or "").strip()[:200],
            exp_tag=str(photo.get("expTag") or ""),
        )

    @staticmethod
    def _submitted_comment_id(response: Dict[str, Any], action: str) -> str:
        if not response:
            raise KuaishouCommentError(f"快手{action}接口返回空结果", sent_unknown=True)
        result_code = response.get("result")
        if result_code != 1:
            message = str(
                response.get("message")
                or response.get("errorMsg")
                or response.get("error_msg")
                or f"result={result_code}"
            )
            if _AUTH_ERROR_RE.search(message):
                raise KuaishouNotLoggedInError(f"快手登录态失效：{message}")
            raise KuaishouCommentError(
                f"{action}失败：{message}",
                need_captcha=bool(_RISK_ERROR_RE.search(message)),
                raw=response,
            )
        comment_id = str(response.get("commentId") or "")
        if not comment_id:
            raise KuaishouCommentError(
                f"快手{action}接口未返回 commentId，发布状态不确定",
                sent_unknown=True,
                raw=response,
            )
        return comment_id

    async def submit_comment(
        self, target: KuaishouPhotoTarget, content: str
    ) -> Dict[str, Any]:
        """在作品下发布一条顶层评论。"""
        if self.client is None:
            raise KuaishouNotLoggedInError("快手内存登录态不存在，请重新调用评论工具")
        try:
            response = await self.client.add_comment(
                photo_id=target.photo_id,
                photo_author_id=target.photo_author_id,
                content=content,
                exp_tag=target.exp_tag,
            )
        except Exception as exc:
            raise KuaishouCommentError(
                f"发布评论请求异常：{exc}", sent_unknown=True
            ) from exc
        new_comment_id = self._submitted_comment_id(response, "发布评论")
        verified = await self._verify_comment(target.photo_id, new_comment_id, content)
        return {
            "ok": True,
            "request_accepted": True,
            "sent": True,
            "sent_unknown": False,
            "self_checked": verified,
            "list_sync_pending": not verified,
            "comment_id": new_comment_id,
            "photo_id": target.photo_id,
            "photo_title": target.photo_title,
            "message": (
                "快手顶层评论已发布，评论列表自检通过。"
                if verified
                else "快手顶层评论已发布并返回 commentId；评论读取接口尚未同步，可刷新网页确认，不要自动重试。"
            ),
        }

    async def _verify_comment(
        self, photo_id: str, new_comment_id: str, content: str
    ) -> bool:
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                pcursor = ""
                for _ in range(VERIFY_MAX_PAGES):
                    page = await self.client.get_video_comments(photo_id, pcursor)
                    for item in page.get("rootCommentsV2") or []:
                        if _comment_user_id(item) != self._current_user_id:
                            continue
                        if new_comment_id and _comment_id(item) == new_comment_id:
                            return True
                        if str(item.get("content") or "").strip() == content:
                            return True
                    next_cursor = str(page.get("pcursorV2") or "no_more")
                    if next_cursor == "no_more" or next_cursor == pcursor:
                        break
                    pcursor = next_cursor
            except Exception as exc:
                utils.logger.warning(
                    f"[KuaishouCommentBot] 顶层评论自检第 {attempt + 1} 次失败（忽略）: {exc}"
                )
            if attempt < VERIFY_ATTEMPTS - 1:
                await asyncio.sleep(VERIFY_INTERVAL_SECONDS)
        return False
