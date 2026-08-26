# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_dm.py
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

"""私信（DM）填框功能离线单测（不连 CDP、不发网络请求、不真实发送）。

用真实 DouyinCommentBot + 脚本化 FakePage/FakeLocator 测真实策略链逻辑，
重点防「Java 同步移植漏 await 导致检查静默失效」类回归。
"""

import argparse
import re

import pytest

from media_platform.douyin.comment_bot import (
    DouyinCommentBot,
    _normalize_text,
    _validate_dm_args,
)

SEC_UID = "MS4wLjABAAAA-test-sec-uid"

# ─── 脚本化替身 ───


class FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.press_events = []

    async def type(self, ch):
        self.page.typed += ch
        if self.page.typing_updates_text:
            self.page.sync_typed_to_editable()

    async def press(self, key):
        self.press_events.append(key)


class FakeLocator:
    """按 (selector, index, filters) 描述定位器，行为由 FakePage 的规则脚本决定。"""

    def __init__(self, owner, sel, index=None, filters=None):
        self.owner = owner
        self.sel = sel
        self.index = index  # None=全部 / int=第 n 个 / "last"=最后一个
        self.filters = list(filters or [])

    @property
    def first(self):
        return FakeLocator(self.owner, self.sel, index=0, filters=self.filters)

    @property
    def last(self):
        return FakeLocator(self.owner, self.sel, index="last", filters=self.filters)

    def nth(self, i):
        return FakeLocator(self.owner, self.sel, index=i, filters=self.filters)

    def filter(self, **kwargs):
        flt = list(self.filters)
        for k, v in kwargs.items():
            if v is not None:
                flt.append((k, v))
        return FakeLocator(self.owner, self.sel, index=self.index, filters=flt)

    async def count(self):
        return self.owner.resolve_count(self)

    async def is_visible(self):
        return self.owner.resolve(self).get("visible", False)

    async def is_enabled(self):
        return self.owner.resolve(self).get("enabled", True)

    async def fill(self, text):
        spec = self.owner.resolve(self)
        if spec.get("fill_ignored"):
            return  # 模拟受控组件吞掉合成输入
        # zero_width：模拟抖音 IM 输入框实际内容末尾带零宽空格（2026-08-24 真机发现）
        written = text + "​" if spec.get("zero_width") else text
        spec["inner_text"] = written
        spec["text"] = written
        self.owner.fills.append((self.sel, self.index, text))

    async def inner_text(self):
        return self.owner.resolve(self).get("inner_text", "")

    async def text_content(self):
        return self.owner.resolve(self).get("text", "")

    async def click(self, force=False, timeout=None):
        self.owner.clicks.append((self.sel, self.index))

    async def hover(self):
        self.owner.hovers.append((self.sel, self.index))

    async def press(self, key):
        self.owner.press_events.append((self.sel, self.index, key))

    async def evaluate(self, js):
        self.owner.evals.append((self.sel, self.index, js))
        return None


class FakePage:
    def __init__(self):
        self.url = "https://www.douyin.com/user/" + SEC_UID
        self.title_text = ""
        self.rules = []  # [(match_fn(locator), spec)]
        self.fills = []
        self.clicks = []
        self.hovers = []
        self.press_events = []  # locator 级按键（如 contenteditable 内按 Enter）
        self.evals = []  # evaluate 调用（如原生 click 发送按钮）
        self.evaluates = []  # page 级 evaluate 调用（如发送自检的全页文本搜索）
        self.evaluate_result = False  # page.evaluate 的脚本返回值
        self.typed = ""
        self.typing_updates_text = False
        self.closed = False
        self.goto_applies_url = True
        self.keyboard = FakeKeyboard(self)

    # --- 脚本化配置 ---
    def rule(self, match_fn, **spec):
        self.rules.append((match_fn, spec))
        return spec

    def contenteditable(self, count=0, visible=True, inner_text="", fill_ignored=False, **extra):
        return self.rule(lambda l: l.sel == 'div[contenteditable="true"]',
                         count=count, visible=visible, inner_text=inner_text,
                         text=inner_text, fill_ignored=fill_ignored, **extra)

    def resolve(self, locator):
        for fn, spec in self.rules:
            if fn(locator):
                return spec
        return {"count": 0, "visible": False}

    def resolve_count(self, locator):
        total = self.resolve(locator).get("count", 0)
        if locator.index is None:
            return total
        if locator.index == "last":
            return 1 if total > 0 else 0
        return 1 if 0 <= locator.index < total else 0

    def sync_typed_to_editable(self):
        """键盘逐字输入成功后，把可编辑元素的文本同步为已输入内容。"""
        for fn, spec in self.rules:
            if "inner_text" in spec:
                spec["inner_text"] = self.typed
                spec["text"] = self.typed

    # --- Page 接口 ---
    def locator(self, sel):
        return FakeLocator(self, sel)

    async def title(self):
        return self.title_text

    async def goto(self, url, wait_until=None, timeout=None):
        if self.goto_applies_url:
            self.url = url

    async def wait_for_timeout(self, ms):
        pass

    async def wait_for_url(self, pattern, timeout=None):
        if "/user/" not in self.url:
            raise TimeoutError("fake wait_for_url timeout")

    async def wait_for_load_state(self, state, timeout=None):
        raise TimeoutError("fake networkidle timeout（抖音常态）")

    async def wait_for_selector(self, sel, state=None, timeout=None):
        loc = self.locator(sel)
        if await loc.count() > 0 and await loc.is_visible():
            return
        raise TimeoutError("fake wait_for_selector timeout")

    async def evaluate(self, js, arg=None):
        self.evaluates.append((js, arg))
        return self.evaluate_result

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class FakePlaywright:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


