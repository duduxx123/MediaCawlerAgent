# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/agent/tools/crawl_tools.py
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
3 个爬取工具：关键词搜索 / 指定链接详情 / 创作者主页。
每个工具以子进程方式运行爬虫（见 services/crawler_runner.py），返回紧凑中文 JSON 字符串。
"""

import json
from typing import Any, Dict

from langchain_core.tools import tool
from pydantic import BaseModel, Field

from ..services.crawler_runner import normalize_platform, run_crawl

PLATFORM_LABELS = {"dy": "抖音", "xhs": "小红书", "bili": "B站"}
PLATFORM_DESC = "目标平台，可选值: douyin(抖音) / xhs(小红书) / bilibili(B站)，也接受缩写 dy/bili"


class CrawlByKeywordsArgs(BaseModel):
    """按关键词搜索抓取的参数"""

    platform: str = Field(description=PLATFORM_DESC)
    keywords: str = Field(description="搜索关键词，多个用英文逗号分隔，如'编程副业,程序员兼职'")
    max_notes: int = Field(default=20, ge=1, le=100, description="最多抓取条数(1-100)，越大耗时越长，请克制")
    enable_comments: bool = Field(default=True, description="是否抓取评论")
    enable_sub_comments: bool = Field(default=False, description="是否抓取二级评论")
    max_comments_per_note: int = Field(default=10, ge=1, le=100, description="单条内容最多抓取的评论数")
    start_page: int = Field(default=1, ge=1, le=100, description="起始页码")
    save_option: str = Field(default="jsonl", description="保存格式，默认 jsonl")


class CrawlSpecifiedIdsArgs(BaseModel):
    """按链接/ID 抓取详情（detail 模式）的参数"""

    platform: str = Field(description=PLATFORM_DESC)
    ids_or_urls: str = Field(description="内容链接或 ID，多个用英文逗号分隔（支持完整 URL 或纯 ID）")
    enable_comments: bool = Field(default=True, description="是否抓取评论")
    enable_sub_comments: bool = Field(default=False, description="是否抓取二级评论")
    max_comments_per_note: int = Field(default=10, ge=1, le=100, description="单条内容最多抓取的评论数")
    save_option: str = Field(default="jsonl", description="保存格式，默认 jsonl")


class CrawlCreatorArgs(BaseModel):
    """按创作者主页抓取作品（creator 模式）的参数"""

    platform: str = Field(description=PLATFORM_DESC)
    creator_urls: str = Field(description="创作者主页链接，多个用英文逗号分隔")
    enable_comments: bool = Field(default=True, description="是否抓取评论")
    enable_sub_comments: bool = Field(default=False, description="是否抓取二级评论")
    max_comments_per_note: int = Field(default=10, ge=1, le=100, description="单条内容最多抓取的评论数")
    save_option: str = Field(default="jsonl", description="保存格式，默认 jsonl")


def _normalize_platform_arg(platform: str) -> str:
    """归一化平台参数；非法值不抛异常，返回 None（由各工具转成错误 JSON 回传 LLM，供其自行纠正）。"""
    try:
        return normalize_platform(platform)
    except ValueError:
        return ""


def _format_result(result: Dict[str, Any], platform: str, mode: str) -> str:
    """把 runner 结果转成 LLM 易读的紧凑中文 JSON 字符串。"""
    label = PLATFORM_LABELS.get(platform, platform)
    out: Dict[str, Any] = {"ok": result.get("ok", False), "platform": label, "mode": mode}

    if result.get("busy"):
        out.update({
            "message": result.get("message"),
            "hint": "请告知用户当前已有爬取任务在执行，等待其完成后再重试。",
        })
        return json.dumps(out, ensure_ascii=False)

    if not result.get("ok"):
        out.update({
            "message": result.get("message"),
            "log_tail": result.get("log_tail", ""),
        })
        if result.get("timed_out"):
            out["hint"] = "抓取超时已终止。建议告知用户：减少 max_notes、稍后重试，或检查是否卡在浏览器登录。"
        elif result.get("login_hint"):
            out["hint"] = "日志涉及登录/扫码：首次使用该平台需要在浏览器中扫码登录一次（登录态会保存），请引导用户完成扫码后重试。"
        else:
            out["hint"] = "请如实告知用户抓取失败及原因，可建议降低抓取数量或稍后重试。"
        return json.dumps(out, ensure_ascii=False)

    out.update({
        "total_records": result.get("total_records", 0),
        "message": result.get("message", ""),
        "files": [f.get("path") for f in result.get("files", [])],
        "samples": result.get("samples", []),
    })

    hints: list = []
    if result.get("login_hint"):
        hints.append("首次使用该平台需扫码登录（登录态已保存）。")
    if result.get("files"):
        hints.append("评论数据保存在同目录 *_comments_* 文件中，可用 read_crawled_data 查看。")
    if hints:
        out["hint"] = " ".join(hints)
    return json.dumps(out, ensure_ascii=False)


@tool(args_schema=CrawlByKeywordsArgs)
async def crawl_by_keywords(
    platform: str,
    keywords: str,
    max_notes: int = 20,
    enable_comments: bool = True,
    enable_sub_comments: bool = False,
    max_comments_per_note: int = 10,
    start_page: int = 1,
    save_option: str = "jsonl",
) -> str:
    """按关键词搜索并抓取指定平台的图文/视频内容，保存到 data 目录。
