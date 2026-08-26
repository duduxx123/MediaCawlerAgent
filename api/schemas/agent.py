# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/api/schemas/agent.py
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

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


class AgentChatMessage(BaseModel):
    """对话历史中的单条消息"""

    role: Literal["user", "assistant"]
    content: str


class AgentChatRequest(BaseModel):
    """智能体对话请求"""

    message: str = Field(..., min_length=1, max_length=8000, description="用户消息")
    history: List[AgentChatMessage] = Field(default_factory=list, description="对话历史（旧前端兼容；服务端有该会话记忆时会忽略此字段）")
    session_id: str = Field(default="", max_length=128, description="会话 id：同一会话传同一 id 即可续聊（服务端 checkpointer 自动加载记忆）；不传则用默认会话")


class AgentChatResponse(BaseModel):
    """智能体对话响应"""

    reply: str
    history: List[AgentChatMessage] = Field(description="追加本轮后的完整对话历史")
    tool_calls: List[dict] = Field(default_factory=list, description="本轮工具调用记录")
    error: Optional[str] = Field(default=None, description="出错时的错误信息")


class AgentStatusResponse(BaseModel):
    """智能体状态"""

    available: bool = Field(description="LLM API Key 是否已配置")
    model: str
    base_url: str
    tools: List[str]
    crawler_busy: bool = Field(description="是否已有爬取任务在执行")
