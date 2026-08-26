# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/ks_config.py
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

# Kuaishou platform configuration

# Specify Kuaishou video URL list (supports complete URL or pure ID)
# Supported formats:
# 1. Full video URL: "https://www.kuaishou.com/short-video/3x3zxz4mjrsc8ke?authorId=3x84qugg4ch9zhs&streamSource=search"
# 2. Pure video ID: "3xf8enb8dbj6uig"
KS_SPECIFIED_ID_LIST = [
    "https://www.kuaishou.com/short-video/3x3zxz4mjrsc8ke?authorId=3x84qugg4ch9zhs&streamSource=search&area=searchxxnull&searchKey=python",
    "3xf8enb8dbj6uig",
    # ........................
]

# Specify Kuaishou creator URL list (supports full URL or pure ID)
# Supported formats:
# 1. Creator homepage URL: "https://www.kuaishou.com/profile/3x84qugg4ch9zhs"
# 2. Pure user_id: "3x4sm73aye7jq7i"
KS_CREATOR_ID_LIST = [
    "https://www.kuaishou.com/profile/3xf79edg9msa85c",
    "3x4sm73aye7jq7i",
    # ........................
]

# 是否保存原始用户信息（快手）。True（当前）：落库的用户标识（creator_hash 字段）
# 与昵称保存原文，用于 AI 获客等需要定位真实用户的场景。False：教学版脱敏（sha256 哈希 + 昵称打码）。
# 注意：原始用户信息属于个人信息，请仅用于本人授权范围内的合法用途。
KS_SAVE_ORIGINAL_USER_INFO = True
