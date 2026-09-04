# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1

"""小红书私信 Agent 工具：先准备草稿，再凭一次性 draft_id 确认发送。"""

from __future__ import annotations

import asyncio
import atexit
import json
import re
import secrets
import time
from dataclasses import dataclass
from typing import Any, Optional

from langchain_core.tools import tool
from pydantic import BaseModel, Field

import config
from media_platform.xhs.dm_bot import XiaohongshuDmBot


DRAFT_TTL_SECONDS = 10 * 60
DM_COOLDOWN_SECONDS = 60
_CONNECTION_ERROR_RE = re.compile(
    r"target closed|connection closed|connection reset|connection refused|websocket|"
    r"has been closed|browser has been closed|context has been closed|CDP 连接失败",
    re.IGNORECASE,
)
_HINT_CDP_EXISTING = (
    "请确认 Chrome 已开启远程调试且已登录小红书；首次建立此工具的 CDP 连接时，"
    "需在 Chrome 授权弹窗点击『允许』。验证码、私信权限限制或页面结构变化需要在 Chrome 人工处理。"
)
_HINT_CDP_SELF = (
    "客户端会自动启动独立小红书浏览器，不需要连接本机正在使用的 Chrome，也不需要授权 9222。"
    "若未登录，请在客户端打开的浏览器中登录小红书；验证码或私信权限限制需在该窗口人工处理。"
)


def _hint_cdp() -> str:
    return _HINT_CDP_EXISTING if config.CDP_CONNECT_EXISTING else _HINT_CDP_SELF


@dataclass(frozen=True)
class PendingDm:
    draft_id: str
    user_id: str
    nickname: str
    content: str
    created_at: float
    xiaohongshu_id: str = ""


_bot: Optional[XiaohongshuDmBot] = None
_pending: Optional[PendingDm] = None
_last_attempt_by_user: dict[str, float] = {}
_bot_lock = asyncio.Lock()


def _error_json(message: str, hint: str = "", **extra: Any) -> str:
    result: dict[str, Any] = {"ok": False, "message": str(message)[:500], **extra}
    if hint:
        result["hint"] = hint
    return json.dumps(result, ensure_ascii=False)


def _is_connection_error(exc: BaseException) -> bool:
    return bool(_CONNECTION_ERROR_RE.search(str(exc)))


async def _get_bot() -> XiaohongshuDmBot:
    global _bot
    if _bot is not None and not await _bot.check_alive():
        await _reset_bot()
    if _bot is None:
        candidate = XiaohongshuDmBot()
        await candidate.setup()
        _bot = candidate
    return _bot


async def _reset_bot() -> None:
    global _bot, _pending
    bot, _bot = _bot, None
    _pending = None
    if bot is not None:
        try:
            await asyncio.wait_for(bot.close(), timeout=20)
        except Exception:
            pass


async def cleanup_bot() -> None:
    """Agent 退出时清空内存草稿、限流状态并断开 CDP，不写本地文件。"""
    global _bot, _pending
    bot, _bot = _bot, None
    _pending = None
    _last_attempt_by_user.clear()
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


class PrepareXhsDmArgs(BaseModel):
    user_id: Optional[str] = Field(
        default=None,
        min_length=24,
        max_length=128,
        description="可选的小红书内部用户 ID；当前配置下可直接传 xhs_note_comment.creator_hash",
    )
    xiaohongshu_id: Optional[str] = Field(
        default=None,
        min_length=3,
        max_length=64,
        description="可选的公开小红书号，例如 5460254944；工具会在网页用户搜索中解析内部 ID",
    )
    content: str = Field(
        min_length=1,
        max_length=500,
        description="待发送的私信正文；本工具只填入草稿，不发送",
    )
    profile_url: Optional[str] = Field(
        default=None,
        max_length=2000,
        description="可选的小红书用户主页完整链接；带有效 xsec_token 时定位更稳定",
    )


