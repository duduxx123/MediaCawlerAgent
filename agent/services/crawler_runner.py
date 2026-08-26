# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/services/crawler_runner.py
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
爬虫子进程 Runner —— 以子进程方式运行 `main.py`（复用项目现有 CLI / 登录 / 存储全链路），
等待完成后读取落盘数据，返回结构化摘要供 LLM 消费。

依赖白名单：仅标准库。本模块绝不 import config / main / cmd_arg / media_platform，
所有爬取参数经子进程命令行传递，保证本进程零全局配置污染。
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

# 工具友好名 -> main.py CLI 的 --platform 取值
PLATFORM_CLI_KEY = {"douyin": "dy", "xhs": "xhs", "bilibili": "bili"}
# 平台别名（LLM 可能使用 CLI 缩写或中文名）-> CLI key
PLATFORM_ALIASES = {
    "douyin": "dy", "dy": "dy", "抖音": "dy",
    "xhs": "xhs", "小红书": "xhs",
    "bilibili": "bili", "bili": "bili", "b站": "bili", "哔哩哔哩": "bili",
}
# CLI key -> data/ 目录名（与 store/*/_store_impl.py 中 AsyncFileWriter(platform=...) 保持一致）
PLATFORM_DATA_DIR = {
    "dy": "douyin",
    "xhs": "xhs",
    "bili": "bili",
    "ks": "kuaishou",
    "wb": "weibo",
    "tieba": "tieba",
    "zhihu": "zhihu",
}

PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"

DEFAULT_TIMEOUT = 900.0  # 秒，可用环境变量 AGENT_CRAWL_TIMEOUT_SECONDS 覆盖
MAX_LOG_LINES = 200
MAX_LOG_CHARS = 50_000
MAX_COUNT_LINES = 200_000  # jsonl 逐行计数的行数上限
SAMPLE_READ_LINES = 200  # 抽样时最多读取的行数

# 摘要抽取候选键（各平台字段名不同，不按平台硬编码）
TITLE_KEYS = ("title", "note_title", "desc")
URL_KEYS = ("video_url", "note_url", "aweme_url", "homepage_url", "detail_url", "url")
AUTHOR_KEYS = ("nickname", "author", "author_name", "user_nickname", "nick_name")
LIKE_KEYS = ("liked_count", "like_count", "video_play_count")
COMMENT_KEYS = ("comment_count", "comments_count", "comment_num", "video_comment_count")

LOGIN_HINT_KEYWORDS = ("扫码", "登录", "二维码", "login")

# 全局锁：同一进程内串行化所有爬取任务（CDP 调试端口冲突风险）
_crawl_lock = asyncio.Lock()


def _default_timeout() -> float:
    raw = os.environ.get("AGENT_CRAWL_TIMEOUT_SECONDS", "")
    try:
        return float(raw) if raw else DEFAULT_TIMEOUT
    except ValueError:
        return DEFAULT_TIMEOUT


def normalize_platform(platform: str) -> str:
    """平台名（友好名/CLI 缩写/中文名）-> CLI key，非法值抛可操作的 ValueError（LLM 可据此修正参数）。"""
    key = PLATFORM_ALIASES.get(str(platform).strip().lower())
    if key is None:
        raise ValueError(
            f"不支持的平台 '{platform}'，可选值: douyin(抖音)/xhs(小红书)/bilibili(B站)，"
            f"也接受缩写 dy/bili"
        )
    return key