抓取完成后返回文件路径、记录数与内容摘要。抓取是耗时的子进程任务（可能数分钟），调用后请耐心等待结果。"""
    platform = _normalize_platform_arg(platform)
    if not platform:
        return json.dumps({"ok": False, "message": "平台参数无效，可选值: douyin(抖音)/xhs(小红书)/bilibili(B站)，也接受缩写 dy/bili"}, ensure_ascii=False)
    result = await run_crawl(
        platform,
        "search",
        keywords=keywords,
        max_notes=max_notes,
        enable_comments=enable_comments,
        enable_sub_comments=enable_sub_comments,
        max_comments_per_note=max_comments_per_note,
        start_page=start_page,
        save_option=save_option,
    )
    return _format_result(result, platform, "关键词搜索")


@tool(args_schema=CrawlSpecifiedIdsArgs)
async def crawl_specified_ids(
    platform: str,
    ids_or_urls: str,
    enable_comments: bool = True,
    enable_sub_comments: bool = False,
    max_comments_per_note: int = 10,
    save_option: str = "jsonl",
) -> str:
    """按链接或 ID 抓取指定内容的详情（detail 模式）。支持平台完整链接或纯 ID，多个用英文逗号分隔。
抓取完成后返回文件路径、记录数与内容摘要。抓取是耗时的子进程任务，调用后请耐心等待结果。"""
    platform = _normalize_platform_arg(platform)
    if not platform:
        return json.dumps({"ok": False, "message": "平台参数无效，可选值: douyin(抖音)/xhs(小红书)/bilibili(B站)，也接受缩写 dy/bili"}, ensure_ascii=False)
    result = await run_crawl(
        platform,
        "detail",
        specified_ids=ids_or_urls,
        enable_comments=enable_comments,
        enable_sub_comments=enable_sub_comments,
        max_comments_per_note=max_comments_per_note,
        save_option=save_option,
    )
    return _format_result(result, platform, "详情抓取")


@tool(args_schema=CrawlCreatorArgs)
async def crawl_creator(
    platform: str,
    creator_urls: str,
    enable_comments: bool = True,
    enable_sub_comments: bool = False,
    max_comments_per_note: int = 10,
    save_option: str = "jsonl",
) -> str:
    """抓取指定创作者主页下的作品列表（creator 模式）。接受创作者主页链接，多个用英文逗号分隔。
抓取完成后返回文件路径、记录数与内容摘要。抓取是耗时的子进程任务，调用后请耐心等待结果。"""
    platform = _normalize_platform_arg(platform)
    if not platform:
        return json.dumps({"ok": False, "message": "平台参数无效，可选值: douyin(抖音)/xhs(小红书)/bilibili(B站)，也接受缩写 dy/bili"}, ensure_ascii=False)
    result = await run_crawl(
        platform,
        "creator",
        creator_urls=creator_urls,
        enable_comments=enable_comments,
        enable_sub_comments=enable_sub_comments,
        max_comments_per_note=max_comments_per_note,
        save_option=save_option,
    )
    return _format_result(result, platform, "创作者主页抓取")
