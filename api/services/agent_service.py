# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/services/agent_service.py
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
智能体 API 服务：懒加载 agent 核心（首次调用才 import langchain 全家桶），
API 启动零负担；未配置 LLM API Key 时 API 照常启动，调用 chat 返回 503。
"""

import asyncio
from typing import Any, AsyncIterator, Dict

from ..schemas import AgentChatRequest


class AgentService:
    """智能体服务（模块级单例，与 crawler_manager 同风格）"""

    def __init__(self) -> None:
        self._init_lock = asyncio.Lock()

    def _import_agent_module(self):
        """懒加载 agent 核心模块。"""
        from agent.services import agent_factory

        return agent_factory

    def get_status(self) -> Dict[str, Any]:
        """智能体状态（轻量，不创建 agent 实例）。"""
        try:
            agent_factory = self._import_agent_module()
            return agent_factory.get_status()
        except Exception as e:
            return {
                "available": False,
                "model": "",
                "base_url": "",
                "tools": [],
                "crawler_busy": False,
                "error": f"{type(e).__name__}: {e}",
            }

    async def chat(self, request: AgentChatRequest) -> Dict[str, Any]:
        """非流式对话。未配置 API Key 时抛 RuntimeError（路由层转 503）。"""
        agent_factory = self._import_agent_module()
        if not agent_factory.load_settings().available:
            raise RuntimeError("未配置 LLM API Key：请在项目根目录 .env 中设置 DEEPSEEK_API_KEY（参考 .env.example）")
        history = [{"role": m.role, "content": m.content} for m in request.history]
        return await agent_factory.chat(request.message, history, thread_id=request.session_id or agent_factory.DEFAULT_THREAD_ID)

    async def chat_stream(self, request: AgentChatRequest) -> AsyncIterator[Dict[str, Any]]:
        """流式对话，透传 agent_factory 的统一事件流。"""
        agent_factory = self._import_agent_module()
        if not agent_factory.load_settings().available:
            raise RuntimeError("未配置 LLM API Key：请在项目根目录 .env 中设置 DEEPSEEK_API_KEY（参考 .env.example）")
        history = [{"role": m.role, "content": m.content} for m in request.history]
        async for event in agent_factory.chat_stream(
            request.message, history, thread_id=request.session_id or agent_factory.DEFAULT_THREAD_ID
        ):
            yield event


# 模块级单例
agent_service = AgentService()
