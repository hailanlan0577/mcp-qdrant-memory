"""
迁移脚本：claude-memory → claude-memory-v3
从旧 collection 读取所有记忆，用 text-embedding-v4 重新生成向量，写入新 collection。
旧数据不受影响。

用法：
  export DASHSCOPE_API_KEY=sk-xxx
  python migrate_to_v3.py
"""

import hashlib
import os
import sys
import time

import httpx
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

# Embedding backend 切换:
#   local (默认): 本地 MLX daemon, 4096 维
#   dashscope   : 阿里云 v4, 1024 维
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "local").lower()

QDRANT_URL = "http://localhost:6333"
OLD_COLLECTION = "claude-memory"
NEW_COLLECTION = "claude-memory-v3"

if EMBED_BACKEND == "local":
    VECTOR_DIM = 4096
elif EMBED_BACKEND == "dashscope":
    VECTOR_DIM = 1024
else:
    raise ValueError(f"未知 EMBED_BACKEND: {EMBED_BACKEND}")

LOCAL_EMBED_URL = os.environ.get("LOCAL_EMBED_URL", "http://127.0.0.1:8765/embed")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

if EMBED_BACKEND == "dashscope" and not DASHSCOPE_API_KEY:
    print("错误：EMBED_BACKEND=dashscope 时必须设置 DASHSCOPE_API_KEY 环境变量")
    print("  export DASHSCOPE_API_KEY=sk-xxx")
    sys.exit(1)

http_client = httpx.Client(timeout=30)
qdrant = QdrantClient(url=QDRANT_URL, timeout=30, check_compatibility=False)


def get_embedding(text: str) -> list[float]:
    """生成向量 (按 EMBED_BACKEND 路由)。"""
    if EMBED_BACKEND == "local":
        resp = http_client.post(LOCAL_EMBED_URL, json={"text": text, "text_type": "document"})
        resp.raise_for_status()
        return resp.json()["embedding"]
    resp = http_client.post(
        EMBEDDING_API_URL,
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": EMBEDDING_MODEL,
            "input": text,
            "dimensions": VECTOR_DIM,
            "encoding_format": "float",
        },
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def ensure_new_collection():
    """创建新 collection。"""
    collections = [c.name for c in qdrant.get_collections().collections]
    if NEW_COLLECTION in collections:
        info = qdrant.get_collection(NEW_COLLECTION)
        print(f"新 collection 已存在，当前 {info.points_count} 条记录")
        return

    qdrant.create_collection(
        collection_name=NEW_COLLECTION,
        vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
    )

    # 建立索引
    for field, schema in [
        ("content", TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            min_token_len=2,
            max_token_len=20,
        )),
        ("tags", TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            min_token_len=2,
            max_token_len=20,
        )),
        ("category", "keyword"),
        ("importance", "keyword"),
    ]:
        try:
            qdrant.create_payload_index(
                collection_name=NEW_COLLECTION,
                field_name=field,
                field_schema=schema,
            )
        except Exception:
            pass

    print(f"已创建新 collection: {NEW_COLLECTION} (维度: {VECTOR_DIM})")


def migrate():
    """执行迁移。"""
    # 统计旧数据
    old_info = qdrant.get_collection(OLD_COLLECTION)
    total = old_info.points_count
    print(f"\n旧 collection: {OLD_COLLECTION} ({total} 条记忆)")
    print(f"新 collection: {NEW_COLLECTION}")
    print(f"Embedding: {EMBEDDING_MODEL} ({VECTOR_DIM}维)")
    print(f"{'='*50}")

    ensure_new_collection()

    # 遍历旧数据
    migrated = 0
    skipped = 0
    failed = 0
    offset = None
    batch_points = []

    while True:
        points, offset = qdrant.scroll(
            collection_name=OLD_COLLECTION,
            limit=50,
            offset=offset,
            with_payload=True,
        )

        if not points:
            break

        for point in points:
            content = point.payload.get("content", "")
            if not content.strip():
                skipped += 1
                continue

            try:
                new_embedding = get_embedding(content)
                memory_id = hashlib.md5(content.encode()).hexdigest()

                # 保留原有 payload，更新 version
                payload = dict(point.payload)
                payload["version"] = "v3"
                payload["migrated_from"] = "v2.1"

                batch_points.append(
                    PointStruct(
                        id=memory_id,
                        vector=new_embedding,
                        payload=payload,
                    )
                )

                # 每 10 条批量写入
                if len(batch_points) >= 10:
                    qdrant.upsert(
                        collection_name=NEW_COLLECTION,
                        points=batch_points,
                    )
                    migrated += len(batch_points)
                    batch_points = []
                    print(f"  已迁移 {migrated}/{total} ...", flush=True)

                # API 限速：避免太快
                time.sleep(0.1)

            except Exception as e:
                failed += 1
                print(f"  失败: {content[:60]}... 错误: {e}")

        if offset is None:
            break

    # 写入剩余
    if batch_points:
        qdrant.upsert(
            collection_name=NEW_COLLECTION,
            points=batch_points,
        )
        migrated += len(batch_points)

    print(f"\n{'='*50}")
    print(f"迁移完成！")
    print(f"  成功: {migrated}")
    print(f"  跳过(空内容): {skipped}")
    print(f"  失败: {failed}")
    print(f"\n旧数据保留在 {OLD_COLLECTION}，确认无误后可手动删除。")


if __name__ == "__main__":
    migrate()
