# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests/test_agent.py
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

"""爬虫智能体离线单测（不发起网络请求、不启动子进程爬虫）。"""

import json
import sys

import pytest

from agent.services import crawler_runner
from agent.services.crawler_runner import (
    _collect_outputs,
    _crawl_lock,
    _snapshot_sizes,
    build_command,
    extract_compact_record,
    normalize_platform,
    run_crawl,
)
from agent.tools.crawl_tools import _format_result

BILI_RECORD = {
    "video_id": "123",
    "title": "35岁程序员副业复盘",
    "desc": "写了 10 年代码，折腾过 10 几个副业项目",
    "nickname": "袁某某",
    "liked_count": "684",
    "video_url": "https://www.bilibili.com/video/av123",
}

XHS_RECORD = {
    "note_id": "abc",
    "title": "探店合集",
    "note_url": "https://www.xiaohongshu.com/explore/abc",
    "nickname": "小红",
    "liked_count": "100",
    "comment_count": "25",
}


# ---------- build_command ----------

class TestBuildCommand:

    def test_search_command(self):
        cmd = build_command(
            platform="douyin", crawler_type="search",
            keywords="编程副业,程序员兼职", max_notes=5,
            enable_comments=True, enable_sub_comments=False,
            max_comments_per_note=10, start_page=2, save_option="jsonl", headless=True,
        )
        assert cmd is not None
        assert cmd[0] == sys.executable
        assert cmd[1] == "main.py"
        assert cmd[cmd.index("--platform") + 1] == "dy"
        assert cmd[cmd.index("--type") + 1] == "search"
        assert cmd[cmd.index("--keywords") + 1] == "编程副业,程序员兼职"
        assert cmd[cmd.index("--get_comment") + 1] == "true"
        assert cmd[cmd.index("--get_sub_comment") + 1] == "false"
        assert cmd[cmd.index("--crawler_max_notes_count") + 1] == "5"
        assert cmd[cmd.index("--max_comments_count_singlenotes") + 1] == "10"
        assert cmd[cmd.index("--start") + 1] == "2"
        assert "--headless" in cmd and cmd[cmd.index("--headless") + 1] == "true"

    def test_detail_command(self):
        cmd = build_command(platform="xhs", crawler_type="detail", specified_ids="https://a.com/1,https://a.com/2")
        assert cmd is not None
        assert cmd[cmd.index("--platform") + 1] == "xhs"
        assert cmd[cmd.index("--specified_id") + 1] == "https://a.com/1,https://a.com/2"

    def test_creator_command(self):
        cmd = build_command(platform="bilibili", crawler_type="creator", creator_urls="https://space.bilibili.com/1")
        assert cmd is not None
        assert cmd[cmd.index("--platform") + 1] == "bili"
        assert cmd[cmd.index("--creator_id") + 1] == "https://space.bilibili.com/1"

    @pytest.mark.parametrize(
        "platform,crawler_type,kwargs",
        [
            ("douyin", "search", {}),
            ("xhs", "detail", {}),
            ("bilibili", "creator", {}),
            ("douyin", "search", {"keywords": ""}),
        ],
    )
    def test_missing_mode_params_returns_none(self, platform, crawler_type, kwargs):
        assert build_command(platform=platform, crawler_type=crawler_type, **kwargs) is None

    def test_optional_flags_omitted_when_default(self):
        cmd = build_command(platform="xhs", crawler_type="search", keywords="a")
        assert cmd is not None
        assert "--crawler_max_notes_count" not in cmd
        assert "--headless" not in cmd
        assert "--start" not in cmd


# ---------- 平台映射 ----------

