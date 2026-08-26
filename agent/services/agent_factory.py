# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/services/agent_factory.py
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
LLM 与智能体构建：DeepSeek（OpenAI 兼容）+ LangGraph create_react_agent。

所有 langgraph 相关用法收敛在本文件（create_agent / chat_stream 两处），
上层（CLI / API）只消费统一的事件流，版本差异不影响上层。
"""

import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Any, AsyncIterator, Dict, List, Optional

from dotenv import load_dotenv

from ..tools import ALL_TOOLS
from .crawler_runner import is_crawling

PROJECT_ROOT = Path(__file__).parent.parent.parent
load_dotenv(PROJECT_ROOT / ".env")

# 默认配置（环境变量可覆盖，见 .env.example）
DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"  # 官方轻量模型（备选: deepseek-v4-pro）
DEFAULT_TEMPERATURE = 0.3
DEFAULT_RECURSION_LIMIT = 25
DEFAULT_SAMPLE_LIMIT = 5
# 会话记忆自动摘要（SummarizationMiddleware）：历史超 N token 触发 LLM 摘要压缩，
# 保留最近 M 条原文消息。防止长会话硬撞模型上下文窗口报错。
DEFAULT_SUMMARY_TRIGGER_TOKENS = 24000
DEFAULT_SUMMARY_KEEP_MESSAGES = 20

# 会话记忆（LangChain checkpointer 自动读写，替代手写 history 管理）：
# - 默认 InMemorySaver：进程内存，CLI 会话内有效，退出即忘（当前 CLI 用法，无需持久化）
# - 设置环境变量 AGENT_MEMORY_DB=<sqlite文件路径> 后切换为 SQLite 持久化（跨会话/API 前端场景）
AGENT_MEMORY_DB_ENV = "AGENT_MEMORY_DB"
DEFAULT_THREAD_ID = "default"

SYSTEM_PROMPT = """你是「MediaCrawler 助手」，帮助用户抓取和分析抖音、小红书、Bilibili 的内容数据。

可用工具：
- crawl_by_keywords：按关键词搜索并抓取内容（search 模式）
- crawl_specified_ids：按链接/ID 抓取指定内容详情（detail 模式）
- crawl_creator：按创作者主页抓取其作品（creator 模式）
- read_crawled_data：读取 data 目录中已抓取的内容数据
- list_crawled_files：列出 data 目录中已抓取的数据文件
- fetch_comment_users：抓取抖音视频/图集评论及评论者信息（sec_uid），AI 获客第一步
- post_comment：在抖音视频/图集下发布一条新评论（图集帖会自动激活评论区）
- reply_comment：回复某个用户的评论（@某人，需 sec_uid；同一用户多条评论时传 comment_index 序号精确定位）
- send_dm_user：向指定用户发送私信（DM，需 sec_uid）

