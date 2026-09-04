# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/tools/bili_comment_tools.py
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
B站发评论获客工具（AI 获客）：
  post_bilibili_comment —— 在 B站视频/动态下发一条评论或楼中楼回复（真实发布，纯 API）

底层复用 media_platform/bilibili/comment_bot.py 的 BilibiliCommentBot：
  - CDP 连接用户正在运行的 Chrome（DevToolsActivePort 文件地址直连，复用登录态），
    只从浏览器上下文读 cookie（bili_jct 当 csrf），发评论走 /x/v2/reply/add API
  - 单例懒加载 + 全局限流锁
  - 工具函数内捕获全部异常并返回紧凑中文 JSON（langgraph 1.x 工具抛异常会击穿 agent）
"""

import asyncio
import atexit
import json
import re
from typing import Any, Dict, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

import config
from media_platform.bilibili.comment_bot import (
    BilibiliCommentBot,
    BilibiliCommentError,
    NotLoggedInError,
)

# 单例 bot 与串行锁：CDP 连接是共享资源，同一时刻只允许一个操作
_bot: Optional[BilibiliCommentBot] = None
_bot_lock = asyncio.Lock()

# 连接级错误特征：只有这类错误才丢弃 bot 重建（重建 = 新 CDP 连接 = 用户又要点一次「允许」）。
# 业务级错误（风控/未登录/目标解析失败等）连接还是好的，保留复用，不折腾用户。
# 注意：不把裸 timeout 列入——业务请求超时与 CDP 存活无关，重建只会让用户白点一次「允许」。
_CONNECTION_ERROR_RE = re.compile(
    r"target closed|connection closed|connection reset|connection refused|websocket|"
    r"has been closed|browser has been closed|context has been closed|CDP 连接失败|"
    r"connect call failed|all connection attempts failed|remote protocol error",
    re.IGNORECASE,
)


def _is_connection_error(e: BaseException) -> bool:
    return bool(_CONNECTION_ERROR_RE.search(str(e)))


_HINT_BILI = ("请确认用户已运行开启远程调试的 Chrome 且已登录 B站（cookie 需含 bili_jct/SESSDATA）；"
              "当前进程尚无内存登录态或内存登录态失效时，请在 Chrome 弹窗中点击『允许』。若提示风控/验证码/发布频率受限：不要自动重试，"
              "建议放缓发布频率、更换内容，或请用户在 B站网页端手动完成验证。")
_HINT_BILI_SELF = ("专用浏览器已自动拉起（停在 B站首页，可在该窗口直接登录）。"
                   "首次登录成功后仅在当前 Agent 进程内复用，进程退出后下次需重新授权。"
                   "若提示 B站未登录：也可先让智能体爬取一次 B站（会弹出浏览器扫码）。"
                   "若提示风控/验证码/发布频率受限：不要自动重试，建议放缓发布频率、"
                   "更换内容，或请用户在 B站网页端手动完成验证。")


def _hint_bili() -> str:
    """按浏览器模式选提示：连用户 Chrome（开发态）vs 拉起专用浏览器（exe 方案A）。"""
    return _HINT_BILI if config.CDP_CONNECT_EXISTING else _HINT_BILI_SELF


async def _get_bot() -> BilibiliCommentBot:
    """懒加载单例 bot；首次读取 Cookie 后只复用当前进程的内存客户端。"""
    global _bot
    if _bot is not None:
        alive = False
        try:
            alive = await _bot.check_alive()
        except Exception:
            alive = False
        if not alive:
            # 内存登录态已被清空：丢弃旧 bot，下次调用重新从 Chrome 读取一次
            old, _bot = _bot, None
            try:
                await asyncio.wait_for(old.close(), timeout=20)
            except Exception:
                pass
    if _bot is None:
        bot = BilibiliCommentBot()
        await bot.setup()
        _bot = bot
    return _bot


async def _reset_bot() -> None:
    """连接类异常后丢弃旧 bot，下次调用自动重连。"""
    global _bot
    bot, _bot = _bot, None
    if bot is not None:
        try:
            await asyncio.wait_for(bot.close(), timeout=20)
        except Exception:
            pass


async def cleanup_bot() -> None:
    """Agent 进程退出前清空单例 bot 的内存 Cookie，并关闭异常残留的 CDP。"""
    global _bot
    bot, _bot = _bot, None
    if bot is None:
        return
    try:
        await asyncio.wait_for(bot.close(close_page=False), timeout=20)
    except Exception:
        pass


def _cleanup_bot_at_exit() -> None:
    """进程退出兜底：尽力关闭单例 bot（新事件循环，失败无害）。"""
    bot = _bot
    if bot is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(asyncio.wait_for(bot.close(close_page=False), timeout=20))
        loop.close()
    except Exception:
        pass


atexit.register(_cleanup_bot_at_exit)


def _error_json(message: str, hint: str = "") -> str:
    out: Dict[str, Any] = {"ok": False, "message": str(message)[:300]}
    if hint:
        out["hint"] = hint
    return json.dumps(out, ensure_ascii=False)


class PostBilibiliCommentArgs(BaseModel):
    """B站发评论的参数"""

    target_url: str = Field(description="B站视频链接（/video/BVxxx 或 /video/av123、裸 BV/av 号）或动态链接（t.bilibili.com/opus/…、…/dynamic/…）")
    content: str = Field(description="评论内容（建议不超过100字，语气自然合规）", max_length=500)
    root: Optional[int] = Field(default=None, description="楼中楼：根评论 rpid；不传=发顶层评论")
    parent: Optional[int] = Field(default=None, description="楼中楼：被回复评论 rpid；传 parent 必须同时传 root")


@tool(args_schema=PostBilibiliCommentArgs)
async def post_bilibili_comment(
    target_url: str,
    content: str,
    root: Optional[int] = None,
    parent: Optional[int] = None,
) -> str:
    """在 B站视频或动态（opus/dynamic 链接）下发布一条评论/回复（真实发布，API 直发，
登录态取自用户 Chrome 的 B站 cookie）。楼中楼回复需传 root（根评论 rpid），回复楼中楼内
某条评论再传 parent。发布前请确认内容合规；同一目标避免高频连发（有风控风险）。"""
    try:
        if parent is not None and root is None:
            return _error_json("参数错误：传 parent 时必须同时传 root")
        async with _bot_lock:
            bot = await _get_bot()
            result = await bot.post_comment(target_url, content, root=root, parent=parent)
        return json.dumps(result, ensure_ascii=False)
    except NotLoggedInError as e:
        # 登录态问题：连接是好的，保留 bot，用户登录后重试即可
        return _error_json(str(e), _hint_bili())
    except BilibiliCommentError as e:
        # 业务失败（含风控/验证码）：连接是好的，保留 bot
        if e.need_captcha:
            return _error_json(str(e), "触发风控/验证码：不要自动重试，建议放缓发布频率，或请用户在 B站网页端手动完成验证")
        return _error_json(str(e), _hint_bili())
    except Exception as e:
        if _is_connection_error(e):
            await _reset_bot()
        return _error_json(f"{type(e).__name__}: {e}", _hint_bili())
