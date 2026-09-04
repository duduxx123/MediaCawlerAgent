# -*- coding: utf-8 -*-
"""
线索数据接口：读取本地 SQLite 数据库（database/sqlite_tables.db），合并评论与内容（视频），
供前端「评论线索 / 视频内容 / 评论词云」页面使用。

- GET  /api/leads/comments   -> 评论线索（评论 join 其所属视频，补关键词/链接/标题）
- GET  /api/leads/contents   -> 视频/内容记录
- POST /api/leads/comments/delete -> 批量删除评论（级联删除其二级回复）
- GET  /api/leads/wordclouds -> 列出词云图片与高频词（data/<平台>/words/）
- POST /api/leads/wordclouds/{platform}/generate -> 从数据库评论按需生成词云

数据量小，读接口一次性全量返回，筛选/分页由前端完成。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select

from agent.services.crawler_runner import is_crawling
from api.services.crawler_manager import crawler_manager
from database import models
from database.db_session import get_session

router = APIRouter(prefix="/leads", tags=["leads"])

# 数据目录：D:\MediaCrawler\data（词云图片仍落 data/<平台>/words/）
DATA_DIR = Path(__file__).parent.parent.parent / "data"

# 平台目录名 -> 中文名
PLATFORM_LABELS = {
    "douyin": "抖音",
    "dy": "抖音",
    "bili": "B站",
    "bilibili": "B站",
    "xhs": "小红书",
    "kuaishou": "快手",
    "ks": "快手",
    "weibo": "微博",
    "wb": "微博",
    "tieba": "贴吧",
    "zhihu": "知乎",
}

# 平台目录名 -> (内容模型, 评论模型, 内容ID字段, 评论所属内容ID字段)
PLATFORM_MODELS = {
    "douyin": (models.DouyinAweme, models.DouyinAwemeComment, "aweme_id", "aweme_id"),
    "bili": (models.BilibiliVideo, models.BilibiliVideoComment, "video_id", "video_id"),
    "xhs": (models.XhsNote, models.XhsNoteComment, "note_id", "note_id"),
    "kuaishou": (models.KuaishouVideo, models.KuaishouVideoComment, "video_id", "video_id"),
    "weibo": (models.WeiboNote, models.WeiboNoteComment, "note_id", "note_id"),
    "tieba": (models.TiebaNote, models.TiebaComment, "note_id", "note_id"),
    "zhihu": (models.ZhihuContent, models.ZhihuComment, "content_id", "content_id"),
}

# 内容 / 评论共用的 id 字段候选（兼容历史 jsonl 记录的字段名差异）
CONTENT_ID_KEYS = ("aweme_id", "video_id", "note_id", "content_id", "id")
COMMENT_ID_KEYS = ("aweme_id", "video_id", "note_id", "content_id", "id")
URL_KEYS = ("aweme_url", "video_url", "note_url", "content_url", "url")
TITLE_KEYS = ("title", "desc")


def _pick(record: Dict[str, Any], keys) -> Optional[Any]:
    """取记录中第一个存在且非空的字段值。"""
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _timestamp_sort_value(value: Any) -> float:
    """Normalize SQLite/JSON timestamp values before sorting.

    Historical files and ORM rows may expose the same timestamp as an int or a
    numeric string.  Returning one comparable type prevents mixed-type sort
    failures while keeping the original value in the API response unchanged.
    """
    if value in (None, ""):
        return 0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return 0


# 每平台键别名归一：让 _build_lead/_build_content 用统一的字段名读取各平台差异列
KEY_ALIASES = {
    "weibo": {"comment_like_count": "like_count", "comments_count": "comment_count", "shared_count": "share_count"},
    "tieba": {"publish_time": "create_time", "user_nickname": "nickname"},
    "zhihu": {"publish_time": "create_time", "created_time": "create_time", "content_url": "url",
              "user_nickname": "nickname"},
    "xhs": {"time": "create_time"},
}


def _row_to_dict(platform: str, row: Any) -> Dict[str, Any]:
    """ORM 行 -> 与 jsonl 记录同构的 dict（仅模型声明列 + 键别名归一，丢弃 None 列）。

    jsonl 时代的记录 dict 不含 null 字段，`xxx.get(k, default)` 的兜底依赖"键不存在"；
    douyin_id/red_id 会随 SQLite 镜像一起返回，其他平台仍按各自模型字段返回，
    _build_lead 会以空值兜底，保证响应形状不变。"""
    data = {
        c.name: value
        for c in row.__table__.columns
        if (value := getattr(row, c.name)) is not None
    }
    for src_key, dst_key in KEY_ALIASES.get(platform, {}).items():
        if src_key in data:
            data[dst_key] = data.pop(src_key)
    return data


async def _query_all(platform: str, model) -> List[Dict[str, Any]]:
    """全表查询并转为 dict 列表；数据库未初始化时返回空列表。

    硬编码 sqlite：本接口读镜像库，与 config.SAVE_DATA_OPTION（jsonl）无关。"""
    async with get_session(db_type="sqlite") as session:
        if session is None:
            return []
        result = await session.execute(select(model))
        return [_row_to_dict(platform, row) for row in result.scalars().all()]


def _build_lead(platform: str, comment: Dict[str, Any], content: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """评论 + 其所属内容 -> 单条线索记录。"""
    content = content or {}
    # 公开账号供用户阅读；内部 ID 单独保留给回复/私信等定位操作。
    if platform == "douyin":
        public_id = comment.get("douyin_id", "")
        internal_id = comment.get("sec_uid") or comment.get("creator_hash", "")
    elif platform == "xhs":
        public_id = comment.get("red_id", "")
        internal_id = comment.get("creator_hash", "")
    else:
        public_id = comment.get("creator_hash", "")
        internal_id = ""
    return {
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "keyword": content.get("source_keyword", ""),
        "video_id": _pick(content, CONTENT_ID_KEYS),
        "video_url": _pick(content, URL_KEYS) or "",
        "video_title": _pick(content, TITLE_KEYS) or "",
        "comment_id": comment.get("comment_id"),
        "commenter_name": comment.get("nickname", ""),
        # commenter_id 是旧接口兼容字段；新前端明确读取 public/internal 两列。
        "commenter_id": public_id or internal_id,
        "commenter_public_id": public_id,
        "commenter_internal_id": internal_id,
        "commenter_sec_uid": (
            comment.get("sec_uid") or comment.get("creator_hash", "")
            if platform == "douyin"
            else ""
        ),
        "comment": comment.get("content", ""),
        "like_count": comment.get("like_count", 0),
        "reply_count": comment.get("sub_comment_count", 0),
        "comment_time": comment.get("create_time"),
        "fetch_time": comment.get("last_modify_ts"),
        "pictures": comment.get("pictures", ""),
    }


def _build_content(platform: str, content: Dict[str, Any]) -> Dict[str, Any]:
    """内容（视频/笔记）记录 -> 展示字段。"""
    if platform == "douyin":
        creator_public_id = content.get("douyin_id", "")
        creator_internal_id = content.get("creator_hash", "")
    elif platform == "xhs":
        creator_public_id = content.get("red_id", "")
        creator_internal_id = content.get("creator_hash", "")
    else:
        creator_public_id = content.get("creator_hash", "")
        creator_internal_id = ""
    return {
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "keyword": content.get("source_keyword", ""),
        "id": _pick(content, CONTENT_ID_KEYS),
        "url": _pick(content, URL_KEYS) or "",
        "title": content.get("title", ""),
        "desc": content.get("desc", ""),
        "nickname": content.get("nickname", ""),
        "creator_hash": content.get("creator_hash", ""),
        "creator_public_id": creator_public_id,
        "creator_internal_id": creator_internal_id,
        "like_count": content.get("liked_count", ""),
        "collect_count": content.get("collected_count", ""),
        "comment_count": content.get("comment_count", content.get("video_comment", "")),
        "share_count": content.get("share_count", content.get("video_share_count", "")),
        "play_count": content.get("video_play_count", ""),
        "create_time": content.get("create_time"),
        "fetch_time": content.get("last_modify_ts"),
        "cover_url": content.get("cover_url", content.get("video_cover_url", "")),
    }


@router.get("/comments")
async def list_comment_leads():
    """评论线索：逐条评论 join 其所属视频，补上关键词/链接/标题。"""
    leads: List[Dict[str, Any]] = []

    for platform, (content_model, comment_model, content_id_attr, comment_id_attr) in PLATFORM_MODELS.items():
        contents = await _query_all(platform, content_model)
        content_index: Dict[str, Dict[str, Any]] = {}
        for content in contents:
            cid = content.get(content_id_attr)
            if cid is not None:
                content_index[str(cid)] = content

        for comment in await _query_all(platform, comment_model):
            cid = comment.get(comment_id_attr)
            content = content_index.get(str(cid)) if cid is not None else None
            leads.append(_build_lead(platform, comment, content))

    # 按评论时间倒序
    leads.sort(key=lambda item: _timestamp_sort_value(item.get("comment_time")), reverse=True)
    return {"total": len(leads), "leads": leads}


@router.get("/contents")
async def list_contents():
    """视频/内容记录。"""
    items: List[Dict[str, Any]] = []
    for platform, (content_model, _, _, _) in PLATFORM_MODELS.items():
        for content in await _query_all(platform, content_model):
            items.append(_build_content(platform, content))

    items.sort(key=lambda item: _timestamp_sort_value(item.get("create_time")), reverse=True)
    return {"total": len(items), "contents": items}


# ---------- 评论批量删除 ----------

MAX_DELETE_ITEMS = 1000


class CommentDeleteItem(BaseModel):
    platform: str
    comment_id: str = Field(min_length=1, max_length=255)


class CommentDeleteRequest(BaseModel):
    items: List[CommentDeleteItem] = Field(min_length=1, max_length=MAX_DELETE_ITEMS)


@router.post("/comments/delete")
async def delete_comments(payload: CommentDeleteRequest):
    """批量删除评论，并级联删除其二级回复（parent_comment_id 传递闭包）。"""
    if crawler_manager.status in ("running", "stopping") or is_crawling():
        raise HTTPException(status_code=409, detail="爬取任务进行中，请等待其完成后再删除")

    # 按平台分组（去重、保持顺序）
    groups: Dict[str, List[str]] = {}
    for item in payload.items:
        platform = item.platform.strip().lower()
        if platform not in PLATFORM_MODELS:
            raise HTTPException(status_code=400, detail=f"不支持的平台: {item.platform}")
        if item.comment_id.strip() == "0":
            # "0" 是顶层评论的哨兵值，删除它会级联删掉全平台所有顶层评论
            raise HTTPException(status_code=400, detail="comment_id 不能为 0（顶层评论哨兵值）")
        group = groups.setdefault(platform, [])
        if item.comment_id not in group:
            group.append(item.comment_id)

    deleted_total = 0
    cascaded_total = 0
    not_found: List[str] = []

    for platform, comment_ids in groups.items():
        _, comment_model, _, _ = PLATFORM_MODELS[platform]
        async with get_session(db_type="sqlite") as session:
            if session is None:
                raise HTTPException(status_code=500, detail="数据库未初始化，请检查 SQLite 镜像配置")

            # 命中检查（not_found 统计）
            existing_result = await session.execute(
                select(comment_model.comment_id).where(comment_model.comment_id.in_(comment_ids))
            )
            existing = {row[0] for row in existing_result.all()}
            not_found.extend(cid for cid in comment_ids if cid not in existing)

            # 级联：传递闭包收集所有以目标评论为祖先的回复（二级回复的回复也删）
            to_delete = set(comment_ids)
            if hasattr(comment_model, "parent_comment_id"):
                frontier = set(comment_ids)
                while frontier:
                    children_result = await session.execute(
                        select(comment_model.comment_id).where(comment_model.parent_comment_id.in_(frontier))
                    )
                    children = {row[0] for row in children_result.all()}
                    frontier = children - to_delete
                    to_delete |= children

            deleted_result = await session.execute(
                delete(comment_model).where(comment_model.comment_id.in_(to_delete))
            )
            deleted_total += deleted_result.rowcount or 0
            cascaded_total += max(0, (deleted_result.rowcount or 0) - len(existing))

    if deleted_total == 0:
        raise HTTPException(status_code=404, detail="未找到匹配的评论")

    return {"deleted": deleted_total - cascaded_total, "cascaded": cascaded_total, "not_found": not_found}


# ---------- 评论词云 ----------

def _safe_wordcloud_file(platform: str, filename: str) -> Path:
    """Resolve a word-cloud asset while preventing path traversal."""
    base = (DATA_DIR / platform / "words").resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base or candidate.suffix.lower() not in {".png", ".json"}:
        raise HTTPException(status_code=400, detail="Invalid word cloud file")
    return candidate


@router.post("/wordclouds/{platform}/generate")
async def generate_wordcloud(platform: str):
    """按需生成评论词云：读库评论 -> 词频 JSON + PNG 写入 data/<平台>/words/。
    生成器复用 tools/words.AsyncWordCloudGenerator（与 jsonl 模式同一套分词/绘图逻辑）。"""
    platform = platform.strip().lower()
    if platform not in PLATFORM_MODELS:
        raise HTTPException(status_code=400, detail=f"不支持的平台: {platform}")

    _, comment_model, _, _ = PLATFORM_MODELS[platform]
    filtered = []
    for row in await _query_all(platform, comment_model):
        text = row.get("content") or row.get("content_text") or ""
        if text:
            filtered.append({"content": text})
    if not filtered:
        raise HTTPException(status_code=404, detail="该平台暂无评论数据，无法生成词云")

    from tools.time_util import get_current_date
    from tools.words import AsyncWordCloudGenerator

    words_dir = DATA_DIR / platform / "words"
    words_dir.mkdir(parents=True, exist_ok=True)
    prefix = str(words_dir / f"search_comments_{get_current_date()}")

    try:
        await AsyncWordCloudGenerator().generate_word_frequency_and_cloud(filtered, prefix)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"词云生成失败: {e}")

    return {
        "generated": f"search_comments_{get_current_date()}_word_cloud.png",
        "comments": len(filtered),
    }


@router.get("/wordclouds")
async def list_wordclouds():
    """List generated comment word-cloud images and their top frequencies."""
    items = []
    if not DATA_DIR.exists():
        return {"total": 0, "wordclouds": []}

    for platform_dir in DATA_DIR.iterdir():
        words_dir = platform_dir / "words"
        if not platform_dir.is_dir() or not words_dir.is_dir():
            continue
        for image in sorted(words_dir.glob("*_word_cloud.png"), key=lambda p: p.stat().st_mtime, reverse=True):
            prefix = image.name[:-len("_word_cloud.png")]
            freq_file = words_dir / f"{prefix}_word_freq.json"
            frequencies = {}
            if freq_file.exists():
                try:
                    frequencies = json.loads(freq_file.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    frequencies = {}
            top_words = [
                {"word": str(word), "count": int(count)}
                for word, count in sorted(frequencies.items(), key=lambda pair: pair[1], reverse=True)[:20]
            ]
            items.append({
                "platform": platform_dir.name,
                "platform_label": PLATFORM_LABELS.get(platform_dir.name, platform_dir.name),
                "filename": image.name,
                "image_url": f"/api/leads/wordclouds/{platform_dir.name}/{image.name}",
                "created_at": image.stat().st_mtime,
                "top_words": top_words,
            })
    return {"total": len(items), "wordclouds": items}


@router.get("/wordclouds/{platform}/{filename}")
async def get_wordcloud_file(platform: str, filename: str):
    """Serve a generated word-cloud PNG or its frequency JSON."""
    file_path = _safe_wordcloud_file(platform, filename)
    if not file_path.exists() or not file_path.is_file():
        raise HTTPException(status_code=404, detail="Word cloud file not found")
    media_type = "image/png" if file_path.suffix.lower() == ".png" else "application/json"
    return FileResponse(file_path, media_type=media_type)
