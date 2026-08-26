# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/routers/agent.py
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

"""智能体 HTTP 接口：/api/agent/chat（非流式）、/api/agent/chat/stream（SSE 流式）、/api/agent/status。"""

import json

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from ..schemas import AgentChatRequest, AgentChatResponse, AgentStatusResponse
from ..services import agent_service

router = APIRouter(prefix="/agent", tags=["agent"])


@router.post("/chat", response_model=AgentChatResponse)
async def chat(request: AgentChatRequest):
    """智能体对话（非流式）。爬取类请求可能耗时数分钟。"""
    try:
        result = await agent_service.chat(request)
    except RuntimeError as e:
        # 未配置 LLM API Key
        raise HTTPException(status_code=503, detail=str(e))
    except Exception as e:
        # LLM 限流（429）/网络等
        raise HTTPException(status_code=502, detail=f"智能体服务调用失败，请稍后重试: {type(e).__name__}: {e}")
    return AgentChatResponse(
        reply=result["reply"],
        history=[{"role": m["role"], "content": m["content"]} for m in result["history"]],
        tool_calls=result["tool_calls"],
        error=result.get("error"),
    )


@router.post("/chat/stream")
async def chat_stream(request: AgentChatRequest):
    """智能体对话（SSE 流式）。

    帧格式（JSON，每帧一行 `data: {...}`）：
    - {"type": "token", "content": "..."}
    - {"type": "tool_start", "name": "crawl_by_keywords", "args": {...}}
    - {"type": "tool_end", "name": "...", "result_ok": true}
    - {"type": "done", "reply": "...", "history": [...]}
    - {"type": "error", "message": "..."}
    """

    async def event_generator():
        try:
            async for event in agent_service.chat_stream(request):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except RuntimeError as e:
            # 未配置 LLM API Key
            yield f"data: {json.dumps({'type': 'error', 'message': str(e)}, ensure_ascii=False)}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'type': 'error', 'message': f'智能体服务调用失败，请稍后重试: {type(e).__name__}: {e}'}, ensure_ascii=False)}\n\n"

    return StreamingResponse(event_generator(), media_type="text/event-stream")


@router.get("/status", response_model=AgentStatusResponse)
async def get_status():
    """智能体状态：API Key 是否配置、模型信息、工具清单、爬取任务占用情况。"""
    status = agent_service.get_status()
    return AgentStatusResponse(
        available=status.get("available", False),
        model=status.get("model", ""),
        base_url=status.get("base_url", ""),
        tools=status.get("tools", []),
        crawler_busy=status.get("crawler_busy", False),
    )