def build_command(
    *,
    platform: str,
    crawler_type: str,
    keywords: Optional[str] = None,
    specified_ids: Optional[str] = None,
    creator_urls: Optional[str] = None,
    start_page: int = 1,
    enable_comments: bool = True,
    enable_sub_comments: bool = False,
    max_notes: Optional[int] = None,
    max_comments_per_note: Optional[int] = None,
    save_option: str = "jsonl",
    headless: bool = False,
) -> Optional[List[str]]:
    """构造 main.py 子进程命令（纯函数，可单测）。参数与模式不匹配时返回 None。"""
    cli_key = normalize_platform(platform)
    cmd: List[str] = [
        sys.executable or "python",
        "main.py",
        "--platform", cli_key,
        "--lt", "qrcode",
        "--type", crawler_type,
        "--save_data_option", save_option,
    ]

    if crawler_type == "search" and keywords:
        cmd += ["--keywords", keywords]
    elif crawler_type == "detail" and specified_ids:
        cmd += ["--specified_id", specified_ids]
    elif crawler_type == "creator" and creator_urls:
        cmd += ["--creator_id", creator_urls]
    else:
        return None

    if start_page > 1:
        cmd += ["--start", str(start_page)]
    cmd += ["--get_comment", "true" if enable_comments else "false"]
    cmd += ["--get_sub_comment", "true" if enable_sub_comments else "false"]
    if max_notes is not None:
        cmd += ["--crawler_max_notes_count", str(max_notes)]
    if max_comments_per_note is not None:
        cmd += ["--max_comments_count_singlenotes", str(max_comments_per_note)]
    if headless:
        cmd += ["--headless", "true"]
    return cmd


class _LogSink:
    """有界日志收集：行数/字符数双上限，超限丢弃最旧内容。"""

    def __init__(self) -> None:
        self._lines: List[str] = []
        self._chars = 0

    def append(self, line: str) -> None:
        self._lines.append(line)
        self._chars += len(line)
        while len(self._lines) > MAX_LOG_LINES or self._chars > MAX_LOG_CHARS:
            dropped = self._lines.pop(0)
            self._chars -= len(dropped)

    def tail(self, n: int = 40) -> str:
        return "\n".join(self._lines[-n:])

    def text(self) -> str:
        return "\n".join(self._lines)


def extract_compact_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """从一条原始记录中按候选键抽取紧凑摘要（title/url/author/likes/comments）。"""
    compact: Dict[str, Any] = {}

    for key in TITLE_KEYS:
        if key in record and record[key]:
            title = str(record[key]).strip()
            if title:
                compact["title"] = title[:80] if key == "desc" else title[:120]
                break
    for key in URL_KEYS:
        if key in record and record[key]:
            compact["url"] = str(record[key])
            break
    for key in AUTHOR_KEYS:
        if key in record and record[key]:
            compact["author"] = str(record[key])
            break
    for key in LIKE_KEYS:
        if key in record and record[key] not in (None, ""):
            compact["likes"] = record[key]
            break
    for key in COMMENT_KEYS:
        if key in record and record[key] not in (None, ""):
            compact["comments"] = record[key]
            break
    return compact


def _count_records(path: Path) -> Optional[int]:
    """统计文件记录数：jsonl 逐行、json 取 list 长度、csv 行数减表头，其余返回 None。"""
    try:
        if path.suffix == ".jsonl":
            count = 0
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                for _ in f:
                    count += 1
                    if count >= MAX_COUNT_LINES:
                        break
            return count
        if path.suffix == ".json":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            return len(data) if isinstance(data, list) else None
        if path.suffix == ".csv":
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                return max(0, sum(1 for _ in f) - 1)
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    return None


def _snapshot_sizes(base_dir: Path, crawler_type: str, save_option: str) -> Dict[str, int]:
    """抓取前记录相关数据文件的字节大小基线（append 模式下靠 size 增量识别本次新增数据）。"""
    snap: Dict[str, int] = {}
    if not base_dir.is_dir():
        return snap
    for item_type in ("contents", "comments"):
        for p in base_dir.glob(f"{crawler_type}_{item_type}_*.{save_option}"):
            try:
                snap[str(p)] = p.stat().st_size
            except OSError:
                pass
    return snap


def _read_new_records(path: Path, offset: int, sample_limit: int) -> tuple[int, List[Dict[str, Any]]]:
    """从文件 offset 字节处开始读 jsonl 新增行，返回 (新增行数, 样本列表)。

    data 文件按日期命名、append 追加，故 offset 之后的字节即本次抓取新增的记录。
    """
    count = 0
    samples: List[Dict[str, Any]] = []
    try:
        with open(path, "rb") as f:
            f.seek(offset)
            for raw in f:
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                count += 1
                if len(samples) < sample_limit:
                    try:
                        record = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    compact = extract_compact_record(record)
                    if compact.get("title") or compact.get("url"):
                        samples.append(compact)
    except OSError:
        pass
    return count, samples


