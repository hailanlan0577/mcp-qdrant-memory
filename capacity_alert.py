#!/usr/bin/env /Users/chenyuanhai/mcp-qdrant-memory/venv/bin/python
"""
E1.1.6 - Qdrant 容量告警脚本
超过 5000 条时打印告警（配合 cron 邮件或日志监控）。
"""
import sys
from qdrant_client import QdrantClient

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "unified_memories_v3" if "--prod" in sys.argv else "unified_memories_v3_test"
ALERT_THRESHOLD = 5000

client = QdrantClient(url=QDRANT_URL, timeout=10, check_compatibility=False)
info = client.get_collection(collection_name=COLLECTION_NAME)
total = info.points_count

print(f"[capacity_alert] {COLLECTION_NAME}: {total} 条记忆")

if total >= ALERT_THRESHOLD:
    print(f"⚠️  ALERT: 记忆数 {total} 已超过阈值 {ALERT_THRESHOLD}！建议执行 compact_conversations 压缩。", file=sys.stderr)
    sys.exit(1)
else:
    remaining = ALERT_THRESHOLD - total
    print(f"✓ 正常，距告警阈值还剩 {remaining} 条")
    sys.exit(0)