class TestPlatformMapping:

    @pytest.mark.parametrize("friendly,cli,data_dir", [
        ("douyin", "dy", "douyin"),
        ("xhs", "xhs", "xhs"),
        ("bilibili", "bili", "bili"),
    ])
    def test_mapping(self, friendly, cli, data_dir):
        assert normalize_platform(friendly) == cli
        assert crawler_runner.PLATFORM_DATA_DIR[cli] == data_dir

    @pytest.mark.parametrize("alias,cli", [
        ("dy", "dy"),
        ("bili", "bili"),
        ("抖音", "dy"),
        ("B站", "bili"),
        ("小红书", "xhs"),
    ])
    def test_platform_aliases(self, alias, cli):
        assert normalize_platform(alias) == cli

    def test_invalid_platform_raises(self):
        with pytest.raises(ValueError, match="不支持的平台"):
            normalize_platform("wechat")


# ---------- 摘要抽取 ----------

class TestExtractCompactRecord:

    def test_bilibili_record(self):
        compact = extract_compact_record(BILI_RECORD)
        assert compact["title"] == "35岁程序员副业复盘"
        assert compact["url"] == "https://www.bilibili.com/video/av123"
        assert compact["author"] == "袁某某"
        assert compact["likes"] == "684"

    def test_xhs_record(self):
        compact = extract_compact_record(XHS_RECORD)
        assert compact["title"] == "探店合集"
        assert compact["url"] == "https://www.xiaohongshu.com/explore/abc"
        assert compact["author"] == "小红"
        assert compact["likes"] == "100"
        assert compact["comments"] == "25"

    def test_desc_truncated_as_title_fallback(self):
        compact = extract_compact_record({"desc": "长" * 200})
        assert compact["title"] == "长" * 80


# ---------- 失败诊断 ----------

class TestDiagnoseFailure:

    def test_cdp_launch_failed(self):
        msg = crawler_runner._diagnose_failure("CDP mode launch failed, fallback to standard mode: HTTP 404", 1)
        assert "CDP" in msg and "9222" in msg

    def test_page_goto_timeout(self):
        msg = crawler_runner._diagnose_failure("playwright._impl._errors.TimeoutError: Page.goto: Timeout 30000ms exceeded.", 1)
        assert "页面加载超时" in msg

    def test_generic_failure(self):
        msg = crawler_runner._diagnose_failure("some random error", 2)
        assert msg == "爬取失败（退出码 2）。"


# ---------- run_crawl（离线路径） ----------

class TestRunCrawl:

    @pytest.mark.asyncio
    async def test_incomplete_params_no_subprocess(self):
        result = await run_crawl("xhs", "search")  # 无 keywords
        assert result["ok"] is False
        assert result.get("busy") is not True
        assert "keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_busy_when_lock_held(self):
        await _crawl_lock.acquire()
        try:
            result = await run_crawl("xhs", "search", keywords="a")
            assert result["ok"] is False
            assert result.get("busy") is True
        finally:
            _crawl_lock.release()

    @pytest.mark.asyncio
    async def test_invalid_platform(self):
        with pytest.raises(ValueError):
            await run_crawl("wechat", "search", keywords="a")


# ---------- _collect_outputs / 数据工具（临时目录离线） ----------

def _write_fake_data(tmp_path, platform_dir="douyin", filename="search_contents_2026-08-19.jsonl", records=None):
    """在临时目录构造 data/{platform_dir}/jsonl/{filename}。"""
    records = records or [BILI_RECORD, XHS_RECORD]
    data_dir = tmp_path / "data" / platform_dir / "jsonl"
    data_dir.mkdir(parents=True)
    path = data_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return path


