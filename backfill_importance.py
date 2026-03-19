"""
回填脚本：给现有 v1 记忆添加 importance 字段。
运行一次即可，幂等安全。

用法: python backfill_importance.py
"""

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "claude-memory"

CATEGORY_IMPORTANCE = {
    "project": "high",
    "architecture": "high",
    "solution": "high",
    "preference": "high",
    "debug": "medium",
    "general": "medium",
    "conversation": "low",
}


def main():
    client = QdrantClient(url=QDRANT_URL, timeout=60, check_compatibility=False)

    updated = 0
    skipped = 0
    offset = None

    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            offset=offset,
            with_payload=True,
            with_vectors=True,
        )
        if not points:
            break

        batch = []
        for p in points:
            if p.payload.get("importance"):
                skipped += 1
                continue

            category = p.payload.get("category", "general")
            importance = CATEGORY_IMPORTANCE.get(category, "medium")

            new_payload = {**p.payload, "importance": importance, "version": "v2"}
            batch.append(
                PointStruct(
                    id=p.id,
                    vector=p.vector,
                    payload=new_payload,
                )
            )
            updated += 1

        if batch:
            client.upsert(collection_name=COLLECTION_NAME, points=batch)
            print(f"  已更新 {len(batch)} 条")

        if offset is None:
            break

    print(f"\n完成！更新: {updated} 条，跳过(已有importance): {skipped} 条")


if __name__ == "__main__":
    main()
