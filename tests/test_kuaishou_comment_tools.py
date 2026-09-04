# -*- coding: utf-8 -*-
"""快手视频一级评论工具离线测试：不连接 Chrome、不真实发布评论。"""

import json
from types import SimpleNamespace

import pytest

from agent.tools import ALL_TOOLS
from agent.tools import kuaishou_comment_tools
from agent.tools.kuaishou_comment_tools import post_kuaishou_comment
from media_platform.kuaishou import comment_bot as comment_bot_module
from media_platform.kuaishou.comment_bot import (
    KuaishouCommentBot,
    KuaishouCommentError,
    KuaishouPhotoTarget,
    parse_kuaishou_photo_id,
)


PHOTO_ID = "3xc3drzkpyzwdi4"
USER_ID = "3xcurrentuser"
CONTENT = "作品很好看，感谢分享。"
TARGET = KuaishouPhotoTarget(
    photo_id=PHOTO_ID,
    photo_author_id="3xauthor",
    photo_title="测试作品",
    exp_tag="exp-current",
)


class FakeBot:
    def __init__(self):
        self.alive = True
        self.resolve_calls = []
        self.submit_calls = []
        self.closed = False
        self.resolve_error = None
        self.submit_error = None
        self.submit_result = {
            "ok": True,
            "request_accepted": True,
            "sent": True,
            "sent_unknown": False,
            "self_checked": True,
            "comment_id": "1174000000000",
            "photo_id": PHOTO_ID,
        }

    async def setup(self):
        return None

    async def check_alive(self):
        return self.alive

    async def close(self):
        self.closed = True

    async def resolve_photo_target(self, target):
        if self.resolve_error:
            raise self.resolve_error
        self.resolve_calls.append(target)
        return TARGET

    async def submit_comment(self, target, content):
        self.submit_calls.append((target, content))
        if self.submit_error:
            raise self.submit_error
        return self.submit_result


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(kuaishou_comment_tools, "_bot", bot)
    kuaishou_comment_tools._last_attempt_by_target.clear()
    yield bot
    kuaishou_comment_tools._last_attempt_by_target.clear()
    kuaishou_comment_tools._bot = None


async def _post(**overrides):
    args = {
        "target": f"https://www.kuaishou.com/short-video/{PHOTO_ID}",
        "content": CONTENT,
        **overrides,
    }
    return json.loads(await post_kuaishou_comment.ainvoke(args))


class TestPostCommentTool:
    @pytest.mark.asyncio
    async def test_resolves_then_posts_top_level_comment(self, clean_state):
        result = await _post()
        assert result["ok"] is True
        assert clean_state.resolve_calls == [
            f"https://www.kuaishou.com/short-video/{PHOTO_ID}"
        ]
        assert clean_state.submit_calls == [(TARGET, CONTENT)]

    @pytest.mark.asyncio
    async def test_share_short_link_reaches_bot_resolver(self, clean_state):
        short_link = "https://www.kuaishou.com/f/XafdnoC1kJjiXxn"
        result = await _post(target=short_link)
        assert result["ok"] is True
        assert clean_state.resolve_calls == [short_link]

    @pytest.mark.asyncio
    async def test_resolve_failure_never_posts(self, clean_state):
        clean_state.resolve_error = KuaishouCommentError("作品不可评论")
        result = await _post()
        assert result["ok"] is False
        assert clean_state.submit_calls == []

    @pytest.mark.asyncio
    async def test_invalid_target_is_known_pre_send_failure(self, clean_state):
        clean_state.resolve_error = ValueError("无效作品链接")
        result = await _post()
        assert result["ok"] is False
        assert result["sent_unknown"] is False
        assert clean_state.submit_calls == []

    @pytest.mark.asyncio
    async def test_connection_error_discards_bot(self, clean_state):
        clean_state.resolve_error = RuntimeError("Target closed")
        result = await _post()
        assert result["ok"] is False
        assert kuaishou_comment_tools._bot is None
        assert clean_state.closed is True

    @pytest.mark.asyncio
    async def test_uncertain_submit_is_not_retried(self, clean_state):
        clean_state.submit_error = KuaishouCommentError(
            "连接在提交后关闭", sent_unknown=True
        )
        result = await _post()
        assert result["ok"] is False
        assert result["sent_unknown"] is True
        assert len(clean_state.submit_calls) == 1

        blocked = await _post(content="第二条")
        assert blocked["ok"] is False
        assert blocked["retry_after_seconds"] > 0
        assert len(clean_state.submit_calls) == 1

    @pytest.mark.asyncio
    async def test_same_video_cooldown(self, clean_state):
        first = await _post()
        assert first["ok"] is True
        second = await _post(content="第二条")
        assert second["ok"] is False
        assert second["retry_after_seconds"] > 0
        assert len(clean_state.submit_calls) == 1


class TestTargetParsing:
    def test_full_url(self):
        assert parse_kuaishou_photo_id(
            f"https://www.kuaishou.com/short-video/{PHOTO_ID}?foo=1"
        ) == PHOTO_ID

    def test_bare_photo_id(self):
        assert parse_kuaishou_photo_id(PHOTO_ID) == PHOTO_ID

    @pytest.mark.parametrize(
        "value",
        [
            "https://evil.example/short-video/3x42trd2e3f6hgq",
            "https://www.kuaishou.com/short-video/热门视频",
            "https://www.kuaishou.com/brilliant",
        ],
    )
    def test_rejects_unsafe_or_semantic_url(self, value):
        with pytest.raises(ValueError):
            parse_kuaishou_photo_id(value)


