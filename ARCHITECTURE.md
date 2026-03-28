# 统一记忆系统架构图 (E6.2)

> 更新：2026-03-26 | 两端数据流 + 工具调用路径

## 整体拓扑

```
┌─────────────────────────────────────────────────────────────────┐
│                    MacBook Pro（Claude Code）                    │
│                                                                 │
│  ┌─────────────────┐    ┌──────────────────────────────────┐   │
│  │   用户 / Claude  │    │     MCP Server (server_v3.py)    │   │
│  │                 │◄──►│  工具：store / search / keyword   │   │
│  │  ~/.claude/     │    │       hybrid / delete / update    │   │
│  │  CLAUDE.md      │    │       list / compact / stats      │   │
│  │  规范 + 自检流程 │    │       search_openclaw / global   │   │
│  └────────┬────────┘    └───────────────┬──────────────────┘   │
│           │                             │                       │
│  ┌────────▼────────┐                   │ SSH Tunnel            │
│  │   Stop Hook     │                   │ (port 6333)           │
│  │  auto-pinecone  │                   │                       │
│  │  -store.py      │                   │                       │
│  │  (对话自动双写) │                   │                       │
│  └────────┬────────┘                   │                       │
└───────────┼─────────────────────────── │ ──────────────────────┘
            │                            │
            │ 双写                        │ 读写
            ▼                            ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Mac Mini（服务端）                          │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │              Qdrant (localhost:6333)                   │    │
│  │         Collection: unified_memories_v3                │    │
│  │         1024维 · Cosine · text-embedding-v4            │    │
│  │                                                        │    │
│  │  payload schema:                                       │    │
│  │    content / category / importance / source            │    │
│  │    tags / timestamp / created_at / version             │    │
│  │                                                        │    │
│  │  source=claude_code (≈275条) │ source=openclaw (≈310条)│    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │           Graphiti MCP Server (localhost:18001)         │    │
│  │           知识图谱 · SSE 协议 · Neo4j 持久化             │    │
│  │                                                        │    │
│  │  group_id: claude_code │ group_id: openclaw             │    │
│  │  节点(entities) + 事实(facts) + 事件(episodes)          │    │
│  └────────────────────────────────────────────────────────┘    │
│                                                                 │
│  ┌────────────────────────────────────────────────────────┐    │
│  │              OpenClaw Gateway (localhost:7890)          │    │
│  │           飞书 WebSocket · launchd 开机自启              │    │
│  │                                                        │    │
│  │  插件: openclaw-memory-qdrant-v3                        │    │
│  │    autoRecall: before_agent_start → Top 10 注入         │    │
│  │    autoCapture: agent_end → 自动双写 Qdrant+Graphiti     │    │
│  │    过滤: SYSTEM_MESSAGE_PATTERNS (心跳/cron/health)      │    │
│  └────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

## 数据写入路径

### Claude Code 侧

```
用户对话结束
    │
    ▼
Stop Hook (auto-pinecone-store.py)
    ├─► parse_all_exchanges() 解析 transcript
    ├─► should_skip() 过滤琐碎/系统消息
    ├─► store_to_qdrant_v3() → record_qa.py → Qdrant unified_memories_v3
    ├─► store_to_graphiti() → SSE → Graphiti add_memory
    └─► detect_and_store_reminders() → debug/feedback 自检提醒
```

### OpenClaw 侧

```
agent_end 事件触发
    │
    ▼
autoCapture hook
    ├─► extractText() 提取用户+助手消息
    ├─► isSystemMessage() 过滤系统消息
    ├─► isNoiseContent() 过滤图片/媒体
    ├─► getEmbedding() → text-embedding-v4 (DashScope)
    ├─► 去重检查 (相似度 > 0.92 跳过)
    └─► qdrant.upsert() → unified_memories_v3 (source=openclaw)
```

## 搜索路径

```
Claude Code 搜索请求
    │
    ├─► search_memory(query)
    │      └─ 向量相似度 + importance 加权 + 时间衰减 + 去重
    │
    ├─► keyword_search(keyword)
    │      └─ Qdrant TextIndex 全文检索
    │
    ├─► hybrid_search(query)
    │      ├─ Qdrant 向量搜索 (并行)
    │      └─ Graphiti search_nodes + search_memory_facts (并行)
    │
    └─► global_search(query)
           ├─ source=claude_code Top-K (并行)
           └─ source=openclaw Top-K (并行)
```

## 运维定时任务

| 任务 | 机器 | 时间 | 脚本 |
|------|------|------|------|
| Qdrant 快照备份 | Mac Mini | 每日 2:00 | ~/qdrant-backup.sh |
| Neo4j dump 备份 | Mac Mini | 每日 2:30 | ~/graphiti-backup.sh |
| Graphiti 孤立节点清理 | Mac Mini | 每周日 4:00 | ~/graphiti-cleanup-orphans.sh |
| Gateway 日志轮转 | Mac Mini | 每周日 3:00 | ~/.openclaw/logs/rotate-logs.sh |
| conversation 压缩 | MacBook | 每月1日 3:00 | compact_v3.py |
| 容量告警检查 | MacBook | 每周一 9:00 | capacity_alert.py |
| 记忆健康度报告 | MacBook | 每周一 9:05 | weekly_stats.py |

## 分类与重要性映射

| category | importance | 说明 |
|----------|-----------|------|
| project / architecture / solution | high | 项目关键信息 |
| preference / debug / feedback / decision | high | 用户偏好与修复 |
| summary | high | 压缩后的对话摘要 |
| fact / general | medium | 一般性知识 |
| conversation | low | 原始对话（会被压缩） |
| other | low | 杂项 |