class TestCollectOutputs:

    def test_collect_only_new_records(self, tmp_path, monkeypatch):
        """同一天 append 场景：基线记录旧 size，仅报告新增记录。"""
        monkeypatch.setattr(crawler_runner, "DATA_DIR", tmp_path / "data")
        path = _write_fake_data(tmp_path, records=[BILI_RECORD, XHS_RECORD])
        baseline = _snapshot_sizes(path.parent, "search", "jsonl")

        # 追加一条新记录
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps({**BILI_RECORD, "title": "新增第三条"}, ensure_ascii=False) + "\n")

        outputs = _collect_outputs("dy", "search", "jsonl", baseline, sample_limit=5)
        assert outputs["total_records"] == 1
        assert len(outputs["files"]) == 1
        assert outputs["files"][0]["records"] == 1
        assert outputs["samples"][0]["title"] == "新增第三条"

    def test_collect_no_growth_returns_empty(self, tmp_path, monkeypatch):
        """抓取未产生新数据时，绝不回退到旧数据。"""
        monkeypatch.setattr(crawler_runner, "DATA_DIR", tmp_path / "data")
        path = _write_fake_data(tmp_path, records=[BILI_RECORD, XHS_RECORD])
        baseline = _snapshot_sizes(path.parent, "search", "jsonl")

        outputs = _collect_outputs("dy", "search", "jsonl", baseline, sample_limit=5)
        assert outputs["total_records"] == 0
        assert outputs["files"] == []
        assert outputs["samples"] == []

    def test_collect_empty_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(crawler_runner, "DATA_DIR", tmp_path / "data")
        outputs = _collect_outputs("dy", "search", "jsonl", {}, sample_limit=5)
        assert outputs["total_records"] == 0
        assert outputs["files"] == []


class TestDataTools:

    @pytest.mark.asyncio
    async def test_read_crawled_data(self, tmp_path, monkeypatch):
        import agent.tools.data_tool as data_tool

        monkeypatch.setattr(data_tool, "DATA_DIR", tmp_path / "data")
        _write_fake_data(tmp_path, records=[BILI_RECORD, XHS_RECORD, {**BILI_RECORD, "title": "第三条"}])

        result = json.loads(await data_tool.read_crawled_data.ainvoke({"platform": "douyin", "limit": 2}))
        assert result["ok"] is True
        assert result["total"] == 2
        assert len(result["records"]) == 2
        # 尾部优先：最新追加的记录排在最前
        assert result["records"][0]["title"] == "第三条"

        filtered = json.loads(await data_tool.read_crawled_data.ainvoke({"platform": "douyin", "keyword_filter": "探店"}))
        assert filtered["ok"] is True
        assert filtered["total"] == 1
        assert filtered["records"][0]["title"] == "探店合集"

    @pytest.mark.asyncio
    async def test_read_crawled_data_no_file(self, tmp_path, monkeypatch):
        import agent.tools.data_tool as data_tool

        monkeypatch.setattr(data_tool, "DATA_DIR", tmp_path / "data")
        result = json.loads(await data_tool.read_crawled_data.ainvoke({"platform": "bilibili"}))
        assert result["ok"] is False
        assert "crawl_by_keywords" in result["message"]

    @pytest.mark.asyncio
    async def test_list_crawled_files(self, tmp_path, monkeypatch):
        import agent.tools.data_tool as data_tool

        monkeypatch.setattr(data_tool, "DATA_DIR", tmp_path / "data")
        _write_fake_data(tmp_path, platform_dir="douyin")
        _write_fake_data(tmp_path, platform_dir="bili", filename="search_contents_2026-08-18.jsonl")

        all_files = json.loads(await data_tool.list_crawled_files.ainvoke({"platform": ""}))
        assert all_files["ok"] is True
        assert all_files["total"] == 2

        only_douyin = json.loads(await data_tool.list_crawled_files.ainvoke({"platform": "douyin"}))
        assert only_douyin["total"] == 1
        assert "douyin" in only_douyin["files"][0]["path"]
        assert only_douyin["files"][0]["records"] == 2


# ---------- 工具结果格式化 ----------

class TestFormatResult:

    def test_success_format(self):
        result = _format_result(
            {
                "ok": True,
                "total_records": 3,
                "files": [{"path": "douyin/jsonl/search_contents_2026-08-19.jsonl", "records": 3, "size": 100}],
                "samples": [{"title": "t1"}],
                "existing": False,
                "login_hint": False,
            },
            "dy",
            "关键词搜索",
        )
        data = json.loads(result)
        assert data["ok"] is True
        assert data["platform"] == "抖音"
        assert data["total_records"] == 3
        assert "douyin/jsonl" in data["files"][0]

    def test_failure_with_login_hint(self):
        result = _format_result(
            {"ok": False, "message": "爬取失败（退出码 1）", "log_tail": "请扫码登录", "login_hint": True},
            "xhs",
            "关键词搜索",
        )
        data = json.loads(result)
        assert data["ok"] is False
        assert "扫码登录" in data["hint"]

    def test_busy_format(self):
        result = _format_result({"ok": False, "busy": True, "message": "已有爬取任务正在进行中"}, "bilibili", "详情抓取")
        data = json.loads(result)
        assert data["ok"] is False
        assert "已有爬取任务" in data["hint"]


