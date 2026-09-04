# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/douyin/comment_bot.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""
抖音评论机器人（测试版）— 评论获取走本项目 API 爬虫（快），写操作走 Playwright + CDP。

职责拆分：
  - 获取评论（含 userId）：复用本项目 DouYinClient 的评论 API（/aweme/v1/web/comment/list/），
    直接取原始响应里的 user.sec_uid / user.nickname（注意：项目落库时是脱敏哈希的，
    所以这里走内存原始数据，不写 data 文件）。
  - 发评论 / 回复评论：Playwright 通过 CDP 操作已登录的 Chrome；开发版可连接现有
    Chrome，打包客户端自动启动 exe 专用 profile。选择器与流程移植自
    D:\\CDP-MCP-demo 的 DouyinService.java（Playwright for Java）。

用法：
    # 只读：抓取评论并打印（含 sec_uid，供后续 AI 获客识别意向客户）
    uv run python -m media_platform.douyin.comment_bot <视频链接> --max-comments 30

    # 发一条新评论（发布前需输入 y 确认，--yes 可跳过）
    uv run python -m media_platform.douyin.comment_bot <视频链接> --text "评论内容" --yes

    # 回复某条评论：--reply-to 支持 #序号 / sec_uid / 昵称
    uv run python -m media_platform.douyin.comment_bot <视频链接> --text "回复内容" --reply-to "#3"
    uv run python -m media_platform.douyin.comment_bot <视频链接> --text "回复内容" --reply-to MS4wLjABAAA... --yes

    # 私信填框（只填入输入框，不发送；填完保持页面打开，供你在 Chrome 确认后手动发送）
    uv run python -m media_platform.douyin.comment_bot <视频链接> --text "私信内容" --dm-to "#3"
    uv run python -m media_platform.douyin.comment_bot --text "私信内容" --dm-sec-uid MS4wLjABAAAA...

    # 私信发送（先填框再发送；--dm-send 发送前会要求人工确认）
    uv run python -m media_platform.douyin.comment_bot <视频链接> --text "私信内容" --dm-to "#3" --dm-send

前提：
    CDP_CONNECT_EXISTING=True 时，Chrome 需开启远程调试且已登录抖音；
    CDP_CONNECT_EXISTING=False 时，程序自动启动独立 profile，可在该窗口登录抖音。
