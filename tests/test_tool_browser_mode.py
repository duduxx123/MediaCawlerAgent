# -*- coding: utf-8 -*-
"""Agent 浏览器工具必须遵循全局 CDP 模式，不得偷偷改连用户 Chrome。"""

import pytest

from media_platform.douyin import comment_bot as douyin_bot
from media_platform.xhs import dm_bot as xhs_bot


class _FakePage:
    def __init__(self):
        self.closed = False

    async def goto(self, *args, **kwargs):
        return None

    async def bring_to_front(self):
        return None

    def is_closed(self):
        return self.closed

    async def close(self):
        self.closed = True


class _FakeContext:
    def __init__(self):
        self.page = _FakePage()
        self.pages = [self.page]

    async def new_page(self):
        return self.page


class _FakeChromium:
    async def connect_over_cdp(self, *args, **kwargs):
        raise AssertionError("独立模式不应直接连接用户 Chrome")


class _FakePlaywright:
    def __init__(self):
        self.chromium = _FakeChromium()
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakePlaywrightStarter:
    def __init__(self, playwright):
        self.playwright = playwright

    async def start(self):
        return self.playwright


class _FakeManager:
    instances = []

    def __init__(self):
        self.context = _FakeContext()
        self.launch_platform = ""
        self.cleaned = False
        type(self).instances.append(self)

    async def launch_and_connect(self, *args, **kwargs):
        # 调用期间必须已切换到工具自己的 profile 平台。
        import config

        self.launch_platform = config.PLATFORM
        return self.context

    async def cleanup(self):
        self.cleaned = True


class _FakeDouyinClient:
    async def pong(self, browser_context=None):
        return True


class _FakeCrawler:
    async def create_douyin_client(self, httpx_proxy=None):
        return _FakeDouyinClient()


@pytest.mark.asyncio
async def test_douyin_tool_uses_own_profile_when_existing_mode_disabled(monkeypatch):
    _FakeManager.instances.clear()
    playwright = _FakePlaywright()
    monkeypatch.setattr(douyin_bot.config, "CDP_CONNECT_EXISTING", False)
    monkeypatch.setattr(douyin_bot.config, "PLATFORM", "xhs")
    monkeypatch.setattr(douyin_bot, "async_playwright", lambda: _FakePlaywrightStarter(playwright))
    monkeypatch.setattr(douyin_bot, "CDPBrowserManager", _FakeManager)
    monkeypatch.setattr(douyin_bot, "DouYinCrawler", _FakeCrawler)

    bot = douyin_bot.DouyinCommentBot()
    await bot.setup()

    manager = _FakeManager.instances[-1]
    assert manager.launch_platform == "dy"
    assert douyin_bot.config.CDP_CONNECT_EXISTING is False
    assert douyin_bot.config.PLATFORM == "xhs"

    await bot.close()
    assert manager.cleaned is True
    assert playwright.stopped is True


@pytest.mark.asyncio
async def test_xhs_tool_uses_own_profile_when_existing_mode_disabled(monkeypatch):
    _FakeManager.instances.clear()
    playwright = _FakePlaywright()
    monkeypatch.setattr(xhs_bot.config, "CDP_CONNECT_EXISTING", False)
    monkeypatch.setattr(xhs_bot.config, "PLATFORM", "dy")
    monkeypatch.setattr(xhs_bot, "async_playwright", lambda: _FakePlaywrightStarter(playwright))
    monkeypatch.setattr(xhs_bot, "CDPBrowserManager", _FakeManager)

    bot = xhs_bot.XiaohongshuDmBot()
    await bot.setup()

    manager = _FakeManager.instances[-1]
    assert manager.launch_platform == "xhs"
    assert xhs_bot.config.CDP_CONNECT_EXISTING is False
    assert xhs_bot.config.PLATFORM == "dy"

    await bot.close()
    assert manager.cleaned is True
    assert playwright.stopped is True