def make_bot(page=None):
    bot = DouyinCommentBot()
    bot.page = page or FakePage()
    bot._playwright = FakePlaywright()
    bot._browser_context = None
    return bot


def _args(**kw):
    base = dict(video_url="", text="", reply_to="", dm_to="", dm_sec_uid="",
                dm_send=False, max_comments=30, dry_run=False, yes=False, cdp_port=None)
    base.update(kw)
    return argparse.Namespace(**base)


# ─── 参数校验 ───


class TestValidateDmArgs:

    def test_dm_to_and_dm_sec_uid_mutually_exclusive(self):
        assert "互斥" in _validate_dm_args(_args(dm_to="#1", dm_sec_uid="x"))

    def test_dm_and_reply_to_mutually_exclusive(self):
        assert "互斥" in _validate_dm_args(_args(video_url="v", dm_to="#1", reply_to="#2", text="t"))
        assert "互斥" in _validate_dm_args(_args(dm_sec_uid="x", reply_to="#2", text="t"))

    def test_dm_requires_text(self):
        assert "text" in _validate_dm_args(_args(dm_sec_uid="x"))
        assert "text" in _validate_dm_args(_args(video_url="v", dm_to="#1"))

    def test_dm_to_requires_video_url(self):
        assert "视频链接" in _validate_dm_args(_args(dm_to="#1", text="t"))

    def test_no_video_url_and_no_dm_sec_uid(self):
        assert "视频链接" in _validate_dm_args(_args())

    def test_dm_send_requires_dm_mode(self):
        assert "dm-send" in _validate_dm_args(_args(video_url="v", text="t", dm_send=True))

    def test_valid_combinations(self):
        assert _validate_dm_args(_args(video_url="v", text="t")) is None
        assert _validate_dm_args(_args(video_url="v", dm_to="#1", text="t")) is None
        assert _validate_dm_args(_args(dm_sec_uid="x", text="t")) is None
        assert _validate_dm_args(_args(video_url="v", dm_to="#1", text="t", dm_send=True)) is None


# ─── 填框策略链 ───


