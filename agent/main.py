# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/main.py
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
爬虫智能体 CLI 入口。

用法：
    python -m agent.main                 # 交互式对话
    python -m agent.main "帮我搜B站关键词..."   # 单次问答后退出

命令：/exit 退出 | /clear 清空对话 | /tools 查看工具清单
"""

import sys
import io

# Windows GBK 控制台下输出中文必需：强制 stdout/stderr 为 UTF-8
if sys.stdout and hasattr(sys.stdout, "buffer"):
    if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
if sys.stderr and hasattr(sys.stderr, "buffer"):
    if sys.stderr.encoding and sys.stderr.encoding.lower() != "utf-8":
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import asyncio
from typing import Optional

from .services.agent_factory import chat_stream, clear_thread, load_settings
from .tools import ALL_TOOLS

THREAD_ID = "cli"  # CLI 会话 id（InMemorySaver 进程内记忆：同进程内 /clear 之前一直有记忆，退出即忘）

BANNER = """
============================================================
  MediaCrawler 爬虫智能体 (LangChain + DeepSeek)
============================================================
  支持平台: 抖音 / 小红书 / 快手 / B站 / 微博 / 贴吧 / 知乎
  命令: /exit 退出 | /clear 清空对话记忆 | /tools 工具清单
  提示: 首次在某平台抓取时，会自动打开/复用 Chrome 浏览器，
        请扫码登录一次（登录态会保存到本地）。
============================================================
"""


def _print_help_configure() -> None:
    print("[未配置 LLM API Key] 请在项目根目录创建 .env 文件（可复制 .env.example），并设置：")
    print("  DEEPSEEK_API_KEY=sk-xxxx  （申请地址: https://platform.deepseek.com）")
    print("可选: AGENT_LLM_BASE_URL / AGENT_LLM_MODEL 切换其他 OpenAI 兼容服务。")


async def _run_chat(message: str, thread_id: str = THREAD_ID) -> None:
    """流式执行一轮对话。会话记忆由 LangChain checkpointer 按 thread_id 自动读写。"""
    async for event in chat_stream(message, history=None, thread_id=thread_id):
        etype = event.get("type")
        if etype == "token":
            print(event["content"], end="", flush=True)
        elif etype == "tool_start":
            print(f"\n[工具] {event['name']} 执行中...", flush=True)
        elif etype == "tool_end":
            status = "成功" if event.get("result_ok") else "失败/异常"
            print(f"[工具] {event['name']} {status}", flush=True)
        elif etype == "error":
            print(f"\n[错误] {event['message']}", flush=True)


async def main(single_message: Optional[str] = None) -> None:
    settings = load_settings()
    if not settings.available:
        _print_help_configure()
        return

    try:
        print(BANNER)
        print(f"模型: {settings.model} ({settings.base_url})")

        if single_message:
            await _run_chat(single_message)
            print()
            return

        while True:
            try:
                user_input = await asyncio.to_thread(input, "你 > ")
            except (EOFError, KeyboardInterrupt):
                print("\n再见！")
                break

            cmd = user_input.strip()
            if cmd in ("/exit", "/quit"):
                print("再见！")
                break
            if cmd == "/clear":
                ok = await clear_thread(THREAD_ID)
                print("已清空对话记忆。" if ok else "清空失败（记忆库不可用）。")
                continue
            if cmd == "/tools":
                for t in ALL_TOOLS:
                    print(f"  - {t.name}: {t.description}")
                continue
            if not cmd:
                continue

            print("助手 > ", end="", flush=True)
            await _run_chat(cmd)
            print("\n")
    finally:
        # 退出前优雅关闭各写操作 bot 的 CDP/内存状态，避免残留连接或私信草稿
        from agent.tools.bili_comment_tools import cleanup_bot as cleanup_bili_bot
        from agent.tools.comment_tools import cleanup_bot
        from agent.tools.kuaishou_comment_tools import cleanup_bot as cleanup_ks_comment_bot
        from agent.tools.xhs_dm_tools import cleanup_bot as cleanup_xhs_dm_bot
        await cleanup_bot()
        await cleanup_bili_bot()
        await cleanup_ks_comment_bot()
        await cleanup_xhs_dm_bot()


if __name__ == "__main__":
    message = sys.argv[1] if len(sys.argv) > 1 else None
    try:
        asyncio.run(main(single_message=message))
    except KeyboardInterrupt:
        print("\n已退出。")
