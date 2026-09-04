# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/bilibili/comment_bot.py
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
B站发评论机器人（测试版）— 纯 API 发评论，浏览器只用来取登录 cookie。

职责拆分：
  - 发评论 / 楼中楼回复：POST /x/v2/reply/add（表单，无 wbi 签名——移植自
    D:\\bilibili-mcp-server 的 publishComment，实测可用）。
  - 目标解析：视频链接（BV 转 avid 走本项目 get_video_info）/ av 号直发 /
    动态链接（/opus/…、t.bilibili.com/… 走 web-dynamic detail 拿 comment_id）。
  - 登录态：首次通过 CDP 连接浏览器（已登录 B站）读取 cookie，随后立即断开
    CDP；cookie 仅保存在当前 Python 进程内存中，同一进程内复用，进程退出后
    自动释放。登录态失效（接口报 -101/-111）时才重新连接浏览器读取一次。

用法：
    # 视频下发一条新评论（发布前需输入 y 确认，--yes 可跳过）
    uv run python -m media_platform.bilibili.comment_bot <BV链接> --text "评论内容" --yes

    # av 链接 / 动态链接同样支持
    uv run python -m media_platform.bilibili.comment_bot <av链接> --text "评论内容"
    uv run python -m media_platform.bilibili.comment_bot <opus动态链接> --text "评论内容"

    # 楼中楼回复（root=根评论 rpid；回复楼中楼内某条评论再传 parent）
    uv run python -m media_platform.bilibili.comment_bot <链接> --text "回复内容" --root 123 --parent 456 --yes

前提：
    Chrome 需以 --remote-debugging-port=9222 启动且已登录 B站；
    或在 chrome://inspect/#remote-debugging 勾选"允许远程调试"。