class TestFillDmBoxStrategy:

    @pytest.mark.asyncio
    async def test_scoped_selector_preferred(self):
        page = FakePage()
        page.contenteditable(count=3)  # 通用框 3 个
        page.rule(lambda l: l.sel == '[class*="chat-input"] div[contenteditable="true"]',
                  count=1, visible=True, inner_text="", text="")
        bot = make_bot(page)
        await bot._fill_dm_box("你好")
        assert page.fills == [('[class*="chat-input"] div[contenteditable="true"]', 0, "你好")]

    @pytest.mark.asyncio
    async def test_falls_back_to_last_contenteditable(self):
        page = FakePage()
        page.contenteditable(count=3, inner_text="")
        bot = make_bot(page)
        await bot._fill_dm_box("你好")
        assert page.fills == [('div[contenteditable="true"]', "last", "你好")]

    @pytest.mark.asyncio
    async def test_textarea_fallback(self):
        page = FakePage()
        page.rule(lambda l: l.sel == "textarea", count=2, visible=True, inner_text="", text="")
        bot = make_bot(page)
        await bot._fill_dm_box("你好")
        assert page.fills == [("textarea", "last", "你好")]

    @pytest.mark.asyncio
    async def test_human_type_fallback(self):
        # fill 被受控组件吞掉 → 键盘逐字输入兜底成功
        page = FakePage()
        page.typing_updates_text = True
        page.contenteditable(count=1, inner_text="", fill_ignored=True)
        bot = make_bot(page)
        await bot._fill_dm_box("你好")
        assert page.typed == "你好"

    @pytest.mark.asyncio
    async def test_self_check_failure_raises(self):
        # fill 被吞 + 逐字输入也不落文本 → 所有策略失败必须 raise（只填不送流程的唯一正确性闸门）
        page = FakePage()
        page.typing_updates_text = False
        page.contenteditable(count=1, inner_text="", fill_ignored=True)
        bot = make_bot(page)
        with pytest.raises(RuntimeError, match="未能填入"):
            await bot._fill_dm_box("你好")

    @pytest.mark.asyncio
    async def test_fill_dm_input_full_flow(self):
        # 完整链路：点私信按钮 → 等面板 → 填框（不发送）
        page = FakePage()
        page.rule(lambda l: l.sel == 'span:text-is("私信")', count=1, visible=True)
        page.contenteditable(count=1, inner_text="")
        bot = make_bot(page)
        await bot.fill_dm_input("你好")
        assert page.fills == [('div[contenteditable="true"]', "last", "你好")]
        assert any(ev == ('span:text-is("私信")', 0) for ev in page.clicks)
        assert page.keyboard.press_events == []  # 从未按 Enter，确保不发送

    @pytest.mark.asyncio
    async def test_zero_width_char_in_actual_passes_self_check(self):
        # 真机回归：抖音输入框实际内容末尾带 U+200B，自检必须剔除后比对（否则误判失败并重复填写）
        page = FakePage()
        page.contenteditable(count=1, inner_text="", zero_width=True)
        bot = make_bot(page)
        await bot._fill_dm_box("你好")
        assert page.fills == [('div[contenteditable="true"]', "last", "你好")]  # 只填一次，不触发逐字兜底


# ─── 内容比对 normalize ───


class TestNormalizeText:

    def test_strips_zero_width_chars(self):
        assert _normalize_text("你好​") == "你好"
        assert _normalize_text("你​‌好﻿") == "你好"

    def test_collapses_whitespace(self):
        assert _normalize_text("你 好\n\n吗") == "你 好 吗"


# ─── 用户不存在判定 ───


class TestIsUserNotExist:

    @pytest.mark.asyncio
    async def test_empty_title(self):
        page = FakePage()
        page.title_text = ""
        bot = make_bot(page)
        assert await bot._is_user_not_exist() is True

    @pytest.mark.asyncio
    async def test_not_exist_text(self):
        page = FakePage()
        page.title_text = "抖音"
        page.rule(lambda l: l.sel == "div, span, button, p, h1, h2, h3"
                  and any(k == "has_text" and isinstance(v, re.Pattern) for k, v in l.filters),
                  count=1, visible=True, text="该用户已注销")
        bot = make_bot(page)
        assert await bot._is_user_not_exist() is True

    @pytest.mark.asyncio
    async def test_profile_buttons_present(self):
        page = FakePage()
        page.title_text = "张三 的抖音 - 抖音"
        page.rule(lambda l: l.sel == 'button:has-text("关注"), button:has-text("私信"), button:has-text("分享主页")',
                  count=1, visible=True)
        bot = make_bot(page)
        assert await bot._is_user_not_exist() is False


# ─── 等待聊天面板 ───


class TestWaitDmPanel:

    @pytest.mark.asyncio
    async def test_input_appears_returns(self):
        page = FakePage()
        page.url = "https://www.douyin.com/im/chat"
        page.contenteditable(count=1)
        bot = make_bot(page)
        await bot._wait_dm_panel(timeout_ms=1000)  # 不抛错即通过

    @pytest.mark.asyncio
    async def test_login_modal_raises(self):
        page = FakePage()
        page.rule(lambda l: l.sel == '[class*="login-mask"]', count=1, visible=True)
        bot = make_bot(page)
        with pytest.raises(RuntimeError, match="登录"):
            await bot._wait_dm_panel(timeout_ms=1000)

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        page = FakePage()
        bot = make_bot(page)
        with pytest.raises(RuntimeError, match="聊天面板"):
            await bot._wait_dm_panel(timeout_ms=100)


# ─── 提交私信 ───