# ---------- 工具错误格式化（ToolErrorMiddleware on_error） ----------

class TestToolErrorFormatter:

    def test_formatter_basic(self):
        from agent.services.agent_factory import _tool_error_formatter

        out = json.loads(_tool_error_formatter(RuntimeError("模拟崩溃")))
        assert out["ok"] is False
        assert "RuntimeError" in out["message"]
        assert "模拟崩溃" in out["message"]
        assert "hint" in out

    def test_formatter_with_request_includes_tool_name(self):
        from agent.services.agent_factory import _tool_error_formatter

        class FakeRequest:
            tool_call = {"name": "crawl_by_keywords"}

        out = json.loads(_tool_error_formatter(ValueError("参数错误"), FakeRequest()))
        assert "crawl_by_keywords" in out["message"]

    def test_formatter_truncates_long_message(self):
        from agent.services.agent_factory import _tool_error_formatter

        out = json.loads(_tool_error_formatter(RuntimeError("长" * 2000)))
        assert len(out["message"]) <= 400  # 截断到 300 字符 + 前缀


# ---------- 会话记忆（LangChain checkpointer） ----------

class TestChatStreamMemory:
    """离线测试 chat_stream 的 checkpointer 会话记忆逻辑（fake agent，不触网不落盘）。"""

    @staticmethod
    def _fake_msg(mtype: str, content: str):
        class _M:
            def model_dump(self):
                return {"type": mtype, "content": content}

        return _M()

    @staticmethod
    def _make_fake_agent(state_messages=None):
        state_messages = state_messages or []

        class FakeState:
            def __init__(self, messages):
                self.values = {"messages": messages}

        class FakeAgent:
            def __init__(self):
                self.state = FakeState(state_messages)
                self.inputs = []

            async def astream(self, payload, config=None):
                self.inputs.append((payload, config))
                yield {"agent": {"messages": [TestChatStreamMemory._fake_msg("ai", "你好")]}}

            async def aget_state(self, config=None):
                return self.state

        return FakeAgent()

    async def _collect(self, monkeypatch, fake_agent, message, history=None, thread_id="t1"):
        import agent.services.agent_factory as af

        async def fake_get_agent():
            return fake_agent

        monkeypatch.setattr(af, "get_agent", fake_get_agent)
        events = []
        async for e in af.chat_stream(message, history=history, thread_id=thread_id):
            events.append(e)
        return events

    @pytest.mark.asyncio
    async def test_memory_reuse_only_sends_new_message(self, monkeypatch):
        # checkpoint 已有该会话记忆 → 只发本轮消息，不再重发调用方 history
        agent = self._make_fake_agent([
            self._fake_msg("human", "旧问题"),
            self._fake_msg("ai", "旧回答"),
        ])
        bogus_history = [{"role": "user", "content": "过期历史不应重发"}]
        events = await self._collect(monkeypatch, agent, "新消息", history=bogus_history, thread_id="t1")

        payload, config = agent.inputs[0]
        assert len(payload["messages"]) == 1  # 只有本轮消息
        assert payload["messages"][0].content == "新消息"
        assert config["configurable"]["thread_id"] == "t1"
        done = [e for e in events if e["type"] == "done"]
        assert done and "新消息" not in str(done[0]["history"])  # history 读自查点（fake 不回写）

    @pytest.mark.asyncio
    async def test_no_memory_seeds_from_history(self, monkeypatch):
        # checkpoint 无记忆且调用方传了 history（旧前端兼容）→ 用 history 播种
        agent = self._make_fake_agent()  # 空记忆
        history = [
            {"role": "user", "content": "之前的问题"},
            {"role": "assistant", "content": "之前的回答"},
        ]
        await self._collect(monkeypatch, agent, "新消息", history=history)

        payload, _ = agent.inputs[0]
        assert len(payload["messages"]) == 3  # 播种的 2 条历史 + 本轮消息
        assert payload["messages"][0].content == "之前的问题"
        assert payload["messages"][2].content == "新消息"

    @pytest.mark.asyncio
    async def test_done_returns_checkpoint_history(self, monkeypatch):
        agent = self._make_fake_agent([
            self._fake_msg("human", "旧问题"),
            self._fake_msg("ai", "旧回答"),
        ])
        events = await self._collect(monkeypatch, agent, "新消息")
        done = [e for e in events if e["type"] == "done"][0]
        assert done["history"] == [
            {"role": "user", "content": "旧问题"},
            {"role": "assistant", "content": "旧回答"},
        ]

    @pytest.mark.asyncio
    async def test_clear_thread_with_in_memory_checkpointer(self, monkeypatch):
        import agent.services.agent_factory as af
        from langgraph.checkpoint.memory import InMemorySaver

        monkeypatch.setattr(af, "_checkpointer", InMemorySaver())
        assert await af.clear_thread("t1") is True


