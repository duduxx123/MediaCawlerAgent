# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/__init__.py
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
MediaCrawler 爬虫智能体（LangChain Agent）包。

结构：
- agent/services/crawler_runner.py  子进程方式运行爬虫（复用 main.py CLI），读取落盘数据
- agent/services/agent_factory.py   DeepSeek LLM + LangGraph ReAct 智能体构建与流式封装
- agent/tools/                       LangChain 工具定义（通用多平台爬取工具 + 数据工具）
- agent/main.py                      CLI 交互入口

注意：本包绝不 import config / main / cmd_arg / media_platform（避免全局配置副作用），
所有爬取参数通过子进程命令行传递。
"""
