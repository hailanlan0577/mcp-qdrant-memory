#!/usr/bin/env python3
"""
独立 QA 记录脚本 - 直接写入 Qdrant V3 (unified_memories_v3)
不依赖 MCP server，可从 Bash/hook 调用。

用法:
    python record_qa.py "用户问题" "Claude回答" [category] [tags]

环境变量:
    DASHSCOPE_API_KEY  (必须)
"""

import hashlib
import json
import os
import sys
import time
from datetime import datetime, timezone

import httpx

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "unified_memories_v3"
VECTOR_DIM = 1024
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "sk-0da94cd0218b4224aaebc5cf4a24c39f")
EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

_client = httpx.Client(timeout=30, trust_env=False)


def get_embedding(text: str) -> list[float]:
    resp = _client.post(
        EMBEDDING_API_URL,
        headers={"Authorization": f"Bearer {DASHSCOPE_API_KEY}", "Content-Type": "application/json"},
        json={"model": "text-embedding-v4", "input": text[:8000], "dimensions": VECTOR_DIM,
              "encoding_format": "float", "extra_body": {"text_type": "document"}},
    )
    resp.raise_for_status()
    return resp.json()["data"][0]["embedding"]


def store(content: str, category: str = "conversation", tags: str = "", source: str = "claude_code") -> str:
    vector = get_embedding(content)
    now = datetime.now(timezone.utc)
    point_id = hashlib.md5(f"{content[:100]}{time.time()}".encode()).hexdigest()
    # 转换为整数 ID（Qdrant 支持）
    point_id_int = int(point_id[:8], 16)

    payload = {
        "content": content,
        "category": category,
        "tags": tags,
        "source": source,
        "created_at": now.isoformat(),
        "timestamp": int(now.timestamp()),
        "importance": "low" if category == "conversation" else "high",
    }

    resp = _client.put(
        f"{QDRANT_URL}/collections/{COLLECTION_NAME}/points",
        headers={"Content-Type": "application/json"},
        json={"points": [{"id": point_id_int, "vector": vector, "payload": payload}]},
    )
    resp.raise_for_status()
    return point_id_int


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print("用法: python record_qa.py <user_msg> <claude_reply> [category] [tags]")
        sys.exit(1)

    user_msg = sys.argv[1]
    claude_reply = sys.argv[2]
    category = sys.argv[3] if len(sys.argv) > 3 else "conversation"
    tags = sys.argv[4] if len(sys.argv) > 4 else ""

    today = datetime.now().strftime("%Y-%m-%d")
    content = f"[{today}]\n\n用户: {user_msg}\n\nClaude: {claude_reply}"

    try:
        pid = store(content, category, tags)
        print(f"✅ 已写入 Qdrant V3 (ID: {pid})")
    except Exception as e:
        print(f"❌ 写入失败: {e}", file=sys.stderr)
        sys.exit(1)
