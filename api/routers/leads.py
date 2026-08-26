# -*- coding: utf-8 -*-
"""
线索数据接口：读取 data/*/jsonl/*.jsonl，合并评论与内容（视频），供前端「评论线索 / 视频内容」页面使用。

- GET /api/leads/comments  -> 评论线索（评论 join 其所属视频，补关键词/链接/标题）
- GET /api/leads/contents  -> 视频/内容记录
数据量小，一次性全量返回，筛选/分页由前端完成。
"""
import json
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

router = APIRouter(prefix="/leads", tags=["leads"])

# 数据目录：D:\MediaCrawler\data
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

# 内容 / 评论共用的 id 字段候选（按平台不同，抖音 aweme_id、B站 video_id 等）
CONTENT_ID_KEYS = ("aweme_id", "video_id", "note_id", "id")
COMMENT_ID_KEYS = ("aweme_id", "video_id", "note_id", "id")
URL_KEYS = ("aweme_url", "video_url", "note_url", "url")
TITLE_KEYS = ("title", "desc")


def _pick(record: Dict[str, Any], keys) -> Optional[Any]:
    """取记录中第一个存在且非空的字段值。"""
    for key in keys:
        value = record.get(key)
        if value not in (None, ""):
            return value
    return None


def _load_jsonl(path: Path) -> List[Dict[str, Any]]:
    """解析单个 jsonl 文件，跳过空行与坏行。"""
    records: List[Dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    except (OSError, UnicodeDecodeError):
        return []
    return records


def _scan_platforms() -> Dict[str, Dict[str, List[Dict[str, Any]]]]:
    """扫描 data/*/jsonl/*.jsonl，返回 {platform: {"contents": [...], "comments": [...]}}。"""
    result: Dict[str, Dict[str, List[Dict[str, Any]]]] = {}
    if not DATA_DIR.exists():
        return result

    for plat_dir in DATA_DIR.iterdir():
        if not plat_dir.is_dir():
            continue
        jsonl_dir = plat_dir / "jsonl"
        if not jsonl_dir.is_dir():
            continue

        platform = plat_dir.name.lower()
        contents: List[Dict[str, Any]] = []
        comments: List[Dict[str, Any]] = []

        for fp in sorted(jsonl_dir.glob("*.jsonl")):
            if "_comments_" in fp.name:
                comments.extend(_load_jsonl(fp))
            elif "_contents_" in fp.name:
                contents.extend(_load_jsonl(fp))

        if contents or comments:
            result[platform] = {"contents": contents, "comments": comments}

    return result


def _build_lead(platform: str, comment: Dict[str, Any], content: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """评论 + 其所属内容 -> 单条线索记录。"""
    content = content or {}
    return {
        "platform": platform,
        "platform_label": PLATFORM_LABELS.get(platform, platform),
        "keyword": content.get("source_keyword", ""),
        "video_id": _pick(content, CONTENT_ID_KEYS),
        "video_url": _pick(content, URL_KEYS) or "",
        "video_title": _pick(content, TITLE_KEYS) or "",
        "comment_id": comment.get("comment_id"),
        "commenter_name": comment.get("nickname", ""),
        "commenter_id": comment.get("douyin_id", ""),
        "commenter_sec_uid": comment.get("sec_uid") or comment.get("creator_hash", ""),
        "comment": comment.get("content", ""),
        "like_count": comment.get("like_count", 0),
        "reply_count": comment.get("sub_comment_count", 0),
        "comment_time": comment.get("create_time"),
        "fetch_time": comment.get("last_modify_ts"),
        "pictures": comment.get("pictures", ""),
    }


def _build_content(platform: str, content: Dict[str, Any]) -> Dict[str, Any]:
    """内容（视频/笔记）记录 -> 展示字段。"""
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

    for platform, data in _scan_platforms().items():
        content_index: Dict[str, Dict[str, Any]] = {}
        for content in data["contents"]:
            cid = _pick(content, CONTENT_ID_KEYS)
            if cid is not None:
                content_index[str(cid)] = content

        for comment in data["comments"]:
            comment_id_key = _pick(comment, COMMENT_ID_KEYS)
            content = content_index.get(str(comment_id_key)) if comment_id_key is not None else None
            leads.append(_build_lead(platform, comment, content))

    # 按评论时间倒序
    leads.sort(key=lambda item: item.get("comment_time") or 0, reverse=True)
    return {"total": len(leads), "leads": leads}


@router.get("/contents")
async def list_contents():
    """视频/内容记录。"""
    items: List[Dict[str, Any]] = []
    for platform, data in _scan_platforms().items():
        for content in data["contents"]:
            items.append(_build_content(platform, content))

    items.sort(key=lambda item: item.get("create_time") or 0, reverse=True)
    return {"total": len(items), "contents": items}


def _safe_wordcloud_file(platform: str, filename: str) -> Path:
    """Resolve a word-cloud asset while preventing path traversal."""
    base = (DATA_DIR / platform / "words").resolve()
    candidate = (base / filename).resolve()
    if candidate.parent != base or candidate.suffix.lower() not in {".png", ".json"}:
        raise HTTPException(status_code=400, detail="Invalid word cloud file")
    return candidate


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
