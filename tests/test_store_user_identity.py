# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_store_user_identity.py
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

"""各平台「脱敏/原文」独立开关单测（不触网）。

每个平台 config 有独立开关（config/dy_config.py 的 DY_SAVE_ORIGINAL_USER_INFO 等）：
- True（当前默认）：该平台落库的用户标识与昵称保存原文
- False：教学版脱敏（sha256 哈希 + 昵称打码，合规路径另有 *_no_user_info.py 测试覆盖）
"""

import asyncio

import config
import pytest

from store.douyin import _comment_nickname, _comment_user_key
from tools.user_hash import anonymize_user_id, mask_nickname

USER_INFO = {
    "uid": "12345",
    "sec_uid": "MS4wLjABAAAAAAAAtestSecUid",
    "nickname": "奶服",
}


class TestDouyinStore:
    def test_default_anonymized(self, monkeypatch):
        monkeypatch.setattr(config, "DY_SAVE_ORIGINAL_USER_INFO", False)
        result = _comment_user_key(USER_INFO)
        assert result == anonymize_user_id("12345")
        assert "MS4wLjAB" not in result  # 脱敏模式不落原始 sec_uid

    def test_original_sec_uid_when_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "DY_SAVE_ORIGINAL_USER_INFO", True)
        assert _comment_user_key(USER_INFO) == "MS4wLjABAAAAAAAAtestSecUid"
        assert _comment_nickname(USER_INFO) == "奶服"

    def test_empty_when_no_sec_uid_and_enabled(self, monkeypatch):
        monkeypatch.setattr(config, "DY_SAVE_ORIGINAL_USER_INFO", True)
        assert _comment_user_key({"uid": "12345"}) == ""


class TestXhsStore:
    """跨平台冒烟：开关打开时其他平台 store 同样落原文（以小红书为例）。"""

    def test_note_raw_user_info_when_enabled(self, monkeypatch):
        import store.xhs as xs

        monkeypatch.setattr(config, "XHS_SAVE_ORIGINAL_USER_INFO", True)
        note_item = {
            "note_id": "abc",
            "type": "normal",
            "title": "t",
            "desc": "d",
            "time": 1,
            "last_update_time": 0,
            "user": {"user_id": "u123", "nickname": "小红同学", "avatar": "http://x/a.jpg"},
            "ip_location": "上海",
            "interact_info": {"liked_count": "1", "collected_count": "0",
                              "comment_count": "0", "share_count": "0"},
            "image_list": [], "tag_list": [], "xsec_token": "tok",
        }
        captured = {}

        class FakeStore:
            async def store_content(self, content_item):
                captured.update(content_item)

        orig = xs.XhsStoreFactory.create_store
        xs.XhsStoreFactory.create_store = staticmethod(lambda: FakeStore())
        try:
            asyncio.run(xs.update_xhs_note(note_item))
        finally:
            xs.XhsStoreFactory.create_store = orig
        assert captured.get("creator_hash") == "u123"  # 原文 user_id
        assert captured.get("nickname") == "小红同学"  # 原文昵称

    def test_note_anonymized_when_disabled(self, monkeypatch):
        import store.xhs as xs

        monkeypatch.setattr(config, "XHS_SAVE_ORIGINAL_USER_INFO", False)
        note_item = {
            "note_id": "abc",
            "type": "normal",
            "title": "t",
            "desc": "d",
            "time": 1,
            "last_update_time": 0,
            "user": {"user_id": "u123", "nickname": "小红同学", "avatar": "http://x/a.jpg"},
            "ip_location": "上海",
            "interact_info": {"liked_count": "1", "collected_count": "0",
                              "comment_count": "0", "share_count": "0"},
            "image_list": [], "tag_list": [], "xsec_token": "tok",
        }
        captured = {}

        class FakeStore:
            async def store_content(self, content_item):
                captured.update(content_item)

        orig = xs.XhsStoreFactory.create_store
        xs.XhsStoreFactory.create_store = staticmethod(lambda: FakeStore())
        try:
            asyncio.run(xs.update_xhs_note(note_item))
        finally:
            xs.XhsStoreFactory.create_store = orig
        assert captured.get("creator_hash") == anonymize_user_id("u123")
        assert captured.get("nickname") == mask_nickname("小红同学")
