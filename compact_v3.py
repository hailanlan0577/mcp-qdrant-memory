#!/usr/bin/env python3
"""
compact_v3.py — unified_memories_v3 对话记忆自动压缩
用法:
  python compact_v3.py                  # 压缩 30 天前的 conversation，dry_run=True
  python compact_v3.py --days 30 --run  # 实际执行
"""
import argparse
import hashlib
import os
import sys
import time
from datetime import datetime

# 绕过代理直连本地 Qdrant
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue, PointStruct, Range

# Embedding backend 切换:
#   local (默认): 本地 MLX daemon, 4096 维, collection=unified_memories_v3_local
#   dashscope   : 阿里云 v4, 1024 维, collection=unified_memories_v3
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "local").lower()
_IS_PROD = "--prod" in sys.argv

QDRANT_URL = "http://localhost:6333"
LOCAL_EMBED_URL = os.environ.get("LOCAL_EMBED_URL", "http://127.0.0.1:8765/embed")

if not _IS_PROD:
    # 测试模式始终用老 1024 维测试集合(避免污染 local)
    COLLECTION_NAME = "unified_memories_v3_test"
elif EMBED_BACKEND == "local":
    COLLECTION_NAME = "unified_memories_v3_local"
elif EMBED_BACKEND == "dashscope":
    COLLECTION_NAME = "unified_memories_v3"
else:
    raise ValueError(f"未知 EMBED_BACKEND: {EMBED_BACKEND}")

DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
DASHSCOPE_EMBED_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding"


def make_id(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def get_embedding(text: str) -> list[float]:
    if EMBED_BACKEND == "local":
        resp = httpx.post(LOCAL_EMBED_URL, json={"text": text, "text_type": "document"}, timeout=30)
        resp.raise_for_status()
        return resp.json()["embedding"]
    resp = httpx.post(
        DASHSCOPE_EMBED_URL,
        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
        json={"model": "text-embedding-v4", "input": {"texts": [text]}, "parameters": {"text_type": "document"}},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["output"]["embeddings"][0]["embedding"]


def compact(before_days: int = 30, dry_run: bool = True):
    if not dry_run and EMBED_BACKEND == "dashscope" and not DASHSCOPE_API_KEY:
        print("ERROR: DASHSCOPE_API_KEY 未设置，无法生成摘要 embedding")
        sys.exit(1)

    client = QdrantClient(url=QDRANT_URL, timeout=60, check_compatibility=False)
    cutoff_ts = int(time.time()) - before_days * 86400

    all_convs: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[
                FieldCondition(key="category", match=MatchValue(value="conversation")),
                FieldCondition(key="timestamp", range=Range(lt=cutoff_ts)),
            ]),
            limit=100, offset=offset, with_payload=True,
        )
        if not points:
            break
        for p in points:
            ts = p.payload.get("timestamp", 0)
            date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"
            all_convs.append({"id": p.id, "date": date_str,
                               "content": p.payload.get("content", ""),
                               "tags": p.payload.get("tags", "")})
        if offset is None:
            break

    if not all_convs:
        print(f"没有 {before_days} 天前的 conversation 需要压缩。")
        return

    by_date: dict[str, list] = {}
    for c in all_convs:
        by_date.setdefault(c["date"], []).append(c)

    print(f"[{'DRY RUN' if dry_run else '执行'}] 共 {len(all_convs)} 条待压缩，{len(by_date)} 天")
    for date in sorted(by_date):
        convs = by_date[date]
        total_chars = sum(len(c["content"]) for c in convs)
        print(f"  {date}: {len(convs)} 条 ({total_chars} 字符)")

    if dry_run:
        print("\n加 --run 参数执行实际压缩。")
        return

    compressed = deleted = 0
    for date, convs in sorted(by_date.items()):
        snippets, all_tags = [], set()
        for conv in convs:
            lines = conv["content"].split("\n")
            summary = []
            for line in lines:
                s = line.strip()
                if s.startswith(("[问]", "[答]", "用户:", "Claude:")):
                    summary.append(s[:150])
            snippets.append(" | ".join(summary) if summary else conv["content"][:200])
            for tag in conv["tags"].split(","):
                t = tag.strip()
                if t and t != date:
                    all_tags.add(t)

        merged = f"[{date} 对话摘要] 共 {len(convs)} 轮对话\n\n"
        remaining = 4000 - len(merged)
        for i, snippet in enumerate(snippets):
            entry = f"({i+1}) {snippet}\n"
            if len(entry) > remaining:
                merged += f"... 及其他 {len(snippets) - i} 条\n"
                break
            merged += entry
            remaining -= len(entry)

        tags = f"{date},daily-summary,compressed," + ",".join(sorted(all_tags)[:5])
        embedding = get_embedding(merged)
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(
                id=make_id(merged), vector=embedding,
                payload={"content": merged, "category": "summary", "tags": tags,
                         "importance": "high", "source": "claude_code",
                         "created_at": f"{date}T00:00:00",
                         "timestamp": int(datetime(*[int(x) for x in date.split("-")]).timestamp()),
                         "version": "v3"},
            )],
        )
        client.delete(collection_name=COLLECTION_NAME,
                      points_selector=[c["id"] for c in convs])
        print(f"  ✅ {date}: {len(convs)} 条 → 1 条摘要")
        compressed += 1
        deleted += len(convs)

    print(f"\n完成！删除 {deleted} 条，生成 {compressed} 条摘要。")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--run", action="store_true", help="实际执行（默认 dry_run）")
    parser.add_argument("--prod", action="store_true", help="操作生产集合（默认走测试集合）")
    args = parser.parse_args()
    if not args.prod:
        print(f"[TEST MODE] 集合: {COLLECTION_NAME}（加 --prod 操作生产）")
    compact(before_days=args.days, dry_run=not args.run)
