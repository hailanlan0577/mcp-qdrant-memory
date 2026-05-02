"""
迁移脚本：openclaw_memories → openclaw_memories_v3
纯 REST API 实现，不依赖 qdrant-client（兼容 Python 3.9 + LibreSSL）
"""

import hashlib
import json
import os
import time
import urllib.request
import urllib.error

# Embedding backend 切换:
#   local (默认): 本地 MLX daemon, 4096 维
#   dashscope   : 阿里云 v4, 1024 维
EMBED_BACKEND = os.environ.get("EMBED_BACKEND", "local").lower()

QDRANT_URL = "http://localhost:6333"
OLD_COLLECTION = "openclaw_memories"
NEW_COLLECTION = "openclaw_memories_v3"

if EMBED_BACKEND == "local":
    VECTOR_DIM = 4096
elif EMBED_BACKEND == "dashscope":
    VECTOR_DIM = 1024
else:
    raise ValueError(f"未知 EMBED_BACKEND: {EMBED_BACKEND}")

LOCAL_EMBED_URL = os.environ.get("LOCAL_EMBED_URL", "http://127.0.0.1:8765/embed")
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "sk-0da94cd0218b4224aaebc5cf4a24c39f")
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"


def qdrant_request(method, path, body=None):
    url = f"{QDRANT_URL}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


def get_embedding(text):
    if EMBED_BACKEND == "local":
        data = json.dumps({"text": text, "text_type": "document"}).encode()
        req = urllib.request.Request(LOCAL_EMBED_URL, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())["embedding"]
    data = json.dumps({
        "model": EMBEDDING_MODEL,
        "input": text,
        "dimensions": VECTOR_DIM,
        "encoding_format": "float",
    }).encode()
    req = urllib.request.Request(EMBEDDING_API_URL, data=data, method="POST")
    req.add_header("Authorization", f"Bearer {DASHSCOPE_API_KEY}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    return result["data"][0]["embedding"]


def ensure_new_collection():
    collections_resp = qdrant_request("GET", "/collections")
    names = [c["name"] for c in collections_resp["result"]["collections"]]

    if NEW_COLLECTION in names:
        info = qdrant_request("GET", f"/collections/{NEW_COLLECTION}")
        count = info["result"]["points_count"]
        print(f"新 collection 已存在，当前 {count} 条记录")
        return

    qdrant_request("PUT", f"/collections/{NEW_COLLECTION}", {
        "vectors": {"size": VECTOR_DIM, "distance": "Cosine"}
    })

    # 建立索引
    indexes = [
        ("text", {
            "type": "text",
            "tokenizer": "multilingual",
            "min_token_len": 2,
            "max_token_len": 20,
        }),
        ("category", "keyword"),
        ("importance_level", "keyword"),
        ("created_at", "keyword"),
        ("timestamp", "integer"),
    ]

    for field, schema in indexes:
        try:
            qdrant_request("PUT", f"/collections/{NEW_COLLECTION}/index", {
                "field_name": field,
                "field_schema": schema,
            })
        except Exception:
            pass

    print(f"已创建新 collection: {NEW_COLLECTION} (维度: {VECTOR_DIM})")


def migrate():
    # 统计旧数据
    old_info = qdrant_request("GET", f"/collections/{OLD_COLLECTION}")
    total = old_info["result"]["points_count"]
    print(f"\n旧 collection: {OLD_COLLECTION} ({total} 条记忆)")
    print(f"新 collection: {NEW_COLLECTION}")
    print(f"Embedding: {EMBEDDING_MODEL} ({VECTOR_DIM}维)")
    print(f"{'='*50}")

    ensure_new_collection()

    migrated = 0
    skipped = 0
    failed = 0
    offset = None
    batch_points = []

    while True:
        scroll_body = {
            "limit": 50,
            "with_payload": True,
        }
        if offset is not None:
            scroll_body["offset"] = offset

        result = qdrant_request("POST", f"/collections/{OLD_COLLECTION}/points/scroll", scroll_body)
        points = result["result"]["points"]
        offset = result["result"].get("next_page_offset")

        if not points:
            break

        for point in points:
            text = point["payload"].get("text", "")
            if not text.strip():
                skipped += 1
                continue

            try:
                new_embedding = get_embedding(text)
                memory_id = hashlib.md5(text.encode()).hexdigest()

                payload = dict(point["payload"])
                payload["version"] = "v3"
                payload["migrated_from"] = "v2"

                # 确保有 timestamp
                if "timestamp" not in payload and "createdAt" in payload:
                    payload["timestamp"] = payload["createdAt"]

                # 确保有 created_at 日期
                if "created_at" not in payload and "createdAt" in payload:
                    try:
                        from datetime import datetime
                        ts = payload["createdAt"]
                        if isinstance(ts, (int, float)):
                            payload["created_at"] = datetime.fromtimestamp(ts / 1000).strftime("%Y-%m-%d")
                    except Exception:
                        pass

                # 确保有 importance_level
                if "importance_level" not in payload:
                    payload["importance_level"] = "medium"

                batch_points.append({
                    "id": memory_id,
                    "vector": new_embedding,
                    "payload": payload,
                })

                if len(batch_points) >= 10:
                    qdrant_request("PUT", f"/collections/{NEW_COLLECTION}/points", {
                        "points": batch_points
                    })
                    migrated += len(batch_points)
                    batch_points = []
                    print(f"  已迁移 {migrated}/{total} ...", flush=True)

                time.sleep(0.1)

            except Exception as e:
                failed += 1
                print(f"  失败: {text[:60]}... 错误: {e}")

        if offset is None:
            break

    if batch_points:
        qdrant_request("PUT", f"/collections/{NEW_COLLECTION}/points", {
            "points": batch_points
        })
        migrated += len(batch_points)

    print(f"\n{'='*50}")
    print(f"迁移完成！")
    print(f"  成功: {migrated}")
    print(f"  跳过(空内容): {skipped}")
    print(f"  失败: {failed}")

    # 验证
    new_info = qdrant_request("GET", f"/collections/{NEW_COLLECTION}")
    new_count = new_info["result"]["points_count"]
    print(f"\n新 collection 当前: {new_count} 条记录")
    print(f"旧数据保留在 {OLD_COLLECTION}，确认无误后可手动删除。")


if __name__ == "__main__":
    migrate()