def _collect_outputs(
    platform_cli_key: str,
    crawler_type: str,
    save_option: str,
    baseline: Dict[str, int],
    sample_limit: int,
) -> Dict[str, Any]:
    """对比抓取前基线，收集本次新增的产物：文件列表、新增记录数、新增样本。

    不区分 contents/comments 之外的逻辑，只报告 size 增长的文件；无新增即返回空，
    绝不回退到旧数据（避免把历史抓取结果误报为本次结果）。
    """
    base_dir = DATA_DIR / PLATFORM_DATA_DIR.get(platform_cli_key, platform_cli_key) / save_option
    files: List[Dict[str, Any]] = []
    total_records = 0
    samples: List[Dict[str, Any]] = []

    if not base_dir.is_dir():
        return {"files": files, "total_records": 0, "samples": samples}

    for item_type in ("contents", "comments"):
        for p in sorted(base_dir.glob(f"{crawler_type}_{item_type}_*.{save_option}"), key=lambda x: x.stat().st_mtime):
            try:
                new_size = p.stat().st_size
            except OSError:
                continue
            old_size = baseline.get(str(p), 0)
            if new_size <= old_size:
                continue  # 无新增

            count = 0
            if p.suffix == ".jsonl":
                is_contents = "_contents_" in p.name
                count, new_samples = _read_new_records(p, old_size, sample_limit - len(samples) if is_contents else 0)
                if is_contents:
                    samples.extend(new_samples)
                total_records += count
            else:
                # 非 jsonl（json/csv 整文件重写），无法可靠按 size 增量，粗略计 1 条新增
                count = None
                total_records += 1

            files.append({
                "path": str(p.relative_to(DATA_DIR)),
                "records": count,
                "size": new_size - old_size,
            })

    return {"files": files, "total_records": total_records, "samples": samples}


def is_crawling() -> bool:
    """是否已有爬取任务在执行（供 API status 使用）。"""
    return _crawl_lock.locked()


def _spawn(cmd: List[str]) -> subprocess.Popen:
    """启动子进程；解释器不存在时回退到 uv run python main.py。

    注意：Windows 下 asyncio.create_subprocess_exec 不支持 encoding 参数
    （报 "encoding must be None"），故沿用 api/services/crawler_manager.py
    的同步 Popen + run_in_executor 逐行读模式（项目已验证可用的 Windows 范式）。
    """
    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",   # main.py 已强制 stdout UTF-8
        errors="replace",
        bufsize=1,
        cwd=str(PROJECT_ROOT),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    try:
        return subprocess.Popen(cmd, **kwargs)
    except FileNotFoundError:
        fallback = ["uv", "run", "python", "main.py"] + cmd[2:]
        return subprocess.Popen(fallback, **kwargs)


