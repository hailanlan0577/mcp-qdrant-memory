"""
记忆压缩脚本：将旧的 conversation 记录压缩为周总结。

策略：
1. 找出 N 天前的所有 conversation 类记忆
2. 按周分组
3. 提取每周的关键话题和操作
4. 生成一条周总结（importance=high, category=summary）
5. 删除原始 conversation 记录

用法:
  python compress.py              # 压缩 14 天前的 conversation
  python compress.py --days 7     # 压缩 7 天前的
  python compress.py --dry-run    # 预览不执行
"""

import argparse
import re
import time
from collections import defaultdict
from datetime import datetime, timedelta

from fastembed import TextEmbedding
from qdrant_client import QdrantClient
from qdrant_client.models import (
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    Range,
)

import hashlib

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "claude-memory"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
VECTOR_DIM = 512


def make_id(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def extract_topics(conversations: list[dict]) -> dict:
    """从对话记录中提取关键话题。"""
    topics = defaultdict(int)
    actions = []

    for conv in conversations:
        content = conv.get("content", "")
        tags = conv.get("tags", "")

        # 从 tags 提取关键词
        if tags:
            for tag in tags.split(","):
                tag = tag.strip()
                if tag and not tag.startswith("20") and tag not in ("qa-full", "conversation"):
                    if not re.match(r"^round-\d+$", tag):
                        topics[tag] += 1

        # 从内容提取 [问] 和 [答] 的摘要
        q_match = re.search(r"\[问\]\s*(.+?)(?:\n|$)", content)
        a_match = re.search(r"\[答\]\s*(.+?)(?:\n|$)", content)
        if q_match:
            q_text = q_match.group(1).strip()[:80]
            actions.append(q_text)

    return {
        "topics": dict(sorted(topics.items(), key=lambda x: -x[1])[:15]),
        "actions": actions[:20],
    }


def generate_summary(week_label: str, conversations: list[dict], extracted: dict) -> str:
    """生成一周的结构化总结。"""
    topics = extracted["topics"]
    actions = extracted["actions"]

    lines = [f"[周总结] {week_label}（共 {len(conversations)} 轮对话）"]
    lines.append("")

    if topics:
        top_topics = list(topics.keys())[:10]
        lines.append(f"关键话题: {', '.join(top_topics)}")
        lines.append("")

    if actions:
        lines.append("主要操作:")
        for i, action in enumerate(actions[:15], 1):
            lines.append(f"  {i}. {action}")

    return "\n".join(lines)


def get_week_label(dt: datetime) -> str:
    """获取周标签，如 '2026-03-03 ~ 2026-03-09'"""
    start = dt - timedelta(days=dt.weekday())
    end = start + timedelta(days=6)
    return f"{start.strftime('%Y-%m-%d')} ~ {end.strftime('%Y-%m-%d')}"


def main():
    parser = argparse.ArgumentParser(description="压缩旧的 conversation 记忆为周总结")
    parser.add_argument("--days", type=int, default=14, help="压缩多少天之前的记录（默认14天）")
    parser.add_argument("--dry-run", action="store_true", help="预览模式，不执行删除和存储")
    args = parser.parse_args()

    client = QdrantClient(url=QDRANT_URL, timeout=60, check_compatibility=False)
    embed_model = TextEmbedding(model_name=EMBEDDING_MODEL)

    cutoff = int((datetime.now() - timedelta(days=args.days)).timestamp())

    print(f"查找 {args.days} 天前的 conversation 记忆...")

    # 获取所有旧的 conversation 记忆
    old_convos = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(
                must=[
                    FieldCondition(key="category", match=MatchValue(value="conversation")),
                    FieldCondition(key="timestamp", range=Range(lt=cutoff)),
                ]
            ),
            limit=100,
            offset=offset,
            with_payload=True,
        )
        if not points:
            break
        for p in points:
            old_convos.append({
                "id": p.id,
                "content": p.payload.get("content", ""),
                "tags": p.payload.get("tags", ""),
                "timestamp": p.payload.get("timestamp", 0),
                "created_at": p.payload.get("created_at", ""),
            })
        if offset is None:
            break

    if not old_convos:
        print("没有需要压缩的记忆。")
        return

    print(f"找到 {len(old_convos)} 条待压缩")

    # 按周分组
    weekly = defaultdict(list)
    for conv in old_convos:
        ts = conv["timestamp"]
        if ts:
            dt = datetime.fromtimestamp(ts)
            week = get_week_label(dt)
            weekly[week].append(conv)

    # 处理每周
    total_deleted = 0
    total_summaries = 0

    for week_label, convos in sorted(weekly.items()):
        extracted = extract_topics(convos)
        summary_text = generate_summary(week_label, convos, extracted)

        print(f"\n{'='*60}")
        print(f"周: {week_label} ({len(convos)} 条)")
        print(f"{'='*60}")
        print(summary_text)

        if args.dry_run:
            print(f"\n[DRY RUN] 将删除 {len(convos)} 条，生成 1 条总结")
            continue

        # 生成总结记忆
        embedding = list(embed_model.embed([summary_text]))[0].tolist()
        summary_id = make_id(summary_text)
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[
                PointStruct(
                    id=summary_id,
                    vector=embedding,
                    payload={
                        "content": summary_text,
                        "category": "summary",
                        "tags": f"周总结,{week_label}",
                        "importance": "high",
                        "created_at": datetime.now().isoformat(),
                        "timestamp": int(time.time()),
                        "version": "v2",
                        "compressed_from": len(convos),
                    },
                )
            ],
        )
        total_summaries += 1

        # 删除原始记录
        ids_to_delete = [c["id"] for c in convos]
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=ids_to_delete,
        )
        total_deleted += len(ids_to_delete)
        print(f"  ✅ 已压缩: 删除 {len(ids_to_delete)} 条 → 生成 1 条总结")

    print(f"\n{'='*60}")
    if args.dry_run:
        print(f"[DRY RUN] 预计删除 {len(old_convos)} 条，生成 {len(weekly)} 条总结")
    else:
        print(f"完成！删除: {total_deleted} 条，生成总结: {total_summaries} 条")

    # 显示压缩后统计
    info = client.get_collection(collection_name=COLLECTION_NAME)
    print(f"当前总记忆数: {info.points_count}")


if __name__ == "__main__":
    main()
