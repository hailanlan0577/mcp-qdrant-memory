"""从 Pinecone 迁移所有记忆到自建 Qdrant。

用法: python migrate_from_pinecone.py
"""

import sys
import time

from fastembed import TextEmbedding
from pinecone import Pinecone
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    PointStruct,
    VectorParams,
)

# ── Pinecone 配置 ──
PINECONE_API_KEY = "pcsk_22B3cg_8MjoJwru3yqiTyMSxP6XmpK3N8xX4PdVeSh3atTuT6WtkkL646qTgXorhZCzgkF"
PINECONE_INDEX = "claude-memory"
PINECONE_NAMESPACE = "claude-code"

# ── Qdrant 配置 ──
QDRANT_URL = "http://192.168.3.100:6333"
COLLECTION_NAME = "claude-memory"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
VECTOR_DIM = 512


def main():
    # 初始化
    print("🔧 初始化连接...")
    pc = Pinecone(api_key=PINECONE_API_KEY)
    pc_index = pc.Index(PINECONE_INDEX)

    qdrant = QdrantClient(url=QDRANT_URL, timeout=60, check_compatibility=False)
    embed_model = TextEmbedding(model_name=EMBEDDING_MODEL)

    # 确保 Qdrant collection 存在
    collections = [c.name for c in qdrant.get_collections().collections]
    if COLLECTION_NAME not in collections:
        qdrant.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )
        print(f"✅ 创建 Qdrant collection: {COLLECTION_NAME}")

    # 从 Pinecone 获取所有向量 ID
    print("📥 从 Pinecone 获取所有记忆...")
    all_ids = []
    for ids_batch in pc_index.list(namespace=PINECONE_NAMESPACE):
        all_ids.extend(ids_batch)

    total = len(all_ids)
    print(f"📊 共找到 {total} 条记忆")

    if total == 0:
        print("没有数据需要迁移。")
        return

    # 分批获取并迁移
    batch_size = 50
    migrated = 0
    failed = 0

    for i in range(0, total, batch_size):
        batch_ids = all_ids[i : i + batch_size]

        # 从 Pinecone 获取完整数据
        fetch_result = pc_index.fetch(ids=batch_ids, namespace=PINECONE_NAMESPACE)

        points = []
        for vec_id, vec_data in fetch_result.vectors.items():
            metadata = vec_data.metadata or {}
            content = metadata.get("content", "")

            if not content:
                failed += 1
                continue

            # 用新模型重新生成向量
            new_embedding = list(embed_model.embed([content]))[0].tolist()

            points.append(
                PointStruct(
                    id=vec_id,
                    vector=new_embedding,
                    payload=metadata,
                )
            )

        if points:
            qdrant.upsert(collection_name=COLLECTION_NAME, points=points)
            migrated += len(points)

        print(f"  进度: {min(i + batch_size, total)}/{total} (成功: {migrated}, 跳过: {failed})")
        time.sleep(0.1)

    print(f"\n✅ 迁移完成！")
    print(f"   成功: {migrated} 条")
    print(f"   跳过: {failed} 条")
    print(f"   总计: {total} 条")

    # 验证
    info = qdrant.get_collection(collection_name=COLLECTION_NAME)
    print(f"\n📊 Qdrant collection 状态:")
    print(f"   向量数量: {info.points_count}")
    print(f"   向量维度: {VECTOR_DIM}")


if __name__ == "__main__":
    main()