# ---------- 会话记忆自动摘要（SummarizationMiddleware 接线） ----------

class TestSummarizationMiddlewareWiring:

    def test_get_agent_wires_summarization_middleware(self, monkeypatch):
        """离线验证 get_agent 的接线：ToolError + Summarization 两个中间件、阈值参数正确。"""
        import asyncio
        from types import SimpleNamespace

        import langchain.agents as la
        import agent.services.agent_factory as af

        captured = {}

        class FakeModel:
            _llm_type = "openai-chat"  # SummarizationMiddleware 初始化时读取

            def with_retry(self):
                return self

        def fake_create_agent(model, tools, system_prompt, middleware, checkpointer, **kwargs):
            captured["model"] = model
            captured["middleware"] = middleware
            captured["checkpointer"] = checkpointer
            return object()

        monkeypatch.setattr(af, "_agent_cache", None)
        monkeypatch.setattr(af, "load_settings", lambda: SimpleNamespace(
            api_key="k", base_url="u", model="m", available=True,
            recursion_limit=25, sample_limit=5,
            summary_trigger_tokens=24000, summary_keep_messages=20,
        ))
        fake_model = FakeModel()
        monkeypatch.setattr(af, "create_chat_model", lambda: fake_model)

        async def fake_checkpointer():
            return "mem"

        monkeypatch.setattr(af, "_get_checkpointer", fake_checkpointer)
        monkeypatch.setattr(la, "create_agent", fake_create_agent)

        agent = asyncio.run(af.get_agent())
        assert agent is not None
        assert captured["model"] is fake_model
        assert captured["checkpointer"] == "mem"

        from langchain.agents.middleware import SummarizationMiddleware, ToolErrorMiddleware

        mw_types = [type(m) for m in captured["middleware"]]
        assert ToolErrorMiddleware in mw_types and SummarizationMiddleware in mw_types
        sum_mw = next(m for m in captured["middleware"] if isinstance(m, SummarizationMiddleware))
        assert sum_mw.trigger == ("tokens", 24000)
        assert sum_mw.keep == ("messages", 20)


# ---------- 工具注册 ----------

class TestToolRegistration:

    def test_all_tools_registered(self):
        from agent.tools import ALL_TOOLS

        names = {t.name for t in ALL_TOOLS}
        assert names == {
            "crawl_by_keywords", "crawl_specified_ids", "crawl_creator",
            "read_crawled_data", "list_crawled_files",
            "fetch_comment_users", "post_comment", "reply_comment", "send_dm_user",
        }
        for t in ALL_TOOLS:
            assert t.description
            assert t.args_schema is not None