使用策略：
1. 先确认用户意图，选择最合适的平台与模式（search/detail/creator）；一次任务尽量合并为一次工具调用，不要重复抓取相同内容。
2. 抓取是耗时的子进程任务，可能持续数分钟；调用工具后直接等待结果，不要中途重复调用或假设失败。
3. 控制规模：默认 max_notes 不超过 20，除非用户明确要求更多；遵守目标平台条款，克制抓取频率，避免大并发。
4. 首次使用某平台抓取时需要在浏览器中扫码登录（登录态会保存）；若工具结果提示登录失败，请如实告知用户并给出操作指引。
5. 抓取完成后向用户报告：保存的文件路径、记录数、代表性内容标题（引用工具返回的摘要），不要编造统计数字。
6. 数据文件位于项目 data/ 目录，可用 read_crawled_data / list_crawled_files 查看和分析。
7. 回答保持简洁：先结论后细节，默认使用中文。
8. AI 获客流程：fetch_comment_users 拿到评论与 sec_uid → 识别有意向的客户 → reply_comment 评论回复（sec_uid 原样传递；同一用户有多条评论时传 comment_index 抓取序号精确定位，不要用评论正文消歧——表情/图片在页面里渲染成图片，文本对不上）或 send_dm_user 私信深度跟进；内容要针对该用户评论个性化、自然克制，不要推销腔。
9. post_comment / reply_comment / send_dm_user 是真实发布的写操作：发布前确认内容合规；同一视频避免高频连发；未经用户明确要求不要主动发布；私信是打扰性最强的渠道，除非用户明确要求否则不要主动私信。
10. 写操作依赖用户本地已开启远程调试的 Chrome 且需已登录抖音（自动连接，每次新连接需用户在 Chrome 弹窗点『允许』）；工具报连接错误时如实告知用户。
11. reply_comment 失败提示「找不到目标评论」时不要对同一目标反复重试：热门评论区是动态热排序，应重新 fetch_comment_users 后换一条更靠前的评论（或补传 comment_index 序号精确定位）。"""


def load_settings() -> SimpleNamespace:
    """读取智能体配置（.env + 环境变量），缺 API Key 时 available=False 而非抛错。"""
    api_key = os.environ.get("AGENT_LLM_API_KEY") or os.environ.get("DEEPSEEK_API_KEY") or ""
    base_url = os.environ.get("AGENT_LLM_BASE_URL") or DEFAULT_BASE_URL
    model = os.environ.get("AGENT_LLM_MODEL") or DEFAULT_MODEL

    def _int_env(name: str, default: int) -> int:
        try:
            return int(os.environ.get(name, "")) if os.environ.get(name) else default
        except ValueError:
            return default

    return SimpleNamespace(
        api_key=api_key,
        base_url=base_url,
        model=model,
        available=bool(api_key),
        recursion_limit=_int_env("AGENT_RECURSION_LIMIT", DEFAULT_RECURSION_LIMIT),
        sample_limit=_int_env("AGENT_SAMPLE_LIMIT", DEFAULT_SAMPLE_LIMIT),
        summary_trigger_tokens=_int_env("AGENT_SUMMARY_TRIGGER_TOKENS", DEFAULT_SUMMARY_TRIGGER_TOKENS),
        summary_keep_messages=_int_env("AGENT_SUMMARY_KEEP_MESSAGES", DEFAULT_SUMMARY_KEEP_MESSAGES),
    )


def create_chat_model():
    """构建 DeepSeek ChatOpenAI 模型（低温提高工具调用稳定性）。"""
    from langchain_openai import ChatOpenAI

    settings = load_settings()
    if not settings.available:
        raise RuntimeError("未配置 LLM API Key，请在 .env 中设置 DEEPSEEK_API_KEY（参考 .env.example）")
    return ChatOpenAI(
        model=settings.model,
        base_url=settings.base_url,
        api_key=settings.api_key,
        temperature=DEFAULT_TEMPERATURE,
        timeout=120,
        max_retries=2,
    )


_agent_cache: Optional[Any] = None
_checkpointer: Optional[Any] = None


async def _get_checkpointer():
    """懒加载单例 checkpointer。
    默认 InMemorySaver（进程内存，CLI 会话内记忆，退出即忘）；
    设置环境变量 AGENT_MEMORY_DB 后改用 SQLite 文件持久化（跨会话记忆，CLI/API 共享）。
    """
    global _checkpointer
    if _checkpointer is not None:
        return _checkpointer
    sqlite_path = os.environ.get(AGENT_MEMORY_DB_ENV, "")
    if sqlite_path:
        import aiosqlite
        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        db_path = Path(sqlite_path)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = await aiosqlite.connect(str(db_path))
        saver = AsyncSqliteSaver(conn)
        await saver.setup()
        _checkpointer = saver
    else:
        from langgraph.checkpoint.memory import InMemorySaver

        _checkpointer = InMemorySaver()
    return _checkpointer


async def clear_thread(thread_id: str = DEFAULT_THREAD_ID) -> bool:
    """删除一个会话的全部记忆（CLI /clear、前端清空会话时调用）。"""
    try:
        checkpointer = await _get_checkpointer()
        await checkpointer.adelete_thread(thread_id or DEFAULT_THREAD_ID)
        return True
    except Exception:
        return False


def _tool_error_formatter(error: Exception, request: Any = None) -> str:
    """工具执行异常 -> 回传 LLM 的紧凑中文错误 JSON（ToolErrorMiddleware 的 on_error）。

    工具函数体内抛异常在默认配置下会击穿整个 agent 运行（LLM 收不到任何信息），
    通过 ToolErrorMiddleware 把异常转成结构化错误 ToolMessage 回传，LLM 可据此
    修正参数重试或如实告知用户失败原因。参数校验错误由 ToolNode 上游自动转
    error ToolMessage，不经过本函数。
    """
    tool_name = ""
    try:
        if request is not None:
            call = getattr(request, "tool_call", None)
            if isinstance(call, dict):
                tool_name = call.get("name", "")
    except Exception:
        pass
    message = str(error)[:300]
    return json.dumps(
        {
            "ok": False,
            "message": f"工具 '{tool_name or 'unknown'}' 执行出错: {type(error).__name__}: {message}",
            "hint": "请根据错误信息修正参数后重试；若无法修复，请如实告知用户该工具执行失败及原因。",
        },
        ensure_ascii=False,
    )


async def get_agent():
    """懒加载单例：LangChain create_agent + SQLite checkpointer（跨会话记忆）。
    未配置 API Key 时返回 None。"""
    global _agent_cache
    if _agent_cache is not None:
        return _agent_cache
    if not load_settings().available:
        return None
    from langchain.agents import create_agent
    from langchain.agents.middleware import SummarizationMiddleware, ToolErrorMiddleware

    settings = load_settings()
    model = create_chat_model()
    checkpointer = await _get_checkpointer()
    _agent_cache = create_agent(
        model,
        tools=ALL_TOOLS,
        system_prompt=SYSTEM_PROMPT,
        middleware=[
            ToolErrorMiddleware(on_error=_tool_error_formatter),
            # 长会话自动摘要：历史超阈值 token 时 LLM 压缩旧消息，保留最近 N 条原文，
            # 避免上下文窗口硬撞墙报错（阈值见 .env AGENT_SUMMARY_*）
            SummarizationMiddleware(
                model=model,
                trigger=("tokens", settings.summary_trigger_tokens),
                keep=("messages", settings.summary_keep_messages),
            ),
        ],
        checkpointer=checkpointer,
    )
    return _agent_cache


def _history_to_messages(history: Optional[List[Dict[str, str]]]):
    """把 {'role','content'} 历史转成 LangChain 消息。"""
    from langchain_core.messages import AIMessage, HumanMessage

    messages = []
    for item in history or []:
        role = (item.get("role") or "user").lower()
        content = item.get("content", "")
        if not content:
            continue
        messages.append(AIMessage(content=content) if role == "assistant" else HumanMessage(content=content))
    return messages


def _messages_to_history(messages: Optional[List[Any]]) -> List[Dict[str, str]]:
    """checkpoint 里的消息 → 统一 {'role','content'} 历史（只保留 user/assistant 轮次）。"""
    out: List[Dict[str, str]] = []
    for m in messages or []:
        data = m.model_dump() if hasattr(m, "model_dump") else m
        mtype = data.get("type")
        if mtype in ("human", "user"):
            out.append({"role": "user", "content": str(data.get("content") or "")})
        elif mtype in ("ai", "assistant"):
            content = data.get("content") or ""
            if isinstance(content, list):
                content = "".join(
                    (c.get("text", "") if isinstance(c, dict) else str(c)) for c in content
                )
            out.append({"role": "assistant", "content": content})
    return out


def _tool_result_ok(content: Any) -> bool:
    """尽力判断工具返回是否 ok（我们的工具统一返回紧凑 JSON 字符串）。"""
    if isinstance(content, str):
        try:
            data = json.loads(content)
            return bool(data.get("ok")) if isinstance(data, dict) else False
        except json.JSONDecodeError:
            return False
    return False


async def chat_stream(
    message: str,
    history: Optional[List[Dict[str, str]]] = None,
    thread_id: str = DEFAULT_THREAD_ID,
    recursion_limit: Optional[int] = None,
) -> AsyncIterator[Dict[str, Any]]:
    """流式对话，产出统一事件流：

    {"type": "token", "content": str}          模型输出片段
    {"type": "tool_start", "name": str, "args": dict}
    {"type": "tool_end", "name": str, "result_ok": bool}
    {"type": "error", "message": str}          出错时终止流
    {"type": "done", "reply": str, "history": [...]}  结束事件（唯一一次）

    会话记忆由 LangChain checkpointer（SQLite）按 thread_id 自动读写：
    - 该 thread 已有记忆 → 只发送本轮消息（历史由 checkpointer 自动加载）
    - 该 thread 无记忆且调用方传了 history（旧前端兼容）→ 用 history 播种
    """
    agent = await get_agent()
    if agent is None:
        yield {"type": "error", "message": "未配置 LLM API Key：请在项目根目录 .env 中设置 DEEPSEEK_API_KEY（参考 .env.example）"}
        return

    settings = load_settings()
    from langchain_core.messages import HumanMessage

    config = {"configurable": {"thread_id": thread_id or DEFAULT_THREAD_ID},
              "recursion_limit": recursion_limit or settings.recursion_limit}

    # 会话记忆判断：checkpoint 已有该 thread 的消息 → 只发本轮；否则用 history 播种
    try:
        state = await agent.aget_state(config)
    except Exception:
        state = None
    has_memory = bool(state and (state.values or {}).get("messages"))
    messages = [HumanMessage(content=message)]
    if not has_memory:
        messages = _history_to_messages(history) + messages

    reply_parts: List[str] = []
    seen_tool_calls = set()
    try:
        async for event in agent.astream({"messages": messages}, config=config):
            # 默认 stream_mode="updates"：event 为 {节点名: {"messages": [...]}}
            for node_output in event.values():
                if not isinstance(node_output, dict):
                    continue
                for msg in node_output.get("messages", []) or []:
                    data = msg.model_dump() if hasattr(msg, "model_dump") else msg
                    msg_type = data.get("type")

                    if msg_type in ("ai", "AIMessageChunk"):
                        tool_calls = data.get("tool_calls") or []
                        for tc in tool_calls:
                            name = tc.get("name")
                            if name and name not in seen_tool_calls:
                                seen_tool_calls.add(name)
                                yield {"type": "tool_start", "name": name, "args": tc.get("args") or {}}
                        content = data.get("content") or ""
                        if isinstance(content, list):
                            content = "".join(
                                (c.get("text", "") if isinstance(c, dict) else str(c)) for c in content
                            )
                        if content:
                            reply_parts.append(content)
                            yield {"type": "token", "content": content}

                    elif msg_type == "tool":
                        yield {
                            "type": "tool_end",
                            "name": data.get("name", ""),
                            "result_ok": _tool_result_ok(data.get("content")),
                        }
    except Exception as e:
        yield {"type": "error", "message": f"智能体运行出错: {type(e).__name__}: {e}"}
        return

    reply = "".join(reply_parts).strip()
    if not reply:
        reply = "（本轮任务已通过工具执行完成，未生成文字总结。）"

    # 从 checkpointer 读回完整历史（含本轮），保持统一事件契约
    try:
        state = await agent.aget_state(config)
        new_history = _messages_to_history((state.values or {}).get("messages", []) if state else [])
    except Exception:
        new_history = list(history or []) + [
            {"role": "user", "content": message},
            {"role": "assistant", "content": reply},
        ]
    yield {"type": "done", "reply": reply, "history": new_history}


async def chat(message: str, history: Optional[List[Dict[str, str]]] = None,
               thread_id: str = DEFAULT_THREAD_ID) -> Dict[str, Any]:
    """非流式封装：返回 {reply, history, tool_calls, error}。"""
    reply_parts: List[str] = []
    tool_calls: List[Dict[str, Any]] = []
    final_history: List[Dict[str, str]] = list(history or [])
    error: Optional[str] = None

    async for event in chat_stream(message, history, thread_id=thread_id):
        etype = event.get("type")
        if etype == "token":
            reply_parts.append(event["content"])
        elif etype == "tool_start":
            tool_calls.append({"name": event["name"], "status": "running"})
        elif etype == "tool_end":
            for tc in tool_calls:
                if tc["name"] == event["name"] and tc["status"] == "running":
                    tc["status"] = "ok" if event.get("result_ok") else "error"
                    break
        elif etype == "error":
            error = event["message"]
        elif etype == "done":
            final_history = event["history"]
            if not reply_parts:
                reply_parts.append(event["reply"])

    reply = "".join(reply_parts).strip() or error or "（无回复）"
    return {"reply": reply, "history": final_history, "tool_calls": tool_calls, "error": error}


def get_status() -> Dict[str, Any]:
    """智能体状态（供 API /agent/status 使用）。"""
    settings = load_settings()
    return {
        "available": settings.available,
        "model": settings.model,
        "base_url": settings.base_url,
        "tools": [t.name for t in ALL_TOOLS],
        "crawler_busy": is_crawling(),
    }