"""

import argparse
import asyncio
import io
import random
import re
import sys
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

# Windows GBK 控制台下输出中文必需：强制 stdout/stderr 为 UTF-8
if sys.stdout and hasattr(sys.stdout, "buffer"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, BrowserContext, Locator, Page

import config
from media_platform.douyin.core import DouYinCrawler
from media_platform.douyin.help import parse_video_info_from_url
from tools.cdp_browser import CDPBrowserManager

# ─── 选择器常量（移植自 D:\CDP-MCP-demo DouyinService.java） ───

COMMENT_ITEM_SELS = '[data-e2e="comment-item"], div[class*="comment-item"], div[class*="commentItem"]'
COMMENT_AREA_SELS = [
    '[data-e2e="comment-list"]',
    '[data-e2e="comment-container"]',
    'div[class*="comment-list"]',
    'div[class*="commentList"]',
    'div[class*="comment-container"]',
    "#douyin-right-container",
]
COMMENT_CONTAINER_SELS = [
    '[data-e2e="comment-list"]',
    'div[class*="comment-mainContent"]',
    'div[class*="comment-container"]',
    "#douyin-right-container",
]
CONTENT_EDITABLE_SELS = [
    'div[contenteditable="true"]',
    "textarea",
    '[data-e2e="comment-input"] div[contenteditable]',
]
CAPTCHA_SELS = [".secsdk-captcha-drag-wrapper", ".captcha_verify_container"]

# ─── 私信（DM）选择器常量（移植自 D:\CDP-MCP-demo DouyinService.java 私信段 1022-1427） ───

DM_PROFILE_SELS = [
    '[data-e2e="user-profile"]',
    'div[class*="profile"]',
    'div[class*="user-info"]',
]
DM_NAME_SELS = [
    '[data-e2e="user-name"]',
    '[data-e2e="user-title"]',
    'span[class*="nickname"]',
    'h1[class*="name"]',
    'div[class*="nickname"]',
    'div[class*="user-name"]',
]
DM_BUTTON_PRIMARY_SELS = [
    '[data-e2e="user-private-message"]',
    '[data-e2e="private-message"]',
    '[data-e2e="user-follow-btn"] + button',
]
DM_BUTTON_TEXT_SELS = [
    'span:text-is("私信")',
    'button:text-is("私信")',
    'div:text-is("私信")',
]
DM_BUTTON_FUZZY_SELS = [
    'span:has-text("私信")',
    'button:has-text("私信")',
    '[class*="private-message"]',
    '[class*="privateMessage"]',
    '[class*="chat-btn"]',
    '[class*="chatButton"]',
]
DM_INPUT_SCOPED_SELS = [
    '[class*="chat-input"] div[contenteditable="true"]',
    '[class*="im-input"] div[contenteditable="true"]',
    '[class*="im-editor"] div[contenteditable="true"]',
    '[class*="message-editor"] div[contenteditable="true"]',
]
DM_LOGIN_MODAL_SELS = [
    '[class*="login-mask"]',
    '[class*="login-container"]',
]
# 注意不要用 [class*="semi-modal"] 判登录窗：抖音组件库都是 semi 系，聊天弹窗自身会误报
USER_NOT_EXIST_TEXT_RE = re.compile(r"用户[不未]存在|该用户已注销|找不到用户|页面不存在", re.IGNORECASE)
DM_TITLE_NAME_RE = re.compile(r"^(.+?)(的抖音|的主页|抖音| - )")
# 抖音输入框实际内容常带零宽字符（如 U+200B），内容比对前必须剔除（2026-08-24 真机验证发现）
_ZERO_WIDTH_CHARS_RE = re.compile(r"[​‌‍﻿]")


def _normalize_text(text: str) -> str:
    """剔除零宽字符再压缩空白，用于输入框内容比对。"""
    return " ".join(_ZERO_WIDTH_CHARS_RE.sub("", text).split())

# 发送后自检：全页文本节点搜索目标文本（剔除零宽字符 + 压缩空白），
# 跳过 contenteditable 输入框本身——若发送失败，文本仍留在输入框，会假阳性。
_FIND_DM_TEXT_JS = r"""
(needle) => {
  var strip = function (s) {
    return (s || '').replace(/[​‌‍﻿]/g, '').replace(/\s+/g, ' ');
  };
  var target = strip(needle);
  if (!target) return false;
  var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT);
  var n;
  while ((n = walker.nextNode())) {
    var el = n.parentElement;
    if (el && el.closest && el.closest('[contenteditable="true"]')) continue;
    if (strip(n.textContent).indexOf(target) >= 0) return true;
  }
  return false;
}
"""

# 在评论区 DOM 中按 sec_uid（+可选正文片段）定位目标一级评论的 DOM 下标。
# 只匹配一级评论（跳过嵌套在父评论里的子回复）。
# 2026-08-24 真机：评论区在 DOM 里有两份副本（隐藏 0×0 + 悬浮面板可见），
# 优先返回有真实尺寸的可见副本，否则 hover/点击落在隐藏副本上永远失败。
_FIND_COMMENT_BY_USER_JS = r"""
(target) => {
  // 按 sec_uid 定位一级评论；nth>0 时定位「该用户的第 nth 条评论」（DOM 顺序）。
  // 2026-08-24 真机教训：不能用评论文本消歧——API 返回的表情是 [钱] 占位文本，
  // DOM 里渲染成图片，文本匹配会把目标评论过滤掉。序号计数不受内容影响。
  var t = String((target && target.sec_uid) || '').trim().toLowerCase();
  var nth = Number((target && target.nth) || 0) || 0;
  var items = document.querySelectorAll(
    '[data-e2e="comment-item"], div[class*="comment-item"], div[class*="commentItem"]');
  var best = -1, matched = 0;
  for (var i = 0; i < items.length; i++) {
    var item = items[i];
    var p = item.parentElement, nested = false;
    while (p && p !== document.body) {
      if (p.matches && p.matches('[data-e2e="comment-item"]')) { nested = true; break; }
      p = p.parentElement;
    }
    if (nested) continue;
    var ua = item.querySelector('a[href*="/user/"]');
    var href = ua ? (ua.getAttribute('href') || '') : '';
    var m = href.match(/\/user\/([^/?]+)/);
    var userId = m ? m[1] : '';
    if (t && userId.toLowerCase() !== t) continue;
    matched++;
    if (nth > 0) {
      if (matched < nth) continue;
      // 第 nth 条命中：可见副本优先，否则先记 best 继续找可见项
      if (best < 0) best = i;
    } else {
      if (best < 0) best = i;
    }
    var r = item.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return { idx: i, total: items.length, matched: matched };
  }
  // nth 模式下匹配数不足：第 nth 条还没加载进 DOM → 返回 -1 让外层继续加载更多
  if (nth > 0 && matched < nth) return { idx: -1, total: items.length, matched: matched };
  return { idx: best, total: items.length, matched: matched };
}
"""

# 按比例滚动评论条目的滚动容器（虚拟列表：目标在挂载窗口外时条目 0×0，
# 无法 scroll_into_view，只能滚容器本身让目标进入挂载窗口，2026-08-24 真机）。
# 必须从一个有真实尺寸的条目出发找滚动祖先（隐藏副本的祖先不是真正的滚动容器）。
_SCROLL_COMMENT_CONTAINER_JS = r"""
(ratio) => {
  var items = document.querySelectorAll('[data-e2e="comment-item"]');
  var start = null;
  for (var i = 0; i < items.length && i < 300; i++) {
    var ri = items[i].getBoundingClientRect();
    if (ri.width > 0 && ri.height > 0) { start = items[i]; break; }
  }
  if (!start) return false;
  var p = start;
  while (p && p !== document.body) {
    if (p.scrollHeight > p.clientHeight + 10) {
      var cs = getComputedStyle(p);
      if (/(auto|scroll)/.test(cs.overflowY) || /(auto|scroll)/.test(cs.overflow)) {
        p.scrollTop = Number(ratio) * (p.scrollHeight - p.clientHeight);
        return true;
      }
    }
    p = p.parentElement;
  }
  return false;
}
"""

# 评论区是否已可见（列表容器或任意条目有真实尺寸）。
# 2026-08-24 真机：虚拟列表只有视口窗口内的条目有尺寸，可见条目可能是第 N 条——
# 只查前 3 条会误报「未展开」导致反复点击（点击其实一直有效）。
_COMMENT_VISIBLE_JS = r"""
() => {
  var list = document.querySelector('[data-e2e="comment-list"]');
  if (list) {
    var r = list.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) return true;
  }
  var items = document.querySelectorAll('[data-e2e="comment-item"]');
  for (var i = 0; i < items.length && i < 300; i++) {
    var ri = items[i].getBoundingClientRect();
    if (ri.width > 0 && ri.height > 0) return true;
  }
  return false;
}
"""

# 图集页（/note/）：定位可见的『评论(N)』Tab 中心坐标（供 page.mouse 真实点击）。
# 2026-08-24 真机验证：JS 合成 click（isTrusted=false）点 Tab 被抖音忽略、面板不展开；
# 『评论(N)』文本可能多处出现，优先取右侧面板（#douyin-right-container/detailVideo）内的；
# 返回前用 elementFromPoint 校验坐标确实命中 Tab（防页面重渲染后点错位置）。
_GET_COMMENT_TAB_RECT_JS = r"""
() => {
  var all = document.querySelectorAll('*');
  var candidates = [];
  for (var i = 0; i < all.length; i++) {
    var el = all[i];
    if (el.children.length > 2) continue;
    var t = (el.textContent || '').trim();
    if (/^评论\s*\(\d+\)$/.test(t)) {
      var r = el.getBoundingClientRect();
      if (r.width > 0 && r.height > 0) candidates.push(el);
    }
  }
  if (!candidates.length) return null;
  var best = null;
  for (var j = 0; j < candidates.length; j++) {
    var el = candidates[j];
    if (el.closest('#douyin-right-container') || el.closest('#sliderVideo') ||
        el.closest('[class*="detailVideo"]')) { best = el; break; }
  }
  if (!best) best = candidates[candidates.length - 1];
  var r0 = best.getBoundingClientRect();
  var fullyVisible = r0.top >= 0 && r0.left >= 0 &&
                     r0.bottom <= window.innerHeight && r0.right <= window.innerWidth;
  if (!fullyVisible) {
    // 已可见就不滚：平滑滚动是异步动画，测完坐标到真实点击之间元素会移动导致点空（真机教训）
    best.scrollIntoView({ block: 'center', inline: 'center', behavior: 'instant' });
    r0 = best.getBoundingClientRect();
  }
  var r = r0;
  var cx = r.x + r.width / 2, cy = r.y + r.height / 2;
  var hit = document.elementFromPoint(cx, cy);
  var ok = false;
  var p = hit;
  while (p) {
    if (p === best) { ok = true; break; }
    if (p === document.body) break;
    p = p.parentElement;
  }
  return { cx: cx, cy: cy, ok: ok };
}
"""

# 提交后自检：找「含目标文本 + 新鲜时间戳（刚刚/秒前/分钟前）」的评论条目。
# 排除含 contenteditable 的条目（输入框本身/其容器），防残留文本与乐观渲染回声假阳性。
_FIND_FRESH_COMMENT_JS = r"""
(text) => {
  var t = String(text || '').trim();
  if (!t) return false;
  var fresh = /刚刚|秒前|分钟前/;
  var items = document.querySelectorAll(
    '[data-e2e="comment-item"], div[class*="comment-item"], div[class*="commentItem"]');
  for (var i = 0; i < items.length; i++) {
    var full = items[i].textContent || '';
    if (full.indexOf(t) < 0) continue;
    if (!fresh.test(full)) continue;
    if (items[i].querySelector('[contenteditable="true"]')) continue;
    return true;
  }
  return false;
}
"""


class DouyinCommentBot:
    """评论获取（API）+ 发评论/回复/私信填框（CDP 浏览器自动化）的测试机器人。"""

    def __init__(self) -> None:
        self._playwright = None
        self._cdp_manager: Optional[CDPBrowserManager] = None
        self._browser_context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.client = None  # DouYinClient，复用本项目爬虫的 API 客户端
        self._dm_aux_pages: List[Page] = []  # 私信点开后新开的标签页（close 时统一清理）
        self._adopted_page: Optional[Page] = None  # 复用的用户已有 Tab（close 时不替用户关）
        self._bot_page: Optional[Page] = None  # setup 时 bot 自己开的 Tab
        self._probed = False  # DOM 探测只跑一次（发评论/回复提交前）

    # ─── 初始化：CDP 连接 + 复用爬虫的客户端工厂 ───

    @staticmethod
    def _devtools_ws_url() -> Optional[str]:
        """读 Chrome 的 DevToolsActivePort 文件拿精确 ws 地址（移植自旧项目 ChromeLifecycleService）。

        Chrome 136+ 的现有浏览器调试不再暴露 /json/version，这个文件是官方指定入口。
        """
        import os
        from pathlib import Path

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        dt_file = Path(local_app_data) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
        if not dt_file.is_file():
            return None
        try:
            lines = dt_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) < 2 or not lines[0].strip().isdigit():
                return None
            return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        except OSError:
            return None

    async def setup(self) -> None:
        """按配置连接浏览器并构建 DouYinClient（登录态来自浏览器上下文）。"""
        self._playwright = await async_playwright().start()
        self._cdp_manager = None

        if config.CDP_CONNECT_EXISTING:
            print(f"[CDP 连接] 正在连接现有 Chrome（调试端口 {config.CDP_DEBUG_PORT}）...")
            print("[CDP 连接] 注意：若 Chrome 弹出『远程调试授权』确认框，请点击允许，否则会连接超时")
            # 方式1（优先）：DevToolsActivePort 文件里的精确 ws 地址（旧项目验证过的做法）
            ws_url = self._devtools_ws_url()
            if ws_url:
                try:
                    print(f"[CDP 连接] 尝试 DevToolsActivePort 地址: {ws_url}")
                    cdp_browser = await self._playwright.chromium.connect_over_cdp(ws_url, timeout=15000)
                    if cdp_browser.contexts:
                        self._browser_context = cdp_browser.contexts[0]
                    else:
                        self._browser_context = await cdp_browser.new_context()
                    print("[CDP 连接] ✅ 已连接")
                except Exception as e:
                    raise RuntimeError(
                        f"CDP 连接失败（DevToolsActivePort 地址 {ws_url}）：{e}。"
                        "请确认 Chrome 以远程调试模式运行且已登录抖音，并在 Chrome 弹出的授权框点击『允许』后重试。"
                    ) from e

            # 方式2（回退）：端口文件不存在时走通用现有浏览器连接
            if self._browser_context is None:
                self._cdp_manager = CDPBrowserManager()
                self._browser_context = await self._cdp_manager.launch_and_connect(
                    self._playwright,
                    playwright_proxy=None,
                    user_agent=None,
                    headless=False,
                )
        else:
            # 打包客户端：启动 exe 旁 browser_data/cdp_dy_user_data_dir，绝不读取
            # 用户默认 Chrome 的 DevToolsActivePort，也不会弹出现有浏览器授权框。
            print("[CDP 连接] 正在启动客户端独立抖音浏览器...")
            saved_platform = config.PLATFORM
            try:
                config.PLATFORM = "dy"
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
        # 注意：不要 set_viewport_size —— 对 CDP 连接的现有浏览器，它会施加 Emulation
        # 视口覆盖，在用户真实窗口尺寸下页面渲染异常（真机出现过"异常放大"卡死）。
        await self.page.goto("https://www.douyin.com", wait_until="domcontentloaded", timeout=30000)

        # 复用爬虫核心的客户端创建逻辑（与 core.create_douyin_client 完全同源）
        crawler = DouYinCrawler()
        crawler.browser_context = self._browser_context
        crawler.context_page = self.page
        crawler.cdp_manager = self._cdp_manager
        self.client = await crawler.create_douyin_client(httpx_proxy=None)

        if not await self.client.pong(browser_context=self._browser_context):
            print("⚠️ 未检测到抖音登录态：抓评论可能受限；发评论/回复必须先在 Chrome 里登录抖音")

    async def close(self, close_page: bool = True) -> None:
        """关闭自己打开的标签页（close_page=True）。

        连接现有浏览器时不关闭用户上下文；独立模式则清理程序自己启动的浏览器。
        close_page=False（私信只填不送流程）在现有浏览器模式下保留标签页供用户确认。
        """
        if close_page:
            for pg in [self.page] + getattr(self, "_dm_aux_pages", []):
                if pg is getattr(self, "_adopted_page", None):
                    continue  # 复用的是用户自己开的 Tab，不替用户关
                try:
                    if pg and not pg.is_closed():
                        await asyncio.wait_for(pg.close(), timeout=10)
                except Exception:
                    pass
        self._dm_aux_pages = []
        if self._cdp_manager is not None and not config.CDP_CONNECT_EXISTING:
            try:
                await asyncio.wait_for(self._cdp_manager.cleanup(), timeout=20)
            except Exception:
                pass
        if self._playwright:
            try:
                # 2026-08-24 真机：崩溃路径下 playwright.stop() 在 Windows 会挂死进程，必须限时
                await asyncio.wait_for(self._playwright.stop(), timeout=15)
            except Exception:
                pass

    # ─── 评论获取：走本项目 API 爬虫（快） ───

    async def fetch_comments_by_api(self, video_url: str, max_count: int = 30) -> List[Dict[str, Any]]:
        """用 DouYinClient 的评论 API 拉取评论，返回含 sec_uid 的原始结构（不落库、不脱敏）。"""
        video_info = parse_video_info_from_url(video_url)
        if video_info.url_type == "short":
            resolved = await self.client.resolve_short_url(video_url)
            if not resolved:
                raise RuntimeError("短链接解析失败，请换完整视频链接重试")
            video_info = parse_video_info_from_url(resolved)
        aweme_id = video_info.aweme_id
        print(f"[API 评论] aweme_id={aweme_id}, max_count={max_count}")

        detail = await self.client.get_video_by_id(aweme_id)
        if isinstance(detail, dict) and detail.get("desc"):
            print(f"[视频标题] {detail.get('desc')[:60]}")

        raw_comments = await self.client.get_aweme_all_comments(
            aweme_id=aweme_id,
            crawl_interval=0.6,
            is_fetch_sub_comments=False,
            callback=None,
            max_count=max_count,
        )
        comments: List[Dict[str, Any]] = []
        for c in raw_comments:
            user = c.get("user") or {}
            comments.append({
                "cid": c.get("cid", ""),
                "text": (c.get("text") or "").strip(),
                "douyin_id": user.get("unique_id") or user.get("short_id", ""),
                "sec_uid": user.get("sec_uid", ""),
                "uid": user.get("uid", ""),
                "nickname": user.get("nickname", ""),
                "digg_count": c.get("digg_count", 0),
                "reply_comment_total": c.get("reply_comment_total", 0),
                "create_time": c.get("create_time", 0),
            })
        print(f"[API 评论] 共获取 {len(comments)} 条")
        return comments

    # ─── 打开视频页（DOM 写操作的前置） ───

    async def open_video_page(self, video_url: str) -> None:
        # 优先复用用户已打开的目标页（2026-08-24 真机教训：bot 自开新 Tab 偶发渲染卡死，
        # 用户手动开的 Tab 稳定正常），不重载页面、不做 goto。
        adopted = False
        try:
            aweme_id = parse_video_info_from_url(video_url).aweme_id
            if aweme_id and self._browser_context:
                for pg in self._browser_context.pages:
                    try:
                        if (pg is not self.page and not pg.is_closed()
                                and aweme_id in (pg.url or "")):
                            print(f"[复用现有 Tab] {pg.url[:100]}")
                            self.page = pg
                            self._adopted_page = pg
                            adopted = True
                            break
                    except Exception:
                        pass
        except Exception:
            pass
        print(f"[打开视频页] {video_url}")
        if adopted:
            await self.page.wait_for_timeout(1500)
        else:
            await self.page.goto(video_url, wait_until="domcontentloaded", timeout=30000)
            await self.page.wait_for_timeout(2000)
        # 关键：把目标页切到前台。后台标签页的 rAF 被浏览器节流，面板切换/编辑器挂载
        # 等 UI 更新不执行——点击事件派发了也没效果（2026-08-24 真机：时灵时不灵根因）。
        try:
            await self.page.bring_to_front()
            await self.page.wait_for_timeout(800)
        except Exception:
            pass
        if not await self._is_logged_in():
            print("⚠️ 未检测到登录态：请在 Chrome 里先登录抖音再执行写操作")
        if await self._detect_captcha():
            print("⚠️ 检测到验证码：请先在 Chrome 里手动完成验证，再重试写操作")
        await self._scroll_to_comments()
        await self._activate_comment_tab()

    # ─── 发评论（DOM，移植自 DouyinService.postComment） ───

    async def post_comment(self, text: str) -> bool:
        """发布新评论。返回自检结果（True=评论区确认出现，False=已提交但自检未确认）。"""
        print(f"[发评论] {text}")
        await self._expand_comment_box()
        # 图集页悬浮面板：输入栏在评论列表底部（懒挂载），先滚到底让输入栏进入视口
        try:
            await self.page.evaluate(_SCROLL_COMMENT_CONTAINER_JS, 1.0)
            await self.page.wait_for_timeout(2000)
        except Exception:
            pass
        await self._fill_comment_box(text)
        await self._probe_once()
        await self._click_submit_button()
        await self.page.wait_for_timeout(3000)
        return await self._self_check_posted(text)

    # ─── 回复某条评论（DOM，移植自 DouyinService 回复流程） ───

    async def reply_to_comment(self, sec_uid: str, text: str, nth: int = 0, max_rounds: int = 15) -> bool:
        """在评论区滚动查找 sec_uid 对应的一级评论，点『回复』后填入 text 提交。
        nth>0 时定位「该用户第 nth 条评论」（DOM 顺序，不用文本消歧——表情占位文本与 DOM 图片对不上）。
        返回自检结果（True=评论区确认出现，False=已提交但自检未确认）。"""
        nth_desc = f"（该用户第 {nth} 条）" if nth > 0 else ""
        print(f"[回复] 目标 sec_uid={sec_uid}{nth_desc}, 内容={text}")
        idx = -1
        total = 0
        for round_no in range(1, max_rounds + 1):
            idx, total = await self._find_comment_dom_index(sec_uid, nth)
            if idx >= 0:
                print(f"  第 {round_no} 轮找到目标评论 (DOM 下标 {idx}/{total})")
                break
            print(f"  第 {round_no} 轮未找到目标评论，加载更多...")
            if not await self._click_load_more_if_present():
                await self._scroll_comment_container()
            await self.page.wait_for_timeout(1500)
        if idx < 0:
            raise RuntimeError(
                f"滚动 {max_rounds} 轮后仍未在评论区找到目标评论（sec_uid={sec_uid}{nth_desc}）。"
                "可能该评论排序靠后未加载，或评论已被删除；可换更靠前的评论重试。")

        # 目标可能在虚拟列表挂载窗口外（0×0 条目没有位置，无法 scroll_into_view）：
        # ① 按比例滚动评论容器让目标进入挂载窗口；② 滚动会改变列表，滚完必须重找下标。
        item = self.page.locator(COMMENT_ITEM_SELS).nth(idx)
        visible = False
        try:
            visible = await item.is_visible()
        except Exception:
            visible = False
        if not visible:
            try:
                await self.page.evaluate(_SCROLL_COMMENT_CONTAINER_JS, idx / max(total, 1))
                await self.page.wait_for_timeout(1500)
            except Exception:
                pass
            idx2, total = await self._find_comment_dom_index(sec_uid, nth)
            if idx2 < 0:
                raise RuntimeError("目标评论滚动后失联（虚拟列表刷新），请重试")
            item = self.page.locator(COMMENT_ITEM_SELS).nth(idx2)
            if await item.count() == 0:
                raise RuntimeError("目标评论元素已失效（评论区可能已刷新），请重试")
            try:
                visible = await item.is_visible()
            except Exception:
                visible = False
        if not visible:
            # 仍不可见：面板未展开 → 快速失败；面板已展开 → 微调滚动再试一次
            try:
                panel_open = await self.page.evaluate(_COMMENT_VISIBLE_JS)
            except Exception:
                panel_open = False
            if not panel_open:
                raise RuntimeError(
                    "评论区未展开（图集页需先点开右侧『评论(N)』Tab）。"
                    "请在 Chrome 打开评论区后重试，或换一个评论区已展开的目标。")
            try:
                await item.scroll_into_view_if_needed(timeout=3000)
                await self.page.wait_for_timeout(800)
                idx3, total = await self._find_comment_dom_index(sec_uid, nth)
                if idx3 < 0:
                    raise RuntimeError("目标评论滚动后失联（虚拟列表刷新），请重试")
                item = self.page.locator(COMMENT_ITEM_SELS).nth(idx3)
            except Exception:
                pass
        ok = await self._click_reply_button(item)
        if not ok:
            await item.hover(timeout=3000)
            await self.page.wait_for_timeout(800)
            ok = await self._click_reply_button(item)
        if not ok:
            raise RuntimeError("找不到该评论的『回复』按钮")
        await self.page.wait_for_timeout(1500)
        await self._fill_reply_input(text)
        await self._probe_once()
        await self._submit_reply()
        await self.page.wait_for_timeout(2000)
        return await self._self_check_posted(text)

    # ═══ 以下 DOM 辅助方法（1:1 移植 DouyinService.java，含多策略兜底） ═══

    async def _is_logged_in(self) -> bool:
        for sel in ['[data-e2e="user-avatar"]', 'img[class*="avatar"]', '[data-e2e="profile-icon"]']:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                pass
        try:
            login_btn = self.page.locator('span:text-is("登录"), button:has-text("登录")').first
            if await login_btn.count() > 0 and await login_btn.is_visible():
                return False
        except Exception:
            pass
        for sel in ['span:text-is("私信")', 'button:has-text("私信")',
                    'span:text-is("已关注")', 'button:has-text("已关注")',
                    'span:text-is("关注")', 'button:has-text("关注")']:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                pass
        url = self.page.url or ""
        return "/video/" in url or "/note/" in url or "/user/" in url or "/im/" in url

    async def _detect_captcha(self) -> bool:
        for sel in CAPTCHA_SELS:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                pass
        url = (self.page.url or "").lower()
        return "verify" in url or "captcha" in url

    async def _scroll_to_comments(self) -> None:
        for i in range(15):
            for sel in COMMENT_AREA_SELS:
                loc = self.page.locator(sel).first
                try:
                    if await loc.count() > 0 and await loc.is_visible():
                        await loc.scroll_into_view_if_needed()
                        await self.page.wait_for_timeout(1000)
                        return
                except Exception:
                    pass
            await self.page.evaluate("() => window.scrollBy(0, 500)")
            await self.page.wait_for_timeout(800)
        print("⚠️ 评论区未出现（页面结构可能有变化）")

    async def _activate_comment_tab(self, poll_timeout_ms: int = 20000) -> bool:
        """图集页（/note/）评论区藏在『相关推荐 | 评论(N)』Tab 后面，需点 Tab 展开。

        2026-08-24 真机排查：/video/ 图集帖会跳 /note/，评论列表 0×0 隐藏、无输入框，
        Tab 条懒加载（实测 5-8s 才挂载）。JS 合成 click（isTrusted=false）点 Tab 无效，
        必须 page.mouse 真实点击。
        铁律：Tab 是互斥切换——点击一次后必须长等确认（15s），不得短间隔重复点击，
        否则来回来回切换永远等不到展开；点击前用 elementFromPoint 校验坐标命中。
        本方法两种布局通用：评论已可见（经典视频页/已激活）→ 直接返回 True；
        有 Tab → 真实点击等展开；都没有 → 返回 False 交写操作报错。
        """
        # 先快速确认评论是否已可见（最多 2.5s）
        for _ in range(5):
            try:
                if await self.page.evaluate(_COMMENT_VISIBLE_JS):
                    print("  评论区已可见（无需激活 Tab）")
                    return True
            except Exception:
                return False
            await self.page.wait_for_timeout(500)

        # 轮询等『评论(N)』Tab 挂载，坐标校验通过后点击；单次点击后长等 15s
        for _ in range(max(1, poll_timeout_ms // 1000)):
            try:
                pos = await self.page.evaluate(_GET_COMMENT_TAB_RECT_JS)
            except Exception:
                pos = None
            if pos and pos.get("ok"):
                print("  已定位『评论(N)』Tab（坐标校验通过），真实鼠标点击...")
                for click_no in range(2):
                    try:
                        await self.page.mouse.click(pos["cx"], pos["cy"])
                    except Exception:
                        pass
                    for _wait in range(15):
                        await self.page.wait_for_timeout(1000)
                        try:
                            if await self.page.evaluate(_COMMENT_VISIBLE_JS):
                                print("  ✅ 评论区已展开")
                                return True
                        except Exception:
                            return False
                    if click_no == 0:
                        print("  点击后 15s 未展开，重新取坐标补点一次...")
                        try:
                            pos = await self.page.evaluate(_GET_COMMENT_TAB_RECT_JS)
                        except Exception:
                            pos = None
                        if not (pos and pos.get("ok")):
                            return False
                return False
            await self.page.wait_for_timeout(1000)
        print("⚠️ 未找到评论 Tab 或评论区未展开（页面结构可能已变化）")
        return False

    async def _count_comment_items(self) -> int:
        try:
            return await self.page.locator(COMMENT_ITEM_SELS).count()
        except Exception:
            return 0

    async def _click_load_more_if_present(self) -> bool:
        for text in ["点击加载更多", "加载更多", "展开更多评论", "查看全部评论"]:
            for tag in ["span", "button", "div"]:
                try:
                    btn = self.page.locator(f'{tag}:has-text("{text}")').first
                    if await btn.count() > 0 and await btn.is_visible():
                        await btn.click()
                        await self.page.wait_for_timeout(2500)
                        return True
                except Exception:
                    pass
        return False

    async def _scroll_comment_container(self) -> bool:
        # 方案A: 最后一个评论条目滚入视野（最可靠）
        try:
            items = self.page.locator(COMMENT_ITEM_SELS)
            if await items.count() > 0:
                await items.last.scroll_into_view_if_needed()
                await self.page.wait_for_timeout(1500)
                return True
        except Exception:
            pass
        # 方案B: 评论区容器内 mouse wheel
        for sel in COMMENT_AREA_SELS:
            try:
                container = self.page.locator(sel).first
                if await container.count() > 0:
                    box = await container.bounding_box()
                    if box:
                        cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
                        await self.page.mouse.move(cx, cy)
                        await self.page.mouse.wheel(0, 1000)
                        await self.page.wait_for_timeout(1500)
                        return True
            except Exception:
                pass
        # 方案C: window 滚动兜底
        await self.page.evaluate("() => window.scrollBy(0, 1200)")
        await self.page.wait_for_timeout(1500)
        return False

    async def _find_comment_dom_index(self, sec_uid: str, nth: int = 0):
        """返回 (下标, 总条目数)；未找到下标为 -1。nth>0 时定位该用户第 nth 条评论。"""
        try:
            result = await self.page.evaluate(_FIND_COMMENT_BY_USER_JS, {"sec_uid": sec_uid, "nth": nth})
            if result and isinstance(result, dict):
                return int(result.get("idx", -1)), int(result.get("total", 0))
            return -1, 0
        except Exception:
            return -1, 0

    async def _click_reply_button(self, item: Locator) -> bool:
        reply_sels = [
            'span:text-is("回复")',
            'button:text-is("回复")',
            ':text-is("回复")',
            '[class*="reply-btn"]',
            '[class*="replyButton"]',
            'span:has-text("回复")',
        ]
        for sel in reply_sels:
            try:
                btn = item.locator(sel).first
                if await btn.count() > 0:
                    force = sel.startswith("[class") or sel.startswith("span:has")
                    # 非强制点击限 3s：评论区未展开时条目 0×0，30s 默认等待=干等（真机教训）
                    await btn.click(force=force, timeout=3000)
                    return True
            except Exception:
                pass
        return False

    async def _expand_comment_box(self) -> None:
        # 策略1: data-e2e
        try:
            loc = self.page.locator('[data-e2e="comment-input"], [data-e2e="comment-input-area"]').first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click()
                await self.page.wait_for_timeout(1000)
                return
        except Exception:
            pass
        # 策略2: contenteditable
        try:
            loc = self.page.locator('div[contenteditable="true"]').first
            if await loc.count() > 0:
                await loc.click()
                await self.page.wait_for_timeout(1000)
                return
        except Exception:
            pass
        # 策略3: textarea
        try:
            loc = self.page.locator("textarea").first
            if await loc.count() > 0:
                await loc.click()
                await self.page.wait_for_timeout(1000)
                return
        except Exception:
            pass
        # 策略3.5: 点评论输入栏（视频页布局：编辑器懒加载，需点一下输入栏才挂载 contenteditable。
        # 2026-08-24 真机：Java 项目能成功就是盲点策略恰好点中此栏；此处只点 comment-input 类
        # 与占位文案，不用宽泛 div[class*="comment"]（可能误点图片区域开大图浏览器）。
        # 铁律：输入栏在 DOM 里也可能有两份副本（隐藏+可见悬浮面板），必须遍历匹配
        # 点第一个「可见」的，.first 会命中隐藏副本（0×0）导致全部跳过。
        # locator.click 会自动滚动到视野内。）
        for sel in ['div[class*="comment-input"]', 'div[class*="commentInput"]',
                    ':text("留下你的精彩评论吧")', ':text("说点儿好听的")',
                    ':text("有爱评论")', ':text("善语结善缘")']:
            try:
                locs = self.page.locator(sel)
                n = await locs.count()
                for i in range(min(n, 8)):
                    loc = locs.nth(i)
                    if await loc.is_visible():
                        await loc.click()
                        await self.page.wait_for_timeout(1500)
                        return
            except Exception:
                pass
        # 策略4: 点评论区容器
        # 2026-08-24 真机教训：`div[class*="comment"]` 首位可能命中图片/笔记区域，
        # 盲点会误开大图浏览器（用户看到"一张大图卡死"）。改为点评论列表容器（若可见）。
        try:
            loc = self.page.locator('[data-e2e="comment-list"]').first
            if await loc.count() > 0 and await loc.is_visible():
                await loc.click(timeout=3000)
                await self.page.wait_for_timeout(1500)
        except Exception:
            pass

    async def _fill_comment_box(self, text: str) -> None:
        for sel in CONTENT_EDITABLE_SELS:
            try:
                await self.page.wait_for_selector(sel, state="visible", timeout=4000)
                await self.page.wait_for_timeout(300)
                box = self.page.locator(sel).first
                await box.click()
                await self.page.wait_for_timeout(200)
                await box.fill(text)
                print(f"  ✅ 评论已填入 ({sel})")
                await self.page.wait_for_timeout(500)
                return
            except Exception:
                pass
        # 兜底前：再点一次输入栏等编辑器挂载（视频页编辑器懒加载，真机 2026-08-24）
        await self._expand_comment_box()
        try:
            await self.page.wait_for_selector('div[contenteditable="true"]', state="visible", timeout=3000)
            box = self.page.locator('div[contenteditable="true"]').first
            await box.click()
            await self.page.wait_for_timeout(200)
            await box.fill(text)
            print("  ✅ 评论已填入 (点击输入栏后编辑器挂载)")
            await self.page.wait_for_timeout(500)
            return
        except Exception:
            pass
        # 兜底: 键盘逐字输入（模拟人类）
        print("  使用键盘逐字输入...")
        await self._human_type(text)

    async def _human_type(self, text: str, box: Optional[Locator] = None) -> None:
        """键盘逐字输入。box 指定目标输入框（私信面板等场景），缺省取第一个 contenteditable（评论路径）。

        输入前 Ctrl+A 全选：若框内已有残留文本（如 fill 已写入），逐字输入会替换而非追加。
        """
        try:
            if box is None:
                box = self.page.locator('div[contenteditable="true"]').first
            await box.click()
            await self.page.wait_for_timeout(200)
            await self.page.keyboard.press("Control+A")
            for ch in text:
                await self.page.keyboard.type(ch)
                await self.page.wait_for_timeout(50 + random.randint(0, 150))
        except Exception as e:
            raise RuntimeError(f"无法输入内容: {e}")

    async def _probe_dom(self) -> None:
        """DOM 探测：报告当前页面命中了哪些选择器、真实 class 是什么。
        抖音前端发版后 hash class 会变，看这日志即可定位改哪里。只读、尽力而为，失败不阻断。
        """
        print("[DOM 探测] 评论相关选择器命中情况：")
        try:
            async def _hit(sel: str) -> int:
                try:
                    return await self.page.locator(sel).count()
                except Exception:
                    return -1

            comment_item_e2e = await _hit('[data-e2e="comment-item"]')
            comment_item_cls = await _hit('div[class*="comment-item"], div[class*="commentItem"]')
            comment_list_e2e = await _hit('[data-e2e="comment-list"]')
            editable = await _hit('div[contenteditable="true"]')
            print(f"[DOM 探测] comment-item: data-e2e={comment_item_e2e}, class={comment_item_cls}; "
                  f"comment-list data-e2e={comment_list_e2e}; contenteditable={editable}")

            for label, sels in [
                ("输入栏", ['[data-e2e="comment-input"]', 'div[class*="comment-input"]', 'div[class*="commentInput"]']),
                ("发送按钮", ['div[class*="commentInput-right"] [class*="send"]',
                            'div[class*="commentInput-right"] [class*="submit"]',
                            'div[class*="comment-input"] [class*="send"]',
                            'div[class*="commentInput"] span.wchsYBpK',
                            'div[class*="comment-input"] span.wchsYBpK']),
            ]:
                for sel in sels:
                    try:
                        loc = self.page.locator(sel).first
                        if await loc.count() > 0 and await loc.is_visible():
                            cls = ""
                            try:
                                cls = await loc.evaluate("el => el.className || ''")
                            except Exception:
                                pass
                            print(f"[DOM 探测] {label}: 命中 {sel}  → class={str(cls)[:80]}")
                            break
                    except Exception:
                        pass
        except Exception as e:
            print(f"[DOM 探测] 失败（忽略）: {type(e).__name__}")

    async def _probe_once(self) -> None:
        """每个 bot 实例只在第一次提交前探测一次（此时输入框已挂载、发送按钮已出现）。"""
        if self._probed:
            return
        self._probed = True
        await self._probe_dom()

    async def _click_submit_button(self) -> None:
        await self.page.wait_for_timeout(1000)
        # 用户确认：发评论按回车即可直接发送（DraftJS 编辑器聚焦后 Enter=提交）。
        # 回车不依赖任何 class/图标，前端改版最稳，作为第一步。
        # 策略1: 评论区 contenteditable 按 Enter（先 scoped，再最后一个兜底）
        for sel in ['[data-e2e="comment-list"] div[contenteditable="true"]',
                    'div[class*="comment-mainContent"] div[contenteditable="true"]',
                    'div[class*="comment-container"] div[contenteditable="true"]',
                    'div[contenteditable="true"]']:
            try:
                box = self.page.locator(sel).last
                if await box.count() > 0:
                    await box.press("Enter")
                    await self.page.wait_for_timeout(2000)
                    print("  ✅ 已按 Enter 发送")
                    return
            except Exception:
                pass
        # 策略2: 发送按钮（结构/语义前缀优先，硬编码哈希 class 兜底）。
        # 铁律：输入区可能有隐藏副本，必须遍历匹配点第一个可见的。
        for sel in ['div[class*="commentInput-right"] [class*="send"]',
                    'div[class*="commentInput-right"] [class*="submit"]',
                    'div[class*="comment-input"] [class*="send"]',
                    'div[class*="comment-input"] [class*="submit"]',
                    'div[class*="commentInput-right"] span:last-of-type',
                    'div[class*="commentInput"] span.wchsYBpK',
                    'div[class*="comment-input"] span.wchsYBpK']:
            try:
                locs = self.page.locator(sel)
                n = await locs.count()
                for i in range(min(n, 8)):
                    btn = locs.nth(i)
                    if await btn.is_visible():
                        await btn.click(timeout=3000)
                        await self.page.wait_for_timeout(2000)
                        print("  ✅ 已点发送按钮发送")
                        return
            except Exception:
                pass
        # 策略3: 文本按钮「发送/发布」
        for area_sel in ['div[class*="comment-input"]', 'div[class*="commentInput"]'] + COMMENT_CONTAINER_SELS:
            for t in ["发送", "发布"]:
                try:
                    locs = self.page.locator(f'{area_sel} :text-is("{t}")')
                    n = await locs.count()
                    for i in range(min(n, 8)):
                        btn = locs.nth(i)
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.click(timeout=3000)
                            await self.page.wait_for_timeout(2000)
                            return
                except Exception:
                    pass
        # 策略4: 评论区容器内『发送/发布』按钮原生点击
        for container in COMMENT_CONTAINER_SELS:
            for t in ["发送", "发布"]:
                try:
                    sel = (f'{container} button:text-is("{t}"), {container} span:text-is("{t}"), '
                           f'{container} [class*="submit"], {container} [class*="send"]')
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                        await btn.evaluate("el => el.click()")  # 原生 click，不走 Playwright 事件链
                        await self.page.wait_for_timeout(2000)
                        return
                except Exception:
                    pass
        # 策略5: 全局 Enter 兜底
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(2000)

    async def _fill_reply_input(self, text: str) -> None:
        # 点『回复』后会出现第二个 contenteditable，轮询等它出现
        content_editable_count = 0
        for _ in range(10):
            await self.page.wait_for_timeout(400)
            try:
                content_editable_count = await self.page.locator('div[contenteditable="true"]').count()
                if content_editable_count >= 2:
                    break
            except Exception:
                pass
        # 策略1: 最后一个 contenteditable 就是回复框
        try:
            all_boxes = self.page.locator('div[contenteditable="true"]')
            count = await all_boxes.count()
            if count >= 1:
                box = all_boxes.last
                await box.click()
                await self.page.wait_for_timeout(300)
                await box.fill(text)
                print(f"  ✅ 回复已填入 (第 {count} 个 contenteditable)")
                await self.page.wait_for_timeout(500)
                return
        except Exception:
            pass
        # 策略2: 评论区容器内的 contenteditable
        for container in COMMENT_CONTAINER_SELS:
            try:
                box = self.page.locator(f'{container} div[contenteditable="true"]').last
                if await box.count() > 0:
                    await box.click()
                    await self.page.wait_for_timeout(300)
                    await box.fill(text)
                    await self.page.wait_for_timeout(500)
                    return
            except Exception:
                pass
        # 策略3: textarea 兜底
        try:
            ta = self.page.locator("textarea").last
            if await ta.count() > 0:
                await ta.click()
                await self.page.wait_for_timeout(300)
                await ta.fill(text)
                await self.page.wait_for_timeout(500)
                return
        except Exception:
            pass
        # 策略4: 键盘逐字输入兜底
        await self._human_type(text)

    async def _submit_reply(self) -> None:
        await self.page.wait_for_timeout(800)
        # 策略1: 最后一个 contenteditable 按 Enter
        try:
            all_boxes = self.page.locator('div[contenteditable="true"]')
            if await all_boxes.count() > 0:
                await all_boxes.last.press("Enter")
                await self.page.wait_for_timeout(2000)
                return
        except Exception:
            pass
        # 策略2: 评论区容器内 Enter
        for sel in ['[data-e2e="comment-list"] div[contenteditable="true"]',
                    'div[class*="comment-mainContent"] div[contenteditable="true"]',
                    'div[class*="comment-container"] div[contenteditable="true"]']:
            try:
                boxes = self.page.locator(sel)
                if await boxes.count() > 0:
                    await boxes.last.press("Enter")
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        # 策略3: 评论区『发送/发布』按钮（取最后一个，回复的发送按钮后出现）
        for container in COMMENT_CONTAINER_SELS:
            for t in ["发送", "发布"]:
                try:
                    sel = (f'{container} button:text-is("{t}"), {container} span:text-is("{t}"), '
                           f'{container} [class*="submit"], {container} [class*="send"]')
                    btns = self.page.locator(sel)
                    if await btns.count() > 0:
                        btn = btns.last
                        if await btn.is_visible() and await btn.is_enabled():
                            await btn.evaluate("el => el.click()")
                            await self.page.wait_for_timeout(2000)
                            return
                except Exception:
                    pass
        # 策略4: 全局 Enter 兜底
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(2000)

    async def _self_check_posted(self, text: str) -> bool:
        """提交后自检：评论文本是否以「新鲜评论」形态出现在评论区 DOM。

        2026-08-24 真机教训：评论区隐藏时 DOM 里可能有乐观渲染回声/输入框残留，
        仅凭文本命中会假阳性（当时报『已发布』但 API 查无此评）。必须命中项同时含
        新鲜时间戳（刚刚/秒前/分钟前）且不含 contenteditable（排除输入框本身）。
        返回 bool 供上层（agent 工具）区分「确认发布」与「已提交但未确认」。
        """
        try:
            found = await self.page.evaluate(_FIND_FRESH_COMMENT_JS, text)
            if found:
                print("✅ 自检通过：评论已以新鲜时间戳出现在评论区")
                return True
            print("⚠️ 自检未确认发布成功：请切到 Chrome 肉眼确认（可能被风控拦截或弹出验证码）")
            return False
        except Exception:
            print("⚠️ 自检失败，请到 Chrome 人工确认")
            return False

    # ─── 私信（CDP DOM 写操作，移植自 DouyinService.sendDmByUserId；本阶段只填框不发送） ───

    async def open_user_profile(self, sec_uid: str) -> str:
        """导航到用户主页并完成检查（验证码 → 登录 → 用户存在），返回目标用户昵称。"""
        print(f"[打开主页] https://www.douyin.com/user/{sec_uid}")
        await self.page.goto(f"https://www.douyin.com/user/{sec_uid}",
                             wait_until="domcontentloaded", timeout=30000)
        await self._wait_user_profile_page()
        if await self._detect_captcha():
            raise RuntimeError("检测到验证码：请先在 Chrome 里手动完成验证，再重试")
        if not await self._is_logged_in():
            raise RuntimeError("未检测到登录态：私信需要先在 Chrome 里登录抖音")
        if await self._is_user_not_exist():
            raise RuntimeError(f"用户不存在或已注销（sec_uid={sec_uid}）")
        name = await self._extract_username_from_profile()
        print(f"  👤 目标用户: {name}")
        return name

    async def fill_dm_input(self, text: str) -> None:
        """点『私信』按钮 → 等聊天面板 → 填入 text。只填不送，填完自检并保持页面打开供肉眼确认。"""
        print(f"[私信填框] {text}")
        pages_before = set(self._browser_context.pages) if self._browser_context else set()
        await self._click_dm_button()
        await self.page.wait_for_timeout(2500)
        await self._switch_to_new_tab(pages_before)
        await self._wait_dm_panel()
        await self._fill_dm_box(text)
        print("✅ 私信内容已填入输入框（未发送）。页面保持打开，请到 Chrome 确认内容无误后手动发送。")

    # ═══ 以下私信 DOM 辅助方法 ═══

    async def _switch_to_new_tab(self, pages_before) -> None:
        """点『私信』后若开了新标签页，把当前操作页切换过去（旧页保留，close 时统一清理）。"""
        try:
            if self._browser_context is None:
                return
            new_pages = [p for p in self._browser_context.pages if p not in pages_before]
            if new_pages:
                self._dm_aux_pages.append(self.page)
                self.page = new_pages[0]
                print(f"  🔗 私信打开了新标签页，已切换: {self.page.url}")
                await self.page.wait_for_timeout(2000)
        except Exception:
            pass

    async def _wait_user_profile_page(self) -> None:
        # 1) 先等 URL 匹配（navigate 后 URL 通常已正确）
        try:
            await self.page.wait_for_url("**/user/**", timeout=5000)
            print(f"  ✅ URL 已匹配 /user/: {self.page.url}")
        except Exception:
            print(f"  ⏳ URL 未匹配 /user/，当前: {self.page.url}")
        # 2) 等网络空闲（短超时；抖音后台请求不停几乎必超时，超时吞掉不阻塞）
        try:
            await self.page.wait_for_load_state("networkidle", timeout=3000)
        except Exception:
            pass
        # 3) 快速检查 profile 选择器（短超时）
        for sel in DM_PROFILE_SELS:
            try:
                await self.page.wait_for_selector(sel, state="visible", timeout=2000)
                print(f"  ✅ 用户主页元素 ({sel})")
                break
            except Exception:
                pass

    async def _is_user_not_exist(self) -> bool:
        # 检查 title 是否为空
        try:
            title = (await self.page.title()) or ""
        except Exception:
            title = ""
        if not title.strip():
            print("  ⚠️ 页面标题为空，用户可能不存在")
            return True
        # 检查页面上是否有"用户不存在"文本（限定常见文本标签，避免 body 级误匹配）
        try:
            found = (self.page.locator("div, span, button, p, h1, h2, h3")
                     .filter(has_text=USER_NOT_EXIST_TEXT_RE).first)
            if await found.count() > 0 and await found.is_visible():
                print("  ⚠️ 页面显示用户不存在")
                return True
        except Exception:
            pass
        # 检查是否有 profile 相关按钮（关注/私信/分享主页），没有则用户可能不存在
        try:
            btn_count = await self.page.locator(
                'button:has-text("关注"), button:has-text("私信"), button:has-text("分享主页")').count()
            if btn_count == 0:
                print("  ⚠️ 未找到任何 profile 按钮，用户可能不存在")
                return True
        except Exception:
            pass
        return False

    async def _extract_username_from_profile(self) -> str:
        for sel in DM_NAME_SELS:
            try:
                el = self.page.locator(sel).first
                if await el.count() > 0:
                    name = ((await el.text_content()) or "").strip()
                    if name:
                        return name
            except Exception:
                pass
        # 兜底1: 从 document.title 提取（"用户名 的抖音 - 抖音"）
        try:
            title = (await self.page.title()) or ""
        except Exception:
            title = ""
        if title:
            m = DM_TITLE_NAME_RE.match(title)
            if m:
                return m.group(1).strip()
        # 兜底2: h1/h2
        for tag in ["h1", "h2"]:
            try:
                el = self.page.locator(tag).first
                if await el.count() > 0:
                    text = ((await el.text_content()) or "").strip()
                    if text:
                        return text
            except Exception:
                pass
        return title or "(未知用户)"

    async def _click_dm_button(self) -> None:
        print("🖱️  定位私信按钮...")
        # 策略 1: data-e2e 选择器
        for sel in DM_BUTTON_PRIMARY_SELS:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click()
                    print(f"  ✅ 已点击 ({sel})")
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        # 策略 2: 精确文本匹配（force 点击）
        for sel in DM_BUTTON_TEXT_SELS:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True)
                    print(f"  ✅ 已点击 ({sel})")
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        # 策略 3: 模糊文本匹配（has-text + class 前缀）
        for sel in DM_BUTTON_FUZZY_SELS:
            try:
                btn = self.page.locator(sel).first
                if await btn.count() > 0 and await btn.is_visible():
                    await btn.click(force=True)
                    print(f"  ✅ 已点击 ({sel})")
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        # 策略 4: hover 头像区域后再找（某些私信按钮在 hover 后才出现）
        try:
            avatar = self.page.locator('[data-e2e="user-avatar"], img[class*="avatar"]').first
            if await avatar.count() > 0:
                await avatar.hover()
                await self.page.wait_for_timeout(1500)
                for sel in DM_BUTTON_TEXT_SELS:
                    try:
                        btn = self.page.locator(sel).first
                        if await btn.count() > 0 and await btn.is_visible():
                            await btn.click(force=True)
                            print(f"  ✅ hover 后点击 ({sel})")
                            await self.page.wait_for_timeout(2000)
                            return
                    except Exception:
                        pass
        except Exception:
            pass
        # 策略 5: filter 精确匹配可见元素（排除 semi 组件库嵌套子元素）
        for tag in ["span", "button", "div", "a"]:
            try:
                matches = (self.page.locator(tag)
                           .filter(has_text=re.compile(r"^私信$"))
                           .filter(has_not=self.page.locator('[class*="semi"]')))
                count = await matches.count()
                for i in range(count):
                    el = matches.nth(i)
                    if await el.is_visible():
                        await el.click(force=True)
                        print(f"  ✅ filter 点击: <{tag}>")
                        await self.page.wait_for_timeout(2000)
                        return
            except Exception:
                pass
        raise RuntimeError("无法找到私信按钮：该用户可能关闭了私信功能")

    async def _wait_dm_panel(self, timeout_ms: int = 10000) -> None:
        print("💬 等待聊天面板...")
        deadline = time.monotonic() + timeout_ms / 1000
        input_sels = ['div[contenteditable="true"]', "textarea"] + DM_INPUT_SCOPED_SELS
        while time.monotonic() < deadline:
            if await self._detect_login_modal():
                raise RuntimeError("点击私信后弹出登录/验证窗口：请在 Chrome 中完成登录后重试")
            # 检查是否跳转到了 IM 页面（独立页形态）
            url = self.page.url or ""
            if "/im/" in url or "/chat/" in url:
                print(f"  ✅ 已跳转到 IM/聊天页面: {url}")
                await self.page.wait_for_timeout(2000)
            for sel in input_sels:
                try:
                    el = self.page.locator(sel).first
                    if await el.count() > 0 and await el.is_visible():
                        print(f"  ✅ 聊天输入框已出现 ({sel})")
                        await self.page.wait_for_timeout(1000)
                        return
                except Exception:
                    pass
            await self.page.wait_for_timeout(500)
        raise RuntimeError(
            f"聊天面板 {timeout_ms}ms 内未出现：请切到 Chrome 确认私信窗口状态（可能被拦截或该用户关闭了私信）")

    async def _detect_login_modal(self) -> bool:
        for sel in DM_LOGIN_MODAL_SELS:
            try:
                loc = self.page.locator(sel).first
                if await loc.count() > 0 and await loc.is_visible():
                    return True
            except Exception:
                pass
        return False

    async def _fill_dm_box(self, text: str) -> None:
        """填写私信输入框（策略链；自检失败继续下一策略，全部失败则 raise）。只填不送。"""
        # 策略 1: IM 相关选择器（语义最准，优先于"最后一个 contenteditable"）
        for sel in DM_INPUT_SCOPED_SELS:
            try:
                box = self.page.locator(sel).first
                if await box.count() > 0 and await box.is_visible():
                    await box.click()
                    await self.page.wait_for_timeout(300)
                    await box.fill(text)
                    print(f"  ✅ 已填入 ({sel})")
                    if await self._self_check_dm_filled(box, text):
                        return
            except Exception:
                pass
        # 策略 2: 最后一个 contenteditable（聊天输入框通常是页面上最后一个）
        try:
            all_boxes = self.page.locator('div[contenteditable="true"]')
            count = await all_boxes.count()
            if count > 0:
                box = all_boxes.last
                await box.click()
                await self.page.wait_for_timeout(300)
                await box.fill(text)
                print(f"  ✅ 已填入 (第 {count} 个 contenteditable)")
                if await self._self_check_dm_filled(box, text):
                    return
        except Exception:
            pass
        # 策略 3: textarea 兜底
        try:
            ta = self.page.locator("textarea").last
            if await ta.count() > 0 and await ta.is_visible():
                await ta.click()
                await self.page.wait_for_timeout(300)
                await ta.fill(text)
                print("  ✅ 已填入 (textarea)")
                if await self._self_check_dm_filled(ta, text):
                    return
        except Exception:
            pass
        # 策略 4: 键盘逐字输入兜底（受控组件可能吞 fill，逐字输入更接近真人）
        try:
            all_boxes = self.page.locator('div[contenteditable="true"]')
            count = await all_boxes.count()
            if count > 0:
                box = all_boxes.last
                print("  使用键盘逐字输入...")
                await self._human_type(text, box=box)
                if await self._self_check_dm_filled(box, text):
                    return
        except Exception:
            pass
        raise RuntimeError("私信内容未能填入输入框：所有填写策略均失败，请切到 Chrome 查看页面状态")

    async def _self_check_dm_filled(self, box: Locator, text: str) -> bool:
        """填框后自检：读回 inner_text 与目标文本 normalize 比对。成功 True，失败打警告返回 False。"""
        try:
            actual = await box.inner_text()
        except Exception as e:
            print(f"  ⚠️ 自检失败（无法读取输入框内容）: {e}")
            return False
        if _normalize_text(actual) != _normalize_text(text):
            print(f"  ⚠️ 自检失败：输入框内容与目标不一致（实际: {actual!r}），尝试下一策略")
            return False
        print("  ✅ 自检通过：私信已填入输入框（未发送）")
        return True

    async def submit_dm(self) -> None:
        """提交私信：Enter → 发送按钮 → 全局 Enter（移植自 DouyinService.submitDm，Java 1338-1401）。"""
        await self.page.wait_for_timeout(800)
        print("🚀 提交私信...")
        # 策略 1: 最后一个 contenteditable 里按 Enter（抖音 IM 最可靠）
        try:
            all_boxes = self.page.locator('div[contenteditable="true"]')
            count = await all_boxes.count()
            if count > 0:
                await all_boxes.last.press("Enter")
                print(f"  ✅ Enter 已发送 (第 {count} 个 contenteditable)")
                await self.page.wait_for_timeout(2000)
                return
        except Exception:
            pass
        # 策略 2: IM 面板内的 Enter
        for sel in DM_INPUT_SCOPED_SELS:
            try:
                boxes = self.page.locator(sel)
                if await boxes.count() > 0:
                    await boxes.last.press("Enter")
                    print(f"  ✅ Enter 已发送 ({sel})")
                    await self.page.wait_for_timeout(2000)
                    return
            except Exception:
                pass
        # 策略 3: "发送"按钮（容器内原生点击）
        for container in ['[class*="chat-input"]', '[class*="im-panel"]',
                          '[class*="im-footer"]', '[class*="message-editor"]']:
            for t in ["发送"]:
                try:
                    sel = (f'{container} button:text-is("{t}"), {container} span:text-is("{t}"), '
                           f'{container} [class*="send"], {container} [class*="submit"]')
                    btn = self.page.locator(sel).first
                    if await btn.count() > 0 and await btn.is_visible() and await btn.is_enabled():
                        await btn.evaluate("el => el.click()")  # 原生 click，不走 Playwright 事件链
                        print(f"  ✅ 原生点击发送 ({container}: {t})")
                        await self.page.wait_for_timeout(2000)
                        return
                except Exception:
                    pass
        # 策略 4: 全局 Enter 兜底
        print("  ⌨️ 全局 Enter 兜底")
        await self.page.keyboard.press("Enter")
        await self.page.wait_for_timeout(2000)

    async def _self_check_dm_sent(self, text: str) -> None:
        """发送后自检：私信文本是否已出现在会话列表/聊天记录（含最新消息预览）。

        只提示不 raise：发送不可撤回，自检失败请人工确认（可能被风控拦截或弹验证码）。
        2026-08-24 真机验证：消息发送后出现在会话列表 `conversationConversationItem*` 的
        预览文本（PRE.ConversationItemHinttextBox）里；JS 全页文本搜索为最终兜底。
        """
        msg_sels = [
            '[class*="message-item"]',
            '[class*="chat-item"]',
            '[class*="messageItem"]',
            '[class*="chat-message"]',
            'div[class*="ConversationItem"]',
            'pre[class*="textBox"]',
        ]
        needle = _normalize_text(text)
        # 第一轮：has_text 直接匹配（会话列表条目/消息气泡）
        for sel in msg_sels:
            try:
                items = self.page.locator(sel).filter(has_text=text)
                if await items.count() > 0:
                    print("✅ 自检通过：私信已出现在会话列表/聊天记录")
                    return
            except Exception:
                pass
        # 第二轮：JS 全页文本 normalize 搜索（排除输入框本身，防止"没发出但框里还有字"的假阳性）
        try:
            found = await self.page.evaluate(_FIND_DM_TEXT_JS, needle)
            if found:
                print("✅ 自检通过：私信已出现在会话列表/聊天记录")
                return
        except Exception:
            pass
        print("⚠️ 自检未在聊天记录找到该私信：请切到 Chrome 人工确认（可能被风控拦截或弹验证码）")


# ═══ CLI ═══

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抖音评论/私信机器人（测试版）：API 抓评论（含 userId）+ CDP 发评论/回复/私信填框")
    parser.add_argument("video_url", nargs="?", default="", help="抖音视频链接（支持完整链接/短链接/纯ID；--dm-sec-uid 模式可省略）")
    parser.add_argument("--text", default="", help="要发布的评论/回复内容，或要填入的私信内容")
    parser.add_argument("--reply-to", default="", help="回复目标：评论序号(#3) / sec_uid / 昵称；不填则发新评论")
    parser.add_argument("--dm-to", default="", help="私信目标：评论序号(#3) / sec_uid / 昵称（先抓视频评论解析；只填输入框，不发送）")
    parser.add_argument("--dm-sec-uid", default="", help="私信目标 sec_uid（直接打开主页，不抓评论；只填输入框，不发送）")
    parser.add_argument("--dm-send", action="store_true", help="私信填入后继续发送（默认只填不送；发送前需人工确认）")
    parser.add_argument("--max-comments", type=int, default=30, help="API 最多抓取评论数（默认30）")
    parser.add_argument("--dry-run", action="store_true", help="只读模式：仅抓取并打印评论，不执行任何写操作")
    parser.add_argument("--yes", action="store_true", help="跳过发布前的人工确认")
    parser.add_argument("--cdp-port", type=int, default=None, help="CDP 调试端口（默认取 config.CDP_DEBUG_PORT）")
    return parser.parse_args()


def _validate_dm_args(args: argparse.Namespace) -> Optional[str]:
    """校验私信相关参数组合，返回错误文案（None 表示合法）。"""
    if args.dm_to and args.dm_sec_uid:
        return "--dm-to 与 --dm-sec-uid 互斥，只能二选一"
    if (args.dm_to or args.dm_sec_uid) and args.reply_to:
        return "--dm-to / --dm-sec-uid 与 --reply-to 互斥（私信和回复是两种操作）"
    if (args.dm_to or args.dm_sec_uid) and not args.text:
        return "--dm-to / --dm-sec-uid 必须配合 --text 使用（要填入的私信内容）"
    if args.dm_to and not args.video_url:
        return "--dm-to 需要视频链接（先抓评论解析目标用户）"
    if not args.video_url and not args.dm_sec_uid:
        return "缺少视频链接（除非使用 --dm-sec-uid 直接指定私信目标）"
    if args.dm_send and not (args.dm_to or args.dm_sec_uid):
        return "--dm-send 只在私信模式下使用（配合 --dm-to / --dm-sec-uid）"
    return None


def _print_comments(comments: List[Dict[str, Any]]) -> None:
    print("\n===== 评论列表（sec_uid 供定位回复，后续可交给 LLM 识别意向客户） =====")
    for i, c in enumerate(comments, 1):
        t = datetime.fromtimestamp(c["create_time"]).strftime("%m-%d %H:%M") if c.get("create_time") else ""
        print(f"#{i} 昵称={c['nickname']}  sec_uid={c['sec_uid']}  "
              f"赞={c['digg_count']}  回复数={c['reply_comment_total']}  时间={t}")
        print(f"    {c['text']}")


def _resolve_target(comments: List[Dict[str, Any]], target: str) -> Dict[str, Any]:
    """把 --reply-to 参数解析成评论字典（支持 #序号 / sec_uid / 昵称）。

    抖音 sec_uid 固定以 MS4wLjAB 开头：直接用作定位目标，不依赖抓取列表
    （评论热排序会变，目标可能不在本次抓取的列表里，2026-08-24 真机踩过）。
    """
    target = (target or "").strip()
    if target.startswith("#"):
        try:
            idx = int(target[1:]) - 1
        except ValueError:
            raise RuntimeError(f"序号格式错误: {target!r}（应为 #1、#2 ...）")
        if not (0 <= idx < len(comments)):
            raise RuntimeError(f"序号超出范围（共 {len(comments)} 条）")
        c = comments[idx]
    elif target.startswith("MS4wLjAB"):
        c = {"sec_uid": target, "nickname": target, "text": ""}
    else:
        matches = [c for c in comments
                   if c["sec_uid"] == target or c["nickname"] == target
                   or (c["nickname"] and target in c["nickname"])]
        if not matches:
            raise RuntimeError(f"已抓取的评论中没有找到用户 {target!r}（支持 sec_uid 精确匹配 / 昵称匹配）")
        c = matches[0]
    if not c["sec_uid"]:
        raise RuntimeError("该评论没有 sec_uid（可能是私密账号），无法在评论区定位回复")
    return c


async def _main() -> None:
    args = _parse_args()
    error = _validate_dm_args(args)
    if error:
        print(f"错误: {error}")
        return
    if args.reply_to and not args.text:
        print("错误: --reply-to 必须配合 --text 使用")
        return
    if args.cdp_port:
        config.CDP_DEBUG_PORT = args.cdp_port

    bot = DouyinCommentBot()
    keep_page_open = False  # 私信只填不送成功后保持页面打开，供用户肉眼确认
    try:
        await bot.setup()

        # ── 私信模式：只填输入框，不发送 ──
        if args.dm_sec_uid or args.dm_to:
            if args.dm_sec_uid:
                dm_sec_uid, dm_nickname = args.dm_sec_uid, ""
            else:
                comments = await bot.fetch_comments_by_api(args.video_url, args.max_comments)
                _print_comments(comments)
                c = _resolve_target(comments, args.dm_to)
                dm_sec_uid, dm_nickname = c["sec_uid"], c["nickname"]

            who = f"@{dm_nickname} " if dm_nickname else ""
            if args.dry_run:
                action = "发送私信" if args.dm_send else "填入私信（不发送）"
                print(f"\n[dry-run] 将向 {who}({dm_sec_uid}) {action}: {args.text}")
                return
            if not args.yes:
                action = "发送私信" if args.dm_send else "填入私信内容（不发送）"
                ans = input(f"\n确认向 {who}({dm_sec_uid}) {action}？内容: {args.text}\n输入 y 继续: ").strip().lower()
                if ans != "y":
                    print("已取消。")
                    return

            # 私信流程进入后保持页面打开：无论成败用户都能在 Chrome 看到现场（填框确认/验证码/登录窗等）
            keep_page_open = True
            await bot.open_user_profile(dm_sec_uid)
            await bot.fill_dm_input(args.text)
            if args.dm_send:
                await bot.submit_dm()
                await bot._self_check_dm_sent(args.text)
                print("✅ 私信已提交。页面保持打开，请到 Chrome 确认消息已发出。")
            return

        # ── 评论模式：先抓评论 ──
        comments = await bot.fetch_comments_by_api(args.video_url, args.max_comments)
        _print_comments(comments)
        if not args.text:
            return
        reply_c = _resolve_target(comments, args.reply_to) if args.reply_to else None

        if args.dry_run:
            if reply_c:
                idx = comments.index(reply_c) + 1 if reply_c in comments else "sec_uid直连"
                print(f"\n[dry-run] 将回复 #{idx} "
                      f"@{reply_c['nickname']}({reply_c['sec_uid']}): {args.text}")
            else:
                print(f"\n[dry-run] 将发布新评论: {args.text}")
            return

        # 写操作前人工确认（对真实账号的不可逆操作）
        if not args.yes:
            action = f"回复 @{reply_c['nickname']}({reply_c['sec_uid']}) 的评论" if reply_c else "发布新评论"
            ans = input(f"\n确认{action}？内容: {args.text}\n输入 y 继续: ").strip().lower()
            if ans != "y":
                print("已取消。")
                return

        await bot.open_video_page(args.video_url)
        if reply_c:
            # 序号消歧：目标是该用户在抓取列表中的第几条评论（sec_uid 直连模式不在列表 → 第 1 条）
            nth = 0
            if reply_c in comments:
                nth = sum(1 for c in comments[: comments.index(reply_c) + 1]
                          if c.get("sec_uid") == reply_c["sec_uid"])
            await bot.reply_to_comment(reply_c["sec_uid"], args.text, nth=nth)
        else:
            await bot.post_comment(args.text)
    except Exception as e:
        print(f"\n[错误] {type(e).__name__}: {e}")
        if keep_page_open:
            print("（页面保持打开，请切到 Chrome 查看当前状态）")
        raise SystemExit(1)
    finally:
        await bot.close(close_page=not keep_page_open)


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
