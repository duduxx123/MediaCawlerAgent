# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1

"""在快手视频下直接发布一级评论的 Agent 工具。"""

from __future__ import annotations

import asyncio
import atexit
import json
import re
import time
from typing import Any, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

import config
from media_platform.kuaishou.comment_bot import (
    KuaishouCommentBot,
    KuaishouCommentError,
    KuaishouNotLoggedInError,
)


COMMENT_COOLDOWN_SECONDS = 60
_CONNECTION_ERROR_RE = re.compile(
    r"target closed|connection closed|connection reset|connection refused|websocket|"
    r"has been closed|browser has been closed|context has been closed|CDP 连接失败|"
    r"connect call failed|remote protocol error",
    re.IGNORECASE,
)
_HINT_EXISTING = (
    "请确认 Chrome 已开启远程调试且已登录快手；首次读取登录 Cookie 时需在 Chrome "
    "授权弹窗点击『允许』。Cookie 只保存在当前 Agent 进程内，进程退出后清除。"
)
_HINT_SELF = (
    "客户端会自动启动独立快手浏览器，不需要连接本机正在使用的 Chrome，也不需要授权 9222。"
    "若未登录，请在客户端打开的浏览器中登录快手后重试；Cookie 只缓存在当前 Agent 进程内。"
)


def _hint() -> str:
    return _HINT_EXISTING if config.CDP_CONNECT_EXISTING else _HINT_SELF


_bot: Optional[KuaishouCommentBot] = None
_last_attempt_by_target: dict[str, float] = {}
_bot_lock = asyncio.Lock()


def _error_json(message: str, hint: str = "", **extra: Any) -> str:
    result: dict[str, Any] = {"ok": False, "message": str(message)[:500], **extra}
    if hint:
        result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)


def _is_connection_error(exc: BaseException) -> bool:
    return bool(_CONNECTION_ERROR_RE.search(str(exc)))


async def _get_bot() -> KuaishouCommentBot:
    global _bot
    if _bot is not None and not await _bot.check_alive():
        await _reset_bot()
    if _bot is None:
        candidate = KuaishouCommentBot()
        await candidate.setup()
        _bot = candidate
    return _bot


async def _reset_bot() -> None:
    global _bot
    bot, _bot = _bot, None
    if bot is not None:
        try:
            await asyncio.wait_for(bot.close(), timeout=20)
        except Exception:
            pass


async def cleanup_bot() -> None:
    """清空进程内 Cookie/限流状态并释放异常残留的 CDP 连接。"""
    global _bot
    bot, _bot = _bot, None
    _last_attempt_by_target.clear()
    if bot is not None:
        try:
            await asyncio.wait_for(bot.close(), timeout=20)
        except Exception:
            pass


def _cleanup_bot_at_exit() -> None:
    bot = _bot
    if bot is None:
        return
    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(asyncio.wait_for(bot.close(), timeout=20))
        loop.close()
    except Exception:
        pass


atexit.register(_cleanup_bot_at_exit)


class PostKuaishouCommentArgs(BaseModel):
    target: str = Field(
        min_length=6,
        max_length=2000,
        description=(
            "快手作品完整链接、分享短链或 photo_id，例如 "
            "https://www.kuaishou.com/short-video/3x... 或 https://www.kuaishou.com/f/..."
        ),
    )
    content: str = Field(
        min_length=1,
        max_length=500,
        description="要在视频下真实发布的一级评论正文",
    )


@tool(args_schema=PostKuaishouCommentArgs)
async def post_kuaishou_comment(target: str, content: str) -> str:
    """在快手作品下直接发布一条一级评论（真实发布，不需要二次确认）。

    此工具不回复或定向提及某条已有评论。它会先解析作品并检查评论权限，然后提交一次；
    写接口返回 result=1 且带 commentId 时表示发布成功。评论读取列表可能延迟同步，
    self_checked=false 不代表发布失败，仍禁止自动重试。
    """
    content = content.strip()
    if not content:
        return _error_json("评论内容不能为空", sent_unknown=False)
    try:
        async with _bot_lock:
            bot = await _get_bot()
            resolved = await bot.resolve_photo_target(target)
            cooldown_key = resolved.photo_id
            now = time.monotonic()
            remaining = COMMENT_COOLDOWN_SECONDS - (
                now - _last_attempt_by_target.get(cooldown_key, 0.0)
            )
            if remaining > 0:
                return _error_json(
                    f"同一作品评论冷却中，请至少等待 {int(remaining) + 1} 秒",
                    retry_after_seconds=int(remaining) + 1,
                    sent_unknown=False,
                )

            # 从这里开始是真实写操作；无论响应是否完整，都禁止自动重复提交。
            _last_attempt_by_target[cooldown_key] = time.monotonic()
            result = await bot.submit_comment(resolved, content)
        return json.dumps(result, ensure_ascii=False)
    except ValueError as exc:
        return _error_json(str(exc), sent_unknown=False)
    except KuaishouNotLoggedInError as exc:
        await _reset_bot()
        return _error_json(str(exc), _hint(), sent_unknown=False)
    except KuaishouCommentError as exc:
        if _is_connection_error(exc):
            await _reset_bot()
        hint = (
            "触发快手风控/验证码：请在网页端人工检查，不要自动重试。"
            if exc.need_captcha
            else "请先在快手网页评论区核对发布状态；状态不确定时不要自动重试。"
        )
        return _error_json(
            str(exc),
            hint,
            sent_unknown=exc.sent_unknown,
            need_captcha=exc.need_captcha,
        )
    except Exception as exc:
        if _is_connection_error(exc):
            await _reset_bot()
        return _error_json(
            f"{type(exc).__name__}: {exc}",
            "发送状态可能不确定，请先在快手网页评论区核对，禁止自动重试。",
            sent_unknown=True,
        )
