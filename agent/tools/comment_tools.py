# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/tools/comment_tools.py
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
评论/私信获客工具（AI 获客全链路）：
  fetch_comment_users —— API 快速抓取视频评论及评论者 sec_uid（复用本项目 DouYinClient）
  post_comment        —— CDP 操作已登录 Chrome 发布一条新评论
  reply_comment       —— CDP 回复某个用户（按 sec_uid 定位，@某人）
  send_dm_user        —— CDP 向指定用户发送私信（填框+发送+聊天记录自检）

底层复用 media_platform/douyin/comment_bot.py 的 DouyinCommentBot：
  - 浏览器来源遵循 CDP_CONNECT_EXISTING；打包客户端使用自己的独立 profile
  - 单例懒加载 + 全局限流锁（CDP 页面共享，读写串行）
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
from media_platform.douyin.comment_bot import DouyinCommentBot

# 单例 bot 与串行锁：CDP 连接/页面是共享资源，同一时刻只允许一个操作
_bot: Optional[DouyinCommentBot] = None
_bot_lock = asyncio.Lock()

# 连接级错误特征：只有这类错误才丢弃 bot 重建（重建 = 新 CDP 连接 = 用户又要点一次「允许」）。
# 普通页面级错误（找不到目标评论、填框失败等）连接还是好的，保留复用，不折腾用户。
_CONNECTION_ERROR_RE = re.compile(
    r"target closed|connection closed|connection reset|connection refused|websocket|"
    r"has been closed|browser has been closed|context has been closed|CDP 连接失败",
    re.IGNORECASE,
)


def _is_connection_error(e: BaseException) -> bool:
    return bool(_CONNECTION_ERROR_RE.search(str(e)))

_HINT_CDP_EXISTING = ("请确认用户已运行开启远程调试的 Chrome 且已登录抖音，并在 Chrome 弹窗中点击『允许』。"
                      "工具会自动打开并激活评论区；若仍失败，可在 Chrome 里手动点开评论区后重试。")
_HINT_CDP_SELF = ("客户端独立抖音浏览器会自动启动，不需要连接本机正在使用的 Chrome，也不需要授权 9222。"
                  "若提示未登录，请在客户端打开的独立浏览器中登录抖音后重试；登录态保存在 exe 旁的 browser_data 中。")


def _hint_cdp() -> str:
    return _HINT_CDP_EXISTING if config.CDP_CONNECT_EXISTING else _HINT_CDP_SELF

_HINT_REPLY_NOT_FOUND = ("热门视频评论区是动态热排序，序号可能漂移。"
                         "请勿对同一目标反复重试：先用 fetch_comment_users 重新抓取，"
                         "换一条更靠前（index 更小）的评论，或补传 comment_index（序号）精确指定。")


async def _get_bot() -> DouyinCommentBot:
    """懒加载单例 bot；复用前先健康检查（页面被关/连接已断则自动重建）。

    2026-08-24 真机问题：工具调用间 bot 单例的连接可能已经死了（用户关掉标签页、
    Chrome 重启、连接被回收），只查 page.is_closed() 查不出来，下一个工具调用就会
    在死连接上失败。现在复用前随手 evaluate 一下，死了就丢弃重建（重新连 Chrome）。
    """
    global _bot
    if _bot is not None:
        alive = False
        try:
            if _bot.page is not None and not _bot.page.is_closed():
                # 连接是否还活着：随手 evaluate（3 秒内答不上来就当死连接处理）
                await asyncio.wait_for(_bot.page.evaluate("() => true"), timeout=3)
                alive = True
        except Exception:
            alive = False
        if not alive:
            # 旧连接已死：丢弃并顺手关闭（避免残留半开连接），下次调用自动重连
            old, _bot = _bot, None
            try:
                await asyncio.wait_for(old.close(), timeout=20)
            except Exception:
                pass
    if _bot is None:
        bot = DouyinCommentBot()
        await bot.setup()
        _bot = bot
    return _bot