class TestBotReadAndWriteShape:
    @pytest.mark.asyncio
    async def test_memory_cookie_is_reused_and_close_clears_it(self):
        bot = KuaishouCommentBot()
        bot._build_client_from_cookies(
            "userId=123;kuaishou.server.webday7_st=memory-only",
            {"userId": "123", "kuaishou.server.webday7_st": "memory-only"},
        )
        assert await bot.check_alive() is True
        assert bot._current_user_id == "123"
        assert bot._browser_context is None
        await bot.close()
        assert bot.client is None
        assert bot._current_user_id == ""
        assert await bot.check_alive() is False

    @pytest.mark.asyncio
    async def test_setup_disconnects_cdp_after_reading_cookie(self, monkeypatch):
        bot = KuaishouCommentBot()
        calls = []

        async def fake_connect():
            calls.append("connect")
            bot._browser_context = object()

        async def fake_read():
            calls.append("read")
            return (
                "userId=123;kuaishou.server.webday7_st=memory-only",
                {"userId": "123", "kuaishou.server.webday7_st": "memory-only"},
            )

        async def fake_disconnect():
            calls.append("disconnect")
            bot._browser_context = None

        monkeypatch.setattr(bot, "_connect_browser", fake_connect)
        monkeypatch.setattr(bot, "_read_cookies_from_browser", fake_read)
        monkeypatch.setattr(bot, "_disconnect_browser", fake_disconnect)
        await bot.setup()
        assert calls == ["connect", "read", "disconnect"]
        assert bot._current_user_id == "123"

    @pytest.mark.asyncio
    async def test_resolve_photo_target(self):
        class FakeClient:
            async def get_video_info(self, photo_id):
                return {
                    "visionVideoDetail": {
                        "author": {"id": "3xauthor"},
                        "photo": {
                            "id": photo_id,
                            "caption": "标题",
                            "expTag": "exp",
                        },
                        "commentLimit": {"canAddComment": True},
                    }
                }

        bot = KuaishouCommentBot()
        bot.client = FakeClient()
        bot._pass_token = "memory"
        target = await bot.resolve_photo_target(PHOTO_ID)
        assert target.photo_id == PHOTO_ID
        assert target.photo_author_id == "3xauthor"
        assert target.exp_tag == "exp"

    @pytest.mark.asyncio
    async def test_submit_comment_can_succeed_while_read_list_is_pending(self, monkeypatch):
        class FakeClient:
            async def add_comment(self, **kwargs):
                return {"result": 1, "commentId": "999", "status": 1}

        bot = KuaishouCommentBot()
        bot.client = FakeClient()
        bot._current_user_id = USER_ID

        async def not_visible(*args):
            return False

        monkeypatch.setattr(bot, "_verify_comment", not_visible)
        result = await bot.submit_comment(TARGET, CONTENT)
        assert result["request_accepted"] is True
        assert result["ok"] is True
        assert result["sent"] is True
        assert result["sent_unknown"] is False
        assert result["self_checked"] is False
        assert result["list_sync_pending"] is True

    @pytest.mark.asyncio
    async def test_verify_rejects_matching_comment_from_another_user(self, monkeypatch):
        class FakeClient:
            async def get_video_comments(self, photo_id, pcursor=""):
                return {
                    "rootCommentsV2": [
                        {
                            "comment_id": "999",
                            "author_id": "someone-else",
                            "content": CONTENT,
                        }
                    ]
                }

        bot = KuaishouCommentBot()
        bot.client = FakeClient()
        bot._current_user_id = USER_ID
        monkeypatch.setattr(comment_bot_module, "VERIFY_ATTEMPTS", 1)
        assert await bot._verify_comment(PHOTO_ID, "999", CONTENT) is False

    @pytest.mark.asyncio
    async def test_verify_accepts_current_users_comment(self, monkeypatch):
        class FakeClient:
            async def get_video_comments(self, photo_id, pcursor=""):
                return {
                    "rootCommentsV2": [
                        {
                            "comment_id": "999",
                            "author_id": USER_ID,
                            "content": CONTENT,
                        }
                    ]
                }

        bot = KuaishouCommentBot()
        bot.client = FakeClient()
        bot._current_user_id = USER_ID
        monkeypatch.setattr(comment_bot_module, "VERIFY_ATTEMPTS", 1)
        assert await bot._verify_comment(PHOTO_ID, "999", CONTENT) is True

    @pytest.mark.asyncio
    async def test_client_add_comment_is_top_level_only(self):
        from media_platform.kuaishou.client import KuaiShouClient

        client = object.__new__(KuaiShouClient)
        client.graphql = SimpleNamespace(get=lambda name: f"query:{name}")
        captured = {}

        async def fake_post(uri, data):
            captured.update({"uri": uri, "data": data})
            return {"visionAddComment": {"result": 1, "commentId": "999"}}

        client.post = fake_post
        result = await client.add_comment(PHOTO_ID, "3xauthor", CONTENT, exp_tag="exp")
        assert result["commentId"] == "999"
        assert captured["data"]["operationName"] == "visionAddComment"
        assert captured["data"]["variables"] == {
            "photoId": PHOTO_ID,
            "photoAuthorId": "3xauthor",
            "content": CONTENT,
            "expTag": "exp",
        }


def test_tool_registered():
    names = {item.name for item in ALL_TOOLS}
    assert "post_kuaishou_comment" in names
    assert "reply_kuaishou_comment" not in names
