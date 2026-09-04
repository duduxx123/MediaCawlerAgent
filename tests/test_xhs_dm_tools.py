# -*- coding: utf-8 -*-
"""小红书私信工具离线测试：不连接 Chrome，也不真实发送消息。"""

import json

import pytest

from agent.tools import ALL_TOOLS
from agent.tools import xhs_dm_tools
from agent.tools.xhs_dm_tools import confirm_xhs_dm, prepare_xhs_dm
from media_platform.xhs.dm_bot import XiaohongshuDmBot, build_profile_url


USER_ID = "5eb8e1d400000000010075ae"
CONTENT = "你好，看到你在评论里提到想了解更多。"


class FakeBot:
    def __init__(self) -> None:
        self.alive = True
        self.prepare_calls = []
        self.submit_calls = []
        self.closed = False
        self.self_checked = True
        self.prepare_error = None
        self.submit_error = None
        self.resolve_calls = []

    async def setup(self) -> None:
        return None

    async def check_alive(self) -> bool:
        return self.alive

    async def close(self) -> None:
        self.closed = True

    async def prepare_dm(self, user_id, content, profile_url=None):
        if self.prepare_error:
            raise self.prepare_error
        self.prepare_calls.append((user_id, content, profile_url))
        return "小红薯用户"

    async def resolve_public_xhs_id(self, xiaohongshu_id):
        self.resolve_calls.append(xiaohongshu_id)
        return (
            USER_ID,
            "小红薯用户",
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_token=memory-only",
        )

    async def submit_dm(self, user_id, content):
        if self.submit_error:
            raise self.submit_error
        self.submit_calls.append((user_id, content))
        return self.self_checked


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    bot = FakeBot()
    monkeypatch.setattr(xhs_dm_tools, "_bot", bot)
    monkeypatch.setattr(xhs_dm_tools, "_pending", None)
    xhs_dm_tools._last_attempt_by_user.clear()
    monkeypatch.setattr(xhs_dm_tools.config, "XHS_SAVE_ORIGINAL_USER_INFO", True)
    yield bot
    xhs_dm_tools._last_attempt_by_user.clear()
    xhs_dm_tools._pending = None
    xhs_dm_tools._bot = None


async def _prepare(**overrides):
    args = {"user_id": USER_ID, "content": CONTENT, **overrides}
    return json.loads(await prepare_xhs_dm.ainvoke(args))


class TestPrepareXhsDm:

    @pytest.mark.asyncio
    async def test_prepares_without_sending(self, clean_state):
        result = await _prepare()
        assert result["ok"] is True
        assert result["sent"] is False
        assert result["requires_confirmation"] is True
        assert result["nickname"] == "小红薯用户"
        assert result["content"] == CONTENT
        assert result["draft_id"]
        assert clean_state.prepare_calls == [(USER_ID, CONTENT, None)]
        assert clean_state.submit_calls == []

    @pytest.mark.asyncio
    async def test_passes_optional_profile_url_without_echoing_token(self, clean_state):
        url = (
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}"
            "?xsec_token=secret-token&xsec_source=pc_feed"
        )
        result = await _prepare(profile_url=url)
        assert result["ok"] is True
        assert "secret-token" not in json.dumps(result)
        assert clean_state.prepare_calls[-1][2] == url

    @pytest.mark.asyncio
    async def test_public_xhs_id_is_resolved_in_memory(self, clean_state):
        result = await _prepare(user_id=None, xiaohongshu_id="5460254944")
        assert result["ok"] is True
        assert result["user_id"] == USER_ID
        assert result["xiaohongshu_id"] == "5460254944"
        assert "memory-only" not in json.dumps(result)
        assert clean_state.resolve_calls == ["5460254944"]
        assert clean_state.prepare_calls[-1] == (
            USER_ID,
            CONTENT,
            f"https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_token=memory-only",
        )

    @pytest.mark.asyncio
    async def test_public_xhs_id_works_when_database_identity_is_hashed(self, clean_state, monkeypatch):
        monkeypatch.setattr(xhs_dm_tools.config, "XHS_SAVE_ORIGINAL_USER_INFO", False)
        result = await _prepare(user_id=None, xiaohongshu_id="5460254944")
        assert result["ok"] is True

    @pytest.mark.asyncio
    async def test_rejects_hashed_identity_mode_without_profile_url(self, clean_state, monkeypatch):
        monkeypatch.setattr(xhs_dm_tools.config, "XHS_SAVE_ORIGINAL_USER_INFO", False)
        result = await _prepare()
        assert result["ok"] is False
        assert "不可逆哈希" in result["message"]
        assert clean_state.prepare_calls == []

    @pytest.mark.asyncio
    async def test_page_error_keeps_healthy_cdp_bot(self, clean_state):
        clean_state.prepare_error = RuntimeError("无法找到私信按钮")
        result = await _prepare()
        assert result["ok"] is False
        assert xhs_dm_tools._bot is clean_state

    @pytest.mark.asyncio
    async def test_connection_error_discards_bot_and_draft(self, clean_state):
        clean_state.prepare_error = RuntimeError("Target closed")
        result = await _prepare()
        assert result["ok"] is False
        assert xhs_dm_tools._bot is None
        assert xhs_dm_tools._pending is None
        assert clean_state.closed is True


