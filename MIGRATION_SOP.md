# 记忆系统迁移 SOP (E6.3)

> 本文档描述从 unified_memories_v3（text-embedding-v4, 1024维）迁移到未来版本（如 V4）的操作规程

---

## 触发条件

迁移 V4 的典型场景：
- Embedding 模型升级（如切换到更高维度模型）
- Qdrant 集合 schema 重大变更
- 双端记忆数量 > 10,000 条，需要分库

---

## 迁移前检查清单

```
[ ] 确认新 embedding 模型已申请 API Key 并测试通过
[ ] 在测试集合（unified_memories_v4_test）验证新模型效果
[ ] 备份当前 Qdrant 快照（~/qdrant-backups/pre-migration-YYYY-MM-DD.snapshot）
[ ] 备份 Neo4j（~/neo4j-backups/neo4j_pre_migration_YYYY-MM-DD.dump）
[ ] 记录当前记忆数量：memory_stats() 输出存档
[ ] 通知 OpenClaw 侧暂停写入（关闭 autoCapture）
```

---

## 迁移步骤

### Step 1：导出现有记忆

```bash
cd ~/mcp-qdrant-memory
venv/bin/python - << 'EOF'
from qdrant_client import QdrantClient
import json, time

client = QdrantClient(url="http://localhost:6333", timeout=30, check_compatibility=False)
all_points = []
offset = None

while True:
    points, offset = client.scroll(
        collection_name="unified_memories_v3",
        limit=200, offset=offset, with_payload=True, with_vectors=False
    )
    if not points:
        break
    for p in points:
        all_points.append({"id": str(p.id), "payload": p.payload})
    if offset is None:
        break

with open(f"export_v3_{int(time.time())}.json", "w") as f:
    json.dump(all_points, f, ensure_ascii=False, indent=2)
print(f"导出 {len(all_points)} 条记忆")
EOF
```

### Step 2：创建新集合

```bash
# 修改 server_v3.py 中的配置
COLLECTION_NAME = "unified_memories_v4"
VECTOR_DIM = <新维度>
EMBEDDING_MODEL = "<新模型名>"
```

### Step 3：批量重新向量化并写入

```bash
venv/bin/python - << 'EOF'
import json
from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct
# 加载导出数据，重新调用新 embedding API，写入 V4 集合
# 建议每批 50 条，失败重试，记录进度
EOF
```

### Step 4：双写验证期（≥ 1 周）

- 新旧两个集合同时写入
- 用相同查询对比搜索结果质量
- 确认 OpenClaw 插件已切换到新集合

### Step 5：切换生产流量

```bash
# 更新 server_v3.py COLLECTION_NAME
# 重启 MCP Server：pkill -f server_v3.py
# 更新 OpenClaw 插件配置
# 更新 cron 脚本中的集合名
```

### Step 6：清理旧集合

```bash
# 旧集合保留 30 天后删除
# python3 -c "from qdrant_client import QdrantClient; QdrantClient('http://localhost:6333').delete_collection('unified_memories_v3')"
```

---

## 回滚方案

```bash
# 1. 将 COLLECTION_NAME 改回 unified_memories_v3
# 2. 重启 MCP Server
# 3. 旧集合数据完好无损（迁移过程中未删除）
```

---

## 迁移后检查

```
[ ] memory_stats() 总数 ≈ 迁移前数量（允许 ±5%）
[ ] 语义搜索结果质量不下降（抽样 10 条对比）
[ ] Stop Hook 正常写入新集合
[ ] OpenClaw autoCapture 正常写入新集合
[ ] 所有 cron 任务指向新集合
[ ] 旧集合标记为 archived（改名 unified_memories_v3_archived）
```