async def _reset_bot() -> None:
    """连接类异常后丢弃旧 bot（关闭自己开的标签页），下次调用自动重连。"""
    global _bot
    bot, _bot = _bot, None
    if bot is not None:
        try:
            await asyncio.wait_for(bot.close(), timeout=20)
        except Exception:
            pass


async def cleanup_bot() -> None:
    """优雅关闭单例 bot（不关用户标签页）。agent 进程退出前调用，避免残留半开 CDP 连接
    导致 Chrome 侧连接堆积、下次连接失败。"""
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


class FetchCommentUsersArgs(BaseModel):
    """抓取视频评论与评论者信息的参数"""

    video_url: str = Field(description="抖音视频链接（支持完整链接/短链接/纯ID）")
    max_comments: int = Field(default=30, ge=1, le=200, description="最多抓取评论数（1-200，默认30）")


@tool(args_schema=FetchCommentUsersArgs)
async def fetch_comment_users(video_url: str, max_comments: int = 30) -> str:
    """抓取抖音视频下的评论及评论者信息（昵称、sec_uid、评论内容、点赞数、回复数）。
这是 AI 获客的第一步：先拿到评论数据识别意向客户，再用 reply_comment 回复目标用户。
sec_uid 是评论者的唯一标识，回复时必须原样传递。仅读取数据，不产生任何写操作。"""
    try:
        async with _bot_lock:
            bot = await _get_bot()
            comments = await bot.fetch_comments_by_api(video_url, max_comments)
        result = []
        for i, c in enumerate(comments, 1):
            result.append({
                "index": i,
                "sec_uid": c.get("sec_uid", ""),
                "nickname": c.get("nickname", ""),
                "text": c.get("text", ""),
                "digg_count": c.get("digg_count", 0),
                "reply_comment_total": c.get("reply_comment_total", 0),
            })
        return json.dumps(
            {"ok": True, "video_url": video_url, "count": len(result), "comments": result},
            ensure_ascii=False,
        )
    except Exception as e:
        if _is_connection_error(e):
            await _reset_bot()
        return _error_json(f"{type(e).__name__}: {e}", _hint_cdp())


class PostCommentArgs(BaseModel):
    """发布新评论的参数"""

    video_url: str = Field(description="抖音视频链接")
    content: str = Field(description="评论内容（建议不超过100字，语气自然合规）", max_length=500)


@tool(args_schema=PostCommentArgs)
async def post_comment(video_url: str, content: str) -> str:
    """在抖音视频/图集下发布一条新评论（真实发布，通过 CDP 操作已登录 Chrome 的网页版）。
图集帖会自动激活评论区，视频与图集都支持。发布前请确认内容合规；同一视频避免高频连发（有风控风险）。"""
    try:
        async with _bot_lock:
            bot = await _get_bot()
            await bot.open_video_page(video_url)
            confirmed = await bot.post_comment(content)
        message = ("评论已发布（自检通过）" if confirmed
                   else "评论已提交，但自检未确认发布（可能被风控拦截或需验证码），请用户在 Chrome 检查该评论是否显示")
        return json.dumps(
            {"ok": True, "message": message, "self_checked": bool(confirmed),
             "video_url": video_url, "content": content},
            ensure_ascii=False,
        )
    except Exception as e:
        if _is_connection_error(e):
            await _reset_bot()
        return _error_json(f"{type(e).__name__}: {e}", _hint_cdp())


class ReplyCommentArgs(BaseModel):
    """回复某个用户评论的参数"""

    video_url: str = Field(description="抖音视频/图集链接")
    sec_uid: str = Field(description="目标评论者的 sec_uid（来自 fetch_comment_users 的返回，必须原样传递）")
    content: str = Field(description="回复内容（建议不超过100字，针对该用户的评论个性化）", max_length=500)
    comment_index: int = Field(default=0, ge=0, description="目标评论在 fetch_comment_users 返回中的序号（1 起）。同一用户有多条评论时强烈建议传，避免回复错评论；不传则回复该用户第一条可见评论")


