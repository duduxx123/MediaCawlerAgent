# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/tools/__init__.py
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

from langchain_core.tools import BaseTool

from .bili_comment_tools import post_bilibili_comment
from .comment_tools import fetch_comment_users, post_comment, reply_comment, send_dm_user
from .crawl_tools import crawl_by_keywords, crawl_creator, crawl_specified_ids
from .data_tool import list_crawled_files, read_crawled_data
from .kuaishou_comment_tools import post_kuaishou_comment
from .xhs_dm_tools import confirm_xhs_dm, prepare_xhs_dm

ALL_TOOLS: list[BaseTool] = [
    crawl_by_keywords,
    crawl_specified_ids,
    crawl_creator,
    read_crawled_data,
    list_crawled_files,
    fetch_comment_users,
    post_comment,
    reply_comment,
    send_dm_user,
    post_bilibili_comment,
    prepare_xhs_dm,
    confirm_xhs_dm,
    post_kuaishou_comment,
]

__all__ = [
    "ALL_TOOLS",
    "crawl_by_keywords",
    "crawl_specified_ids",
    "crawl_creator",
    "read_crawled_data",
    "list_crawled_files",
    "fetch_comment_users",
    "post_comment",
    "reply_comment",
    "send_dm_user",
    "post_bilibili_comment",
    "prepare_xhs_dm",
    "confirm_xhs_dm",
    "post_kuaishou_comment",
]