class TestSubmitDm:

    @pytest.mark.asyncio
    async def test_enter_on_last_contenteditable(self):
        page = FakePage()
        page.contenteditable(count=2)
        bot = make_bot(page)
        await bot.submit_dm()
        assert page.press_events == [('div[contenteditable="true"]', "last", "Enter")]
        assert page.evals == []

    @pytest.mark.asyncio
    async def test_send_button_native_click(self):
        page = FakePage()
        send_sel = ('[class*="chat-input"] button:text-is("发送"), [class*="chat-input"] span:text-is("发送"), '
                    '[class*="chat-input"] [class*="send"], [class*="chat-input"] [class*="submit"]')
        page.rule(lambda l: l.sel == send_sel, count=1, visible=True, enabled=True)
        bot = make_bot(page)
        await bot.submit_dm()
        assert page.evals == [(send_sel, 0, "el => el.click()")]

    @pytest.mark.asyncio
    async def test_global_enter_fallback(self):
        page = FakePage()
        bot = make_bot(page)
        await bot.submit_dm()
        assert page.keyboard.press_events == ["Enter"]


# ─── 发送后自检 ───


class TestSelfCheckDmSent:

    @pytest.mark.asyncio
    async def test_found_in_message_items(self, capsys):
        page = FakePage()
        page.rule(lambda l: l.sel == '[class*="message-item"]'
                  and any(k == "has_text" for k, v in l.filters),
                  count=1, visible=True)
        bot = make_bot(page)
        await bot._self_check_dm_sent("你好")
        assert "自检通过" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_not_found_warns(self, capsys):
        page = FakePage()
        bot = make_bot(page)
        await bot._self_check_dm_sent("你好")
        assert "人工确认" in capsys.readouterr().out

    @pytest.mark.asyncio
    async def test_found_via_js_page_search(self, capsys):
        # 真机回归：消息出现在会话列表预览里，选择器不匹配，靠 JS 全页文本搜索兜底
        page = FakePage()
        page.evaluate_result = True
        bot = make_bot(page)
        await bot._self_check_dm_sent("感谢开源")
        assert "自检通过" in capsys.readouterr().out
        assert len(page.evaluates) == 1
        assert page.evaluates[0][1] == "感谢开源"  # 传入的是 normalize 后的 needle


# ─── close 语义 ───


class TestCloseKeepPage:

    @pytest.mark.asyncio
    async def test_close_keep_page(self):
        page, aux = FakePage(), FakePage()
        bot = make_bot(page)
        bot._dm_aux_pages = [aux]
        await bot.close(close_page=False)
        assert page.closed is False
        assert aux.closed is False
        assert bot._playwright.stopped is True
        assert bot._dm_aux_pages == []

    @pytest.mark.asyncio
    async def test_close_default_closes_pages(self):
        page, aux = FakePage(), FakePage()
        bot = make_bot(page)
        bot._dm_aux_pages = [aux]
        await bot.close()
        assert page.closed is True
        assert aux.closed is True
        assert bot._playwright.stopped is True


# ─── 打开主页检查顺序 ───


class TestOpenUserProfileChecks:

    @pytest.mark.asyncio
    async def test_captcha_checked_first(self):
        page = FakePage()
        page.goto_applies_url = False
        page.url = "https://www.douyin.com/"
        page.rule(lambda l: l.sel == ".secsdk-captcha-drag-wrapper", count=1, visible=True)
        bot = make_bot(page)
        with pytest.raises(RuntimeError, match="验证码"):
            await bot.open_user_profile(SEC_UID)

    @pytest.mark.asyncio
    async def test_not_logged_in_raises(self):
        page = FakePage()
        page.goto_applies_url = False
        page.url = "https://www.douyin.com/"  # 无 /user/ URL 兜底 → 判定未登录
        bot = make_bot(page)
        with pytest.raises(RuntimeError, match="登录"):
            await bot.open_user_profile(SEC_UID)

    @pytest.mark.asyncio
    async def test_user_not_exist_raises(self):
        page = FakePage()
        page.title_text = "抖音"  # 非空标题 + 无 profile 按钮 → 用户不存在
        bot = make_bot(page)  # url 含 /user/ → _is_logged_in 走 URL 兜底为 True
        with pytest.raises(RuntimeError, match="用户不存在"):
            await bot.open_user_profile(SEC_UID)

    @pytest.mark.asyncio
    async def test_returns_username(self):
        page = FakePage()
        page.title_text = "张三 的抖音 - 抖音"
        page.rule(lambda l: l.sel == 'button:has-text("关注"), button:has-text("私信"), button:has-text("分享主页")',
                  count=1, visible=True)
        page.rule(lambda l: l.sel == '[data-e2e="user-name"]', count=1, visible=True, text="张三")
        bot = make_bot(page)
        name = await bot.open_user_profile(SEC_UID)
        assert name == "张三"