@tool(args_schema=PrepareXhsDmArgs)
async def prepare_xhs_dm(
    content: str,
    user_id: Optional[str] = None,
    xiaohongshu_id: Optional[str] = None,
    profile_url: Optional[str] = None,
) -> str:
    """准备一条小红书私信草稿，但绝不发送。

打开目标用户主页，进入聊天并填入内容，返回目标昵称、正文和一次性 draft_id。
必须把这些信息展示给用户，并在用户明确确认后才能调用 confirm_xhs_dm。评论爬取已由
crawl_by_keywords/crawl_specified_ids 完成；XHS_SAVE_ORIGINAL_USER_INFO=True 时，评论记录的
creator_hash 就是这里需要的 user_id。
"""
    global _pending
    content = content.strip()
    if not content:
        return _error_json("私信内容不能为空")
    user_id = (user_id or "").strip()
    xiaohongshu_id = (xiaohongshu_id or "").strip()
    if not user_id and not xiaohongshu_id:
        return _error_json("user_id 与 xiaohongshu_id 至少需要提供一个")
    if not getattr(config, "XHS_SAVE_ORIGINAL_USER_INFO", False) and user_id and not profile_url and not xiaohongshu_id:
        return _error_json(
            "当前 XHS_SAVE_ORIGINAL_USER_INFO=False，creator_hash 是不可逆哈希，不能用于定位私信目标",
            "请开启原始用户 ID 保存后重新爬取评论，或传入与 user_id 匹配的完整 profile_url。",
        )
    try:
        async with _bot_lock:
            bot = await _get_bot()
            resolved_nickname = ""
            if xiaohongshu_id:
                resolved_user_id, resolved_nickname, resolved_profile_url = await bot.resolve_public_xhs_id(
                    xiaohongshu_id
                )
                if user_id and user_id != resolved_user_id:
                    raise RuntimeError("公开小红书号解析到的用户与传入 user_id 不一致")
                user_id = resolved_user_id
                profile_url = resolved_profile_url
            nickname = await bot.prepare_dm(user_id, content, profile_url)
            if nickname == "(未知用户)" and resolved_nickname:
                nickname = resolved_nickname
            pending = PendingDm(
                draft_id=secrets.token_urlsafe(12),
                user_id=user_id.strip(),
                nickname=nickname,
                content=content,
                created_at=time.monotonic(),
                xiaohongshu_id=xiaohongshu_id,
            )
            _pending = pending
        return json.dumps(
            {
                "ok": True,
                "sent": False,
                "requires_confirmation": True,
                "draft_id": pending.draft_id,
                "expires_in_seconds": DRAFT_TTL_SECONDS,
                "user_id": pending.user_id,
                "xiaohongshu_id": pending.xiaohongshu_id,
                "nickname": pending.nickname,
                "content": pending.content,
                "message": "草稿已填入 Chrome，尚未发送。请向用户展示目标与正文；只有用户明确确认后才可确认发送。",
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        if _is_connection_error(exc):
            await _reset_bot()
        return _error_json(f"{type(exc).__name__}: {exc}", _hint_cdp())


class ConfirmXhsDmArgs(BaseModel):
    draft_id: str = Field(
        min_length=8,
        max_length=128,
        description="prepare_xhs_dm 返回的一次性草稿 ID；仅在用户明确确认目标和正文后传入",
    )


@tool(args_schema=ConfirmXhsDmArgs)
async def confirm_xhs_dm(draft_id: str) -> str:
    """确认并发送已准备的小红书私信草稿。

这是不可撤回的真实写操作。仅当用户已经看到 prepare_xhs_dm 返回的目标昵称/user_id 和完整正文，
并明确要求发送时调用。草稿十分钟过期且只能用一次；发送前会再次校验目标与输入框内容，不自动重试。
"""
    global _pending
    try:
        async with _bot_lock:
            pending = _pending
            if pending is None or not secrets.compare_digest(pending.draft_id, draft_id.strip()):
                return _error_json("草稿 ID 无效或已使用；请重新调用 prepare_xhs_dm")
            if time.monotonic() - pending.created_at > DRAFT_TTL_SECONDS:
                _pending = None
                return _error_json("草稿已超过 10 分钟有效期；请重新准备并再次确认")

            now = time.monotonic()
            last_attempt = _last_attempt_by_user.get(pending.user_id, 0.0)
            remaining = DM_COOLDOWN_SECONDS - (now - last_attempt)
            if remaining > 0:
                return _error_json(
                    f"同一用户发送冷却中，请至少等待 {int(remaining) + 1} 秒后重新准备草稿",
                    retry_after_seconds=int(remaining) + 1,
                )

            bot = await _get_bot()
            # 确认尝试本身即进入冷却，避免“已发出但页面自检异常”时重复调用造成骚扰。
            _last_attempt_by_user[pending.user_id] = now
            _pending = None  # 一次性消费；即使结果不确定也禁止使用同一草稿重发
            self_checked = await bot.submit_dm(pending.user_id, pending.content)

        message = (
            "私信已发送，页面自检通过。"
            if self_checked
            else "发送动作已执行，但页面自检未确认；请在 Chrome 中人工核对，不要自动重试。"
        )
        return json.dumps(
            {
                "ok": True,
                "sent": True,
                "self_checked": self_checked,
                "user_id": pending.user_id,
                "nickname": pending.nickname,
                "message": message,
            },
            ensure_ascii=False,
        )
    except Exception as exc:
        connection_error = _is_connection_error(exc)
        if connection_error:
            await _reset_bot()
        return _error_json(
            f"{type(exc).__name__}: {exc}",
            "本次确认草稿已作废。由于发送状态可能不确定，请先在 Chrome 聊天记录中核对，禁止自动重试。",
            sent_unknown=True,
        )