class TestConfirmXhsDm:

    @pytest.mark.asyncio
    async def test_confirm_sends_exact_prepared_message_once(self, clean_state):
        prepared = await _prepare()
        result = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": prepared["draft_id"]}))
        assert result["ok"] is True
        assert result["sent"] is True
        assert result["self_checked"] is True
        assert clean_state.submit_calls == [(USER_ID, CONTENT)]
        assert xhs_dm_tools._pending is None

        repeated = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": prepared["draft_id"]}))
        assert repeated["ok"] is False
        assert clean_state.submit_calls == [(USER_ID, CONTENT)]

    @pytest.mark.asyncio
    async def test_invalid_draft_never_sends(self, clean_state):
        await _prepare()
        result = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": "wrong-draft-id"}))
        assert result["ok"] is False
        assert clean_state.submit_calls == []

    @pytest.mark.asyncio
    async def test_expired_draft_never_sends(self, clean_state, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(xhs_dm_tools.time, "monotonic", lambda: now[0])
        prepared = await _prepare()
        now[0] += xhs_dm_tools.DRAFT_TTL_SECONDS + 1
        result = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": prepared["draft_id"]}))
        assert result["ok"] is False
        assert "超过" in result["message"]
        assert clean_state.submit_calls == []

    @pytest.mark.asyncio
    async def test_uncertain_send_consumes_draft_and_does_not_retry(self, clean_state):
        clean_state.submit_error = RuntimeError("发送后出现验证码")
        prepared = await _prepare()
        result = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": prepared["draft_id"]}))
        assert result["ok"] is False
        assert result["sent_unknown"] is True
        assert xhs_dm_tools._pending is None

    @pytest.mark.asyncio
    async def test_same_user_cooldown_blocks_new_confirmation(self, clean_state):
        first = await _prepare()
        sent = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": first["draft_id"]}))
        assert sent["ok"] is True

        second = await _prepare(content="第二条")
        blocked = json.loads(await confirm_xhs_dm.ainvoke({"draft_id": second["draft_id"]}))
        assert blocked["ok"] is False
        assert blocked["retry_after_seconds"] > 0
        assert clean_state.submit_calls == [(USER_ID, CONTENT)]


class TestProfileUrlValidation:

    def test_raw_user_id_builds_safe_profile_url(self):
        assert build_profile_url(USER_ID) == f"https://www.xiaohongshu.com/user/profile/{USER_ID}"

    def test_rejects_external_host(self):
        with pytest.raises(ValueError, match="xiaohongshu.com"):
            build_profile_url(USER_ID, f"https://evil.example/user/profile/{USER_ID}")

    def test_rejects_mismatched_profile_user(self):
        other = "6eb8e1d400000000010075ae"
        with pytest.raises(ValueError, match="不一致"):
            build_profile_url(USER_ID, f"https://www.xiaohongshu.com/user/profile/{other}")

    def test_rejects_public_xhs_id_as_internal_user_id(self):
        with pytest.raises(ValueError, match="24 位"):
            build_profile_url("5460254944")


class TestLiveDmButtonSelector:

    @pytest.mark.asyncio
    async def test_clicks_real_icon_button_selector(self):
        """真机主页按钮没有文字，只能依赖 title/class；防止后续退回纯文本选择器。"""
        clicked = []

        class IconButton:
            async def count(self):
                return 1

            async def is_visible(self):
                return True

            async def is_enabled(self):
                return True

            async def click(self):
                clicked.append(True)

        class FakePage:
            def locator(self, selector):
                assert selector == 'button.xhs-user-im-btn[title="发消息"]'

                class Wrapper:
                    @property
                    def first(self):
                        return IconButton()

                return Wrapper()

        bot = XiaohongshuDmBot()
        bot.page = FakePage()
        await bot._click_dm_button()
        assert clicked == [True]


def test_tools_registered():
    names = {tool.name for tool in ALL_TOOLS}
    assert {"prepare_xhs_dm", "confirm_xhs_dm"} <= names