"""

import argparse
import asyncio
import io
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

# Windows GBK 控制台下输出中文必需：强制 stdout/stderr 为 UTF-8
if sys.stdout and hasattr(sys.stdout, "buffer"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

from playwright.async_api import async_playwright, BrowserContext

import config
from media_platform.bilibili.client import BilibiliClient
from tools import utils
from tools.cdp_browser import CDPBrowserManager

# ─── 目标解析常量 ───

VIDEO_URL_RE = re.compile(r"/video/(BV[0-9A-Za-z]+|av\d+)")
DYNAMIC_URL_RE = re.compile(r"/(?:opus|dynamic)/(\d+)")
T_BILLI_SHORT_RE = re.compile(r"t\.bilibili\.com/(\d+)")  # t.bilibili.com 分享链接：路径即动态 id
BARE_BV_RE = re.compile(r"^(BV[0-9A-Za-z]+)$")
BARE_AV_RE = re.compile(r"^(?:av)?(\d+)$")  # 裸纯数字按视频 av 号解释
B23_SHORT_RE = re.compile(r"b23\.tv", re.IGNORECASE)

COMMENT_TYPE_NAMES = {1: "视频", 11: "图文动态", 12: "专栏", 17: "文字动态"}

VERIFY_ATTEMPTS = 3
VERIFY_INTERVAL_S = 3.0


class NotLoggedInError(RuntimeError):
    """cookie 中缺 bili_jct/SESSDATA（B站未登录或登录态失效）。"""


class BilibiliCommentError(RuntimeError):
    """发评论业务失败（含风控/验证码），need_captcha 标记是否被风控拦截。"""

    def __init__(self, message: str, need_captcha: bool = False, raw: Optional[Dict] = None):
        super().__init__(message)
        self.need_captcha = need_captcha
        self.raw = raw


@dataclass
class CommentTarget:
    """发评论目标：parse 阶段只有骨架（kind + id），resolve 阶段补全 type/oid/label。"""

    kind: str  # "video" | "dynamic"
    bvid: Optional[str] = None
    aid: Optional[int] = None
    dynamic_id: Optional[str] = None
    type: Optional[int] = None
    oid: Optional[int] = None
    oid_str: Optional[str] = None
    label: str = ""


def parse_comment_target_url(url: str) -> CommentTarget:
    """纯函数：URL → 骨架 CommentTarget（不含网络调用）。

    支持：/video/BVxxx、/video/av123、裸 BVxxx、裸 av123（裸纯数字按 av 解释）、
    /opus/123、/dynamic/456、t.bilibili.com/123。
    b23.tv 短链与无法识别的链接抛 ValueError（不自动展开短链，避免额外网络跳转）。
    """
    text = (url or "").strip()
    if not text:
        raise ValueError("目标链接为空")

    if B23_SHORT_RE.search(text):
        raise ValueError("检测到 b23.tv 短链接：请先在浏览器中展开为完整链接后重试")

    m = VIDEO_URL_RE.search(text)
    if m:
        token = m.group(1)
        if token.startswith("BV"):
            return CommentTarget(kind="video", bvid=token)
        return CommentTarget(kind="video", aid=int(token[2:]))

    m = DYNAMIC_URL_RE.search(text)
    if m:
        return CommentTarget(kind="dynamic", dynamic_id=m.group(1))

    m = T_BILLI_SHORT_RE.search(text)
    if m:
        return CommentTarget(kind="dynamic", dynamic_id=m.group(1))

    if BARE_BV_RE.match(text):
        return CommentTarget(kind="video", bvid=text)

    if BARE_AV_RE.match(text):
        return CommentTarget(kind="video", aid=int(BARE_AV_RE.match(text).group(1)))

    raise ValueError(f"无法识别的 B站链接：{text}（支持视频 /video/BVxxx、/video/av123 与动态 /opus/123、t.bilibili.com/123）")


class BilibiliCommentBot:
    """发评论（API）+ 登录态获取（CDP 读 cookie）的测试机器人。"""

    def __init__(self) -> None:
        self._playwright = None
        self._cdp_manager: Optional[CDPBrowserManager] = None
        self._browser_context: Optional[BrowserContext] = None
        self.client: Optional[BilibiliClient] = None
        self._csrf = ""
    # ─── 初始化：CDP 连接 + 构建 API 客户端 ───

    @staticmethod
    def _devtools_ws_url() -> Optional[str]:
        """读 Chrome 的 DevToolsActivePort 文件拿精确 ws 地址。

        Chrome 136+ 的现有浏览器调试不再暴露 /json/version，这个文件是官方指定入口。
        """
        import os
        from pathlib import Path

        local_app_data = os.environ.get("LOCALAPPDATA", "")
        dt_file = Path(local_app_data) / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
        try:
            if not dt_file.is_file():
                return None
            lines = dt_file.read_text(encoding="utf-8").strip().splitlines()
            if len(lines) < 2 or not lines[0].strip().isdigit():
                return None
            return f"ws://127.0.0.1:{lines[0].strip()}{lines[1].strip()}"
        except OSError:
            # Chrome 运行权限高于当前 Python 进程时，文件 stat/read 可能被拒绝；
            # 此时交给下面的端口直连回退，不要把它误报成 CDP 不可用。
            return None

    async def setup(self) -> None:
        """初始化：连接浏览器读取 cookie 到内存，读取完成后立即断开 CDP。

        只读 cookie 不开页面：本 bot 全部方法均不触发 wbi 签名（client 的
        playwright_page=None）。浏览器来源跟随 config.CDP_CONNECT_EXISTING：
        - True（开发态）：直连用户正在运行的 Chrome（DevToolsActivePort 优先），复用其登录态
        - False（exe 方案A）：拉起专用 profile 浏览器（与爬虫同 profile 目录，
          browser_data/cdp_bili_user_data_dir，复用扫码登录态），发布完成后随 close() 关闭
        """
        await self._refresh_memory_login()

    async def _refresh_memory_login(self) -> None:
        """从浏览器刷新内存 Cookie；无论成功失败都立即释放本次 CDP 连接。"""
        await self._connect_browser()
        try:
            cookie_str, cookie_dict = await self._read_cookies_from_browser()
            self._build_client_from_cookies(cookie_str, cookie_dict)
        finally:
            await self._disconnect_browser()

    async def _connect_browser(self) -> None:
        """连接浏览器（用户 Chrome 或 方案A 专用浏览器），只建上下文不建 client。"""
        if self._browser_context is not None:
            return
        print(f"[CDP 连接] 正在连接浏览器（调试端口 {config.CDP_DEBUG_PORT}）...")
        print("[CDP 连接] 注意：若 Chrome 弹出『远程调试授权』确认框，请点击允许，否则会连接超时")
        self._playwright = await async_playwright().start()
        self._cdp_manager = None

        if config.CDP_CONNECT_EXISTING:
            # 方式1（优先）：DevToolsActivePort 文件里的精确 ws 地址
            ws_url = self._devtools_ws_url()
            if ws_url:
                try:
                    print(f"[CDP 连接] 尝试 DevToolsActivePort 地址: {ws_url}")
                    cdp_browser = await self._playwright.chromium.connect_over_cdp(
                        ws_url, timeout=config.BROWSER_LAUNCH_TIMEOUT * 1000
                    )
                    if cdp_browser.contexts:
                        self._browser_context = cdp_browser.contexts[0]
                    else:
                        self._browser_context = await cdp_browser.new_context()
                    print("[CDP 连接] ✅ 已连接")
                except Exception as e:
                    # 文件存在但连不上：几乎都是 Chrome 的授权弹窗没点「允许」。快速失败给出明确指引。
                    raise RuntimeError(
                        f"CDP 连接失败（DevToolsActivePort 地址 {ws_url}）：{e}。"
                        "请确认 Chrome 以远程调试模式运行且已登录 B站，并在 Chrome 弹出的授权框点击『允许』后重试。"
                    ) from e

            # 方式2（回退）：端口文件不存在（Chrome 未暴露调试端口）时才走通用连接
            if self._browser_context is None:
                self._cdp_manager = CDPBrowserManager()
                self._browser_context = await self._cdp_manager.launch_and_connect(
                    self._playwright,
                    playwright_proxy=None,
                    user_agent=None,
                    headless=False,
                )
        else:
            # 方案A（客户端 exe）：拉起自己的专用浏览器，profile 目录与爬虫一致，
            # 复用爬虫扫码登录过的 B站登录态。manager 用 config.USER_DATA_DIR % config.PLATFORM
            # 拼 profile 目录，故临时把平台指到 bili。
            print("[CDP 连接] 拉起专用浏览器（复用爬虫的 B站登录态）...")
            saved_platform = config.PLATFORM
            try:
                config.PLATFORM = "bili"
                self._cdp_manager = CDPBrowserManager()
                self._browser_context = await self._cdp_manager.launch_and_connect(
                    self._playwright,
                    playwright_proxy=None,
                    user_agent=None,
                    headless=False,
                )
            finally:
                config.PLATFORM = saved_platform
            # 打开 bilibili.com：若 profile 未登录，用户可直接在该窗口登录（导航失败不影响取 cookie）
            try:
                page = await self._browser_context.new_page()
                await page.goto("https://www.bilibili.com", wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass

    def _build_client_from_cookies(self, cookie_str: str, cookie_dict: Dict) -> None:
        """用给定 cookie 构建/重建 API 客户端（bili_jct 当 csrf）。"""
        self.client = BilibiliClient(
            proxy=None,
            headers={
                "User-Agent": utils.get_user_agent(),
                "Cookie": cookie_str,
                "Origin": "https://www.bilibili.com",
                "Referer": "https://www.bilibili.com/",
            },
            playwright_page=None,  # 本 bot 所有方法不触发 wbi，page 传 None 安全
            cookie_dict=cookie_dict,
        )
        self._csrf = cookie_dict.get("bili_jct", "")

    async def _read_cookies_from_browser(self) -> Tuple[str, Dict]:
        """从已连接的浏览器上下文读 B站 cookie，缺登录态抛 NotLoggedInError。"""
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            self._browser_context, urls=["https://www.bilibili.com"]
        )
        if not (cookie_dict.get("bili_jct") and cookie_dict.get("SESSDATA")):
            raise NotLoggedInError(
                "未检测到 B站登录态（cookie 需含 bili_jct/SESSDATA），请先在浏览器中登录 B站后重试"
            )
        return cookie_str, cookie_dict

    # ─── 生命周期 ───

    async def check_alive(self) -> bool:
        """内存登录态探针；正常状态下不保留、也不探测 CDP 连接。"""
        return self.client is not None and bool(self._csrf)

    async def _disconnect_browser(self) -> None:
        """只断开本次 CDP/Playwright 连接，保留 client 中的内存 Cookie。"""
        if self._cdp_manager is not None and not config.CDP_CONNECT_EXISTING:
            try:
                await asyncio.wait_for(self._cdp_manager.cleanup(), timeout=20)
            except Exception:
                pass
        if self._playwright:
            try:
                # 崩溃路径下 playwright.stop() 在 Windows 会挂死进程，必须限时
                await asyncio.wait_for(self._playwright.stop(), timeout=15)
            except Exception:
                pass
        self._browser_context = None
        self._cdp_manager = None
        self._playwright = None

    async def close(self, close_page: bool = True) -> None:
        """清理 CDP（如有）并清空仅存在于当前进程内的 Cookie。"""
        await self._disconnect_browser()
        # Cookie 只存在于 client、headers 和 csrf 等内存对象中；关闭时显式清空引用。
        self.client = None
        self._csrf = ""

    # ─── 发评论链路 ───

    async def resolve_target(self, target: str) -> CommentTarget:
        """骨架解析 + 网络补全：视频 BV 转 avid（拿标题）、动态拿 comment_type/comment_id。"""
        t = parse_comment_target_url(target)

        if t.kind == "video" and t.bvid:
            data = await self.client.get_video_info(bvid=t.bvid)
            view = data.get("View") or {}
            aid = view.get("aid")
            if not aid:
                raise BilibiliCommentError(f"视频 {t.bvid} 解析失败：无法获取 avid（视频可能不存在）")
            title = str(view.get("title") or "").strip()
            t.aid = int(aid)
            t.type = 1
            t.oid = t.aid
            t.oid_str = str(t.aid)
            t.label = f"{t.bvid}" + (f"《{title[:60]}》" if title else "")
        elif t.kind == "video":
            t.type = 1
            t.oid = t.aid
            t.oid_str = str(t.aid)
            t.label = f"av{t.aid}"
        else:
            data = await self.client.get_dynamic_detail(t.dynamic_id)
            item = data.get("item") or ((data.get("items") or [{}])[0] if isinstance(data, dict) else {})
            basic = item.get("basic") or {}
            comment_type = basic.get("comment_type")
            comment_id = basic.get("comment_id_str") or basic.get("comment_id")
            if not comment_type or not comment_id:
                raise BilibiliCommentError(f"动态 {t.dynamic_id} 解析失败：无法获取评论目标（动态可能不存在）")
            t.type = int(comment_type)
            t.oid = int(comment_id)
            t.oid_str = str(comment_id)
            desc = ""
            try:
                desc = ((item.get("modules") or {}).get("module_dynamic") or {}).get("desc") or {}
                desc = (desc.get("text") or "")[:60]
            except Exception:
                desc = ""
            t.label = f"{COMMENT_TYPE_NAMES.get(t.type, '动态')} {t.dynamic_id}" + (f"《{desc}》" if desc else "")

        print(f"[目标] type={t.type}({COMMENT_TYPE_NAMES.get(t.type, '未知')}) oid={t.oid_str} label={t.label}")
        return t

    async def post_comment(
        self,
        target: str,
        content: str,
        root: Optional[int] = None,
        parent: Optional[int] = None,
    ) -> Dict[str, Any]:
        """发评论/楼中楼回复，返回结构化结果。业务失败 raise BilibiliCommentError（工具层统一捕获）。

        cookie 策略：首次调用从已登录浏览器读取并仅保存在当前进程内存中；
        同一进程复用内存登录态。接口返回 -101（未登录）/-111（csrf 失效）
        时清空内存态、重连浏览器刷新 cookie 后重试一次。
        """
        if self.client is None:
            # 兜底：没有内存 Cookie 时，连接浏览器读取一次后立即断开。
            await self._refresh_memory_login()

        t, resp = await self._post_once(target, content, root, parent)
        code = resp.get("code")

        if code in (-101, -111):
            # 登录态过期 / csrf 轮换：清空当前内存态 → 重连浏览器刷新 → 重试一次
            print(f"[Cookie] 内存登录态已失效（code={code}），重连浏览器刷新...")
            self.client = None
            self._csrf = ""
            await self._refresh_memory_login()
            t, resp = await self._post_once(target, content, root, parent)
            code = resp.get("code")

        data = resp.get("data") or {}

        if code != 0:
            # -101 未登录 / -111 csrf 失效 / -352 风控 / 12002 评论区关闭 等
            msg = resp.get("message") or f"code={code}"
            raise BilibiliCommentError(f"发布失败：{msg}", raw=resp)

        success_action = data.get("success_action", 0)
        if success_action != 0:
            need_captcha = bool(data.get("need_captcha"))
            toast = data.get("success_toast") or data.get("dialog_str") or ""
            message = f"发布未成功（success_action={success_action}）" + (f"：{toast}" if toast else "")
            raise BilibiliCommentError(message, need_captcha=need_captcha, raw=resp)

        rpid = data.get("rpid")
        rpid_str = str(data.get("rpid_str") or rpid or "")
        verified = await self._verify_published(t.type, t.oid, rpid, content)
        return {
            "ok": True,
            "message": ("评论已发布（自检可见）" if verified
                        else "评论已提交，但自检未立即看到（可能审核中或列表刷新慢），请到 B站页面确认"),
            "rpid": rpid,
            "rpid_str": rpid_str,
            "type": t.type,
            "oid": t.oid,
            "oid_str": t.oid_str,
            "target_label": t.label,
            "verified": verified,
            "need_captcha": False,
        }

    async def _post_once(
        self, target: str, content: str, root: Optional[int], parent: Optional[int]
    ) -> Tuple[CommentTarget, Dict]:
        """一次发布尝试：解析目标 + 调发评论接口，返回 (目标, 原始响应 JSON)。"""
        t = await self.resolve_target(target)
        resp = await self.client.post_comment_reply(
            type=t.type, oid=t.oid, message=content, csrf=self._csrf, root=root, parent=parent
        )
        return t, resp

    async def _verify_published(self, type_: int, oid: int, rpid: Any, content: str) -> bool:
        """发后自检：轮询非 wbi 评论列表按 rpid 精确匹配（兜底按内容匹配）。

        best-effort：接口异常只记日志返回 False，不影响 ok=True（发布不可逆）。
        """
        for attempt in range(VERIFY_ATTEMPTS):
            try:
                data = await self.client.get_comment_list(type_, oid, sort=0, pn=1, ps=20)
                replies = data.get("replies") or []
                for r in replies:
                    if str(r.get("rpid")) == str(rpid):
                        return True
                    if ((r.get("content") or {}).get("message") or "") == content:
                        return True
            except Exception as e:
                utils.logger.warning(f"[BilibiliCommentBot] 自检第 {attempt + 1} 次失败（忽略）: {e}")
            if attempt < VERIFY_ATTEMPTS - 1:
                await asyncio.sleep(VERIFY_INTERVAL_S)
        return False


# ─── CLI（真机手动验证用） ───


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="B站发评论机器人（测试版）：CDP 连接已登录 Chrome，纯 API 发评论/楼中楼回复"
    )
    parser.add_argument(
        "target",
        help="B站视频链接（/video/BVxxx、/video/av123、裸 BV/av 号）或动态链接（/opus/123、t.bilibili.com/123）",
    )
    parser.add_argument("--text", required=True, help="评论内容")
    parser.add_argument("--root", type=int, default=None, help="楼中楼：根评论 rpid")
    parser.add_argument("--parent", type=int, default=None, help="楼中楼：被回复评论 rpid（需同时传 --root）")
    parser.add_argument("--yes", action="store_true", help="跳过发布前人工确认")
    parser.add_argument("--cdp-port", type=int, default=None, help="CDP 调试端口（默认读配置）")
    return parser.parse_args()


async def _main() -> None:
    args = _parse_args()
    if args.parent is not None and args.root is None:
        print("错误: --parent 必须配合 --root 使用")
        return
    if args.cdp_port:
        config.CDP_DEBUG_PORT = args.cdp_port

    bot = BilibiliCommentBot()
    try:
        await bot.setup()
        target = await bot.resolve_target(args.target)

        # 写操作前人工确认（对真实账号的不可逆操作）
        if not args.yes:
            action = f"楼中楼回复（root={args.root}, parent={args.parent}）" if args.root else "发布新评论"
            ans = input(f"\n确认在 {target.label} 下{action}？内容: {args.text}\n输入 y 继续: ").strip().lower()
            if ans != "y":
                print("已取消。")
                return

        result = await bot.post_comment(args.target, args.text, root=args.root, parent=args.parent)
        print(f"\n[结果] {result}")
    except Exception as e:
        print(f"\n[错误] {type(e).__name__}: {e}")
        raise SystemExit(1)
    finally:
        await bot.close()


def main() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    main()