async def run_crawl(
    platform: str,
    crawler_type: str,
    *,
    timeout: Optional[float] = None,
    sample_limit: int = 5,
    **params: Any,
) -> Dict[str, Any]:
    """运行一次爬取子进程并返回结构化摘要。绝不抛异常，一切失败信息封装进返回字典。

    返回字段：ok / busy / timed_out / exit_code / message / log_tail /
              files[{path,records,size}] / total_records / samples / login_hint / existing
    """
    platform_cli_key = normalize_platform(platform)

    if _crawl_lock.locked():
        return {
            "ok": False,
            "busy": True,
            "message": "已有爬取任务正在进行中，请等待其完成后再发起新的爬取。",
        }

    cmd = build_command(platform=platform, crawler_type=crawler_type, **params)
    if cmd is None:
        return {
            "ok": False,
            "message": (
                "参数不完整：search 模式需要 keywords；"
                "detail 模式需要 ids_or_urls；creator 模式需要 creator_urls。"
            ),
        }

    timeout = timeout if timeout is not None else _default_timeout()
    save_option = params.get("save_option", "jsonl")
    async with _crawl_lock:
        sink = _LogSink()
        timed_out = False
        exit_code: Optional[int] = None

        # 抓取前记录数据文件基线，用于后续只报告本次新增的记录（同一天 append 到同一文件）
        base_dir = DATA_DIR / PLATFORM_DATA_DIR.get(platform_cli_key, platform_cli_key) / save_option
        baseline = _snapshot_sizes(base_dir, crawler_type, save_option)

        try:
            proc = _spawn(cmd)
        except Exception as e:
            return {"ok": False, "message": f"爬取进程启动失败: {type(e).__name__}: {e}"}

        drain_task = asyncio.create_task(_drain_output(proc, sink))
        try:
            try:
                exit_code = await asyncio.wait_for(
                    asyncio.get_running_loop().run_in_executor(None, proc.wait),
                    timeout,
                )
            except asyncio.TimeoutError:
                timed_out = True
                proc.kill()
                await asyncio.get_running_loop().run_in_executor(None, proc.wait)
                exit_code = proc.returncode
        finally:
            try:
                await asyncio.wait_for(drain_task, 10.0)
            except Exception:
                drain_task.cancel()

        log_text = sink.text()
        login_hint = any(kw in log_text.lower() for kw in LOGIN_HINT_KEYWORDS)

        outputs = _collect_outputs(platform_cli_key, crawler_type, save_option, baseline, sample_limit)

        if timed_out:
            return {
                "ok": False,
                "timed_out": True,
                "exit_code": exit_code,
                "message": f"爬取超时（超过 {timeout:.0f} 秒），已终止子进程。可能卡在登录或网络请求，请稍后重试或减少抓取数量。",
                "log_tail": sink.tail(40),
                "login_hint": login_hint,
            }

        if exit_code != 0:
            return {
                "ok": False,
                "exit_code": exit_code,
                "message": _diagnose_failure(log_text, exit_code),
                "log_tail": sink.tail(40),
                "login_hint": login_hint,
            }

        files = outputs["files"]
        total_records = outputs["total_records"]
        if not files:
            message = "爬取进程正常结束，但本次未抓取到新的数据（可能关键词无结果、被风控或去重跳过）。"
        else:
            message = (
                f"爬取完成：本次新增 {total_records} 条记录，"
                f"文件: {', '.join(f['path'] for f in files)}"
            )
        return {
            "ok": True,
            "exit_code": exit_code,
            "message": message,
            "files": files,
            "total_records": total_records,
            "samples": outputs["samples"],
            "login_hint": login_hint,
        }


def _diagnose_failure(log_text: str, exit_code: int) -> str:
    """根据日志内容识别常见失败模式，给出可操作的中文诊断建议。"""
    message = f"爬取失败（退出码 {exit_code}）。"
    lower = log_text.lower()
    suggestions = []
    if "cdp mode launch failed" in lower or "cdp" in lower and "404" in lower:
        suggestions.append(
            "CDP 模式连接浏览器失败：请确认 Chrome/Edge 已开启远程调试端口 9222"
            "（--remote-debugging-port=9222），或在 config/base_config.py 中把 ENABLE_CDP_MODE 设为 False"
        )
    if "timeout" in lower and "goto" in lower:
        suggestions.append("页面加载超时（30 秒）：请检查网络/代理是否可正常访问目标平台，稍后重试")
    if "风控" in log_text or "验证" in log_text:
        suggestions.append("触发平台风控/验证：建议降低抓取数量与频率、更换 IP 或稍后重试")
    if suggestions:
        message += " 可能原因: " + "；".join(suggestions) + "。"
    return message


async def _drain_output(proc: subprocess.Popen, sink: _LogSink) -> None:
    """逐行收集子进程输出到有界日志 sink（stderr 已合并进 stdout）。

    阻塞的 readline 放到线程池，避免卡住事件循环。
    """
    if proc.stdout is None:
        return
    loop = asyncio.get_running_loop()
    try:
        while True:
            line = await loop.run_in_executor(None, proc.stdout.readline)
            if not line:
                break
            sink.append(line.rstrip("\r\n"))
    except (asyncio.CancelledError, ValueError, OSError):
        # 进程被 kill / 流关闭时的正常退出路径
        pass