@tool(args_schema=ReplyCommentArgs)
async def reply_comment(video_url: str, sec_uid: str, content: str, comment_index: int = 0) -> str:
    """回复抖音视频/图集下某个用户的评论（即 @该用户），视频与图集都支持。sec_uid 必须来自 fetch_comment_users 的返回。
同一用户有多条评论时请传 comment_index（该评论在抓取结果中的序号）精确定位——不要用评论正文定位，表情/图片在页面里渲染成图片，文本对不上。回复会真实发布。"""
    try:
        async with _bot_lock:
            bot = await _get_bot()
            nth = 0
            if comment_index > 0:
                # 序号 → 该用户第几条评论：重抓一次 API 校验序号仍指向该用户（热排序会漂移）
                comments = await bot.fetch_comments_by_api(video_url, max(comment_index, 30))
                if comment_index > len(comments):
                    return _error_json(
                        f"序号 {comment_index} 超出本次抓取范围（共 {len(comments)} 条），"
                        "请用 fetch_comment_users 重新抓取后再试。")
                target = comments[comment_index - 1]
                if target.get("sec_uid") != sec_uid:
                    return _error_json(
                        f"第 {comment_index} 条评论已不是目标用户（评论区热排序变化），"
                        "请用 fetch_comment_users 重新抓取后再试。",
                        _HINT_REPLY_NOT_FOUND)
                nth = sum(1 for c in comments[:comment_index] if c.get("sec_uid") == sec_uid)
            await bot.open_video_page(video_url)
            confirmed = await bot.reply_to_comment(sec_uid, content, nth=nth)
        message = ("回复已发布（自检通过）" if confirmed
                   else "回复已提交，但自检未确认发布（可能被风控拦截或需验证码），请用户在 Chrome 检查该回复是否显示")
        return json.dumps(
            {"ok": True, "message": message, "self_checked": bool(confirmed),
             "video_url": video_url, "sec_uid": sec_uid, "comment_index": comment_index, "content": content},
            ensure_ascii=False,
        )
    except Exception as e:
        if _is_connection_error(e):
            await _reset_bot()
        return _error_json(
            f"{type(e).__name__}: {e}",
            _hint_cdp() + " 若提示找不到目标评论：" + _HINT_REPLY_NOT_FOUND,
        )


class SendDmUserArgs(BaseModel):
    """向用户发私信的参数"""

    sec_uid: str = Field(description="目标用户的 sec_uid（来自 fetch_comment_users 的返回，必须原样传递）")
    content: str = Field(description="私信内容（建议不超过100字，针对该用户个性化、自然克制，不要推销腔）", max_length=500)


@tool(args_schema=SendDmUserArgs)
async def send_dm_user(sec_uid: str, content: str) -> str:
    """向指定抖音用户发送私信（DM，真实发送，通过 CDP 操作已登录 Chrome 的网页版）。
典型 AI 获客流程：fetch_comment_users 识别意向客户 → 本工具私信深度跟进（或先用 reply_comment 评论回复）。
发送前请确认内容合规；未经用户明确要求不要主动发送；私信打扰性最强，避免高频群发（风控风险高）。"""
    try:
        async with _bot_lock:
            bot = await _get_bot()
            nickname = await bot.open_user_profile(sec_uid)
            await bot.fill_dm_input(content)
            await bot.submit_dm()
            await bot._self_check_dm_sent(content)
        return json.dumps(
            {"ok": True, "message": f"私信已提交给 {nickname}",
             "sec_uid": sec_uid, "content": content},
            ensure_ascii=False,
        )
    except Exception as e:
        if _is_connection_error(e):
            await _reset_bot()
        return _error_json(f"{type(e).__name__}: {e}", _hint_cdp())
