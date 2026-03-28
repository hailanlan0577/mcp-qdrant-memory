#!/usr/bin/env /Users/chenyuanhai/mcp-qdrant-memory/venv/bin/python
"""
E5.1.2 - 清理 text 字段冗余
V1 迁移时生成的记录同时有 text + content 两个字段，text 字段已无用，删除节省空间。
用法: python cleanup_text_field.py [--dry-run]
"""
import os
import sys

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue

DRY_RUN = "--dry-run" in sys.argv or (len(sys.argv) < 2 or sys.argv[1] != "--execute")
PROD = "--prod" in sys.argv

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "unified_memories_v3" if ("--prod" in sys.argv) else "unified_memories_v3_test"

client = QdrantClient(url=QDRANT_URL, timeout=30, check_compatibility=False)

# 找出有 text 字段的记录（通过 scroll 全量扫描）
print(f"[cleanup_text_field] 扫描 {COLLECTION_NAME}...")
affected_ids = []
offset = None
scanned = 0

while True:
    points, offset = client.scroll(
        collection_name=COLLECTION_NAME,
        limit=200,
        offset=offset,
        with_payload=True,
    )
    if not points:
        break
    for p in points:
        scanned += 1
        if "text" in p.payload:
            affected_ids.append(p.id)
    if offset is None:
        break

print(f"扫描完成: {scanned} 条记录，其中 {len(affected_ids)} 条含冗余 text 字段")

if not affected_ids:
    print("✓ 无需清理")
    sys.exit(0)

if DRY_RUN:
    print(f"[试运行] 将删除 {len(affected_ids)} 条记录的 text 字段")
    print("执行: python cleanup_text_field.py --execute")
    sys.exit(0)

# 批量删除 text 字段（payload delete）
batch_size = 50
cleaned = 0
for i in range(0, len(affected_ids), batch_size):
    batch = affected_ids[i:i + batch_size]
    client.delete_payload(
        collection_name=COLLECTION_NAME,
        keys=["text"],
        points=batch,
    )
    cleaned += len(batch)
    print(f"  已清理 {cleaned}/{len(affected_ids)} 条...")

print(f"✓ 清理完成：{cleaned} 条记录的 text 字段已删除")
