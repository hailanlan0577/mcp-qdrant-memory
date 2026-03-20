# Qdrant Memory System v3.0

基于 Qdrant 向量数据库的 AI 永久记忆系统，支持 Claude Code（MCP Server）和 OpenClaw（插件）双端接入，记忆双向互通。

## 有 Qdrant V3 vs 没有 Qdrant V3

### 没有 Qdrant V3

#### MacBook Pro 上的 Claude Code

- **没有长期记忆** — 每次打开新窗口，就是一个全新的 Claude，不记得之前聊过什么
- **只有当前会话上下文** — 只能记住这一次对话里的内容，窗口一关全忘
- **本地文件记忆** — 有个 `MEMORY.md` 文件可以手动写一些笔记，但容量小（200行截断），而且是纯文本，不能语义搜索
- **无法跨会话检索** — 你上周做了什么决策、讨论了什么架构，Claude 完全不知道，除非你自己复述一遍
- **无法读取 OpenClaw 的对话** — Mac Mini 上飞书聊了什么，Claude Code 这边一无所知

#### Mac Mini 上的 OpenClaw

- **也没有长期记忆** — 飞书对话结束后，下次再问它就忘了
- **群聊内容不沉淀** — 飞书群里讨论的业务决策、需求分析、架构讨论，聊完就散了
- **无法回忆之前的讨论** — 你问"昨天聊的飞书联动模块是什么"，它答不上来
- **无法被 Claude Code 查询** — MacBook Pro 这边想了解飞书上聊过什么，没有任何途径

### 有了 Qdrant V3

#### MacBook Pro 上的 Claude Code

- **有了永久记忆** — 每轮问答自动存入向量数据库，关掉窗口也不会丢
- **跨会话语义搜索** — 新窗口打开后，可以搜到之前任何一次对话的内容。搜"飞书联动"就能把所有相关讨论调出来
- **大模型级中文理解** — 用阿里云 text-embedding-v4（Qwen3-Embedding），不是简单关键词匹配，而是真正理解语义。搜"包的管理系统"也能找到"包务模块"相关的内容
- **长文本完整覆盖** — 支持 8192 Token，一整轮详细的问答都能完整索引，搜索时能命中内容的任何部分
- **能调取 OpenClaw 的记忆** — 通过 `search_openclaw_memory`，可以搜索 Mac Mini 上飞书聊天的历史内容
- **自动分级** — 重要的架构决策、解决方案自动标记为 high importance，日常闲聊标记为 low，搜索时重要内容优先展示

#### Mac Mini 上的 OpenClaw

- **飞书聊天有了记忆** — 群里讨论的需求、决策、业务细节自动沉淀
- **能回忆之前的讨论** — 你在飞书问"昨天聊的二奢软件回忆一下"，它能完整回忆出来
- **中文语义搜索质量高** — 和 Claude Code 用同一个大模型 embedding（1024维），中文理解能力强
- **能被 Claude Code 跨机器查询** — MacBook Pro 上随时可以调取飞书上的讨论内容，两台机器的知识打通了

### 一句话总结

**没有 V3**：两台机器各自失忆，每次都从零开始，互相不知道对方聊了什么。

**有了 V3**：两台机器都有了永久记忆，能跨会话回忆，而且知识互通。

## v3.0 核心升级

| | v2.3 | v3.0 |
|---|---|---|
| **Embedding 模型** | 本地小模型 (bge-small-zh / all-MiniLM-L6-v2) | **阿里云 text-embedding-v4** |
| **向量维度** | 512 / 384 | **1024** |
| **最大 Token** | 512 | **8,192** |
| **语义理解** | 基础 | **大模型级别** |
| **Claude Code 集合** | claude-memory | **claude-memory-v3** |
| **OpenClaw 集合** | openclaw_memories | **openclaw_memories_v3** |
| **跨系统搜索** | 关键词匹配（维度不同） | **关键词匹配（统一维度，可扩展向量搜索）** |

## 架构

```
┌─────────────────────┐          ┌─────────────────────┐
│   MacBook Pro        │          │   Mac Mini           │
│                     │          │                     │
│  Claude Code        │          │  OpenClaw Gateway   │
│  ├─ server_v3.py    │          │  ├─ index.js (V3)   │
│  │  (MCP Server)    │          │  │  (Plugin)         │
│  │                  │          │  │                   │
│  │  search_memory ──┼──────────┼──┤  memory_search    │
│  │  keyword_search  │  Qdrant  │  │  memory_store     │
│  │  store_memory    │  6333    │  │  memory_keyword   │
│  │  delete_memory   │◄────────►│  │  memory_stats     │
│  │  list_memories   │          │  │  memory_forget    │
│  │  memory_stats    │          │  │                   │
│  │                  │          │  │  跨系统搜索:       │
│  │  search_openclaw ┼──────────┼──►  memory_search    │
│  │  _memory ────────┼──┐      │  │  _claude ─────────┼──┐
│  └──────────────────┘  │      │  └───────────────────┘  │
│                        │      │                         │
└────────────────────────┘      └─────────────────────────┘
            │                              │
            ▼                              ▼
   ┌─────────────────┐          ┌──────────────────────┐
   │ claude-memory-v3 │          │ openclaw_memories_v3  │
   │ 1024-dim         │          │ 1024-dim              │
   │ text-embedding-v4│          │ text-embedding-v4     │
   └─────────────────┘          └──────────────────────┘
              └──────────┬───────────┘
                    Qdrant DB
                 localhost:6333
```

## 版本历史

### v3.0 (2026-03-20) - 当前版本

**Embedding 大升级：text-embedding-v4**

- **阿里云 text-embedding-v4**: 从本地小模型升级到大模型级 embedding，语义理解大幅提升
- **1024 维向量**: 统一 Claude Code 和 OpenClaw 的向量维度
- **8192 Token 长文本**: 支持更长内容的语义编码（原 512）
- **query/document 区分**: 搜索时用 query 类型，存储时用 document 类型，优化检索准确度
- **新集合**: claude-memory-v3 + openclaw_memories_v3，旧数据保留不影响
- **数据迁移**: 提供迁移脚本，重新向量化所有旧数据到新集合
- **依赖精简**: OpenClaw 插件移除 @xenova/transformers 和 sharp，仅需 @qdrant/js-client-rest

### v2.3 (2026-03-19)

**双向记忆互通 + 时间感知回忆**

- 双向跨系统搜索: Claude Code ↔ OpenClaw
- 时间感知 autoRecall: 向量+时间融合策略
- 强化记忆注入 prompt
- Plugin ID 修复

### v2.1 (2026-03-17)

**智能去重 + importance 分级**

- 自动去重存储（相似度 > 0.92 跳过）
- importance 自动分级 + 加权搜索
- 关键词搜索 + 语义模糊删除

### v1.0 (2026-03-13)

- 初始版本，基础 store/search/delete/list

## 文件说明

### Claude Code MCP Server

| 文件 | 说明 |
|------|------|
| `server_v3.py` | **当前使用** - V3 MCP Server, text-embedding-v4 |
| `server_v2_1.py` | V2.1 版本（历史备份） |
| `server_v2.py` | V2.0 版本（历史备份） |
| `server.py` | V1.0 版本（历史备份） |
| `requirements.txt` | Python 依赖 |

### OpenClaw Plugin

| 目录 | 说明 |
|------|------|
| `openclaw-plugin-v3/` | **当前使用** - V3 插件, text-embedding-v4 |
| `openclaw-plugin/` | V2 插件（历史备份） |

### 工具脚本

| 文件 | 说明 |
|------|------|
| `migrate_to_v3.py` | Claude Code 集合迁移 (claude-memory → claude-memory-v3) |
| `migrate_openclaw_v3.py` | OpenClaw 集合迁移 (openclaw_memories → openclaw_memories_v3) |
| `backfill_importance.py` | 为 v1 旧数据补充 importance 字段 |
| `compress.py` | 记忆压缩/合并工具 |
| `migrate_from_pinecone.py` | 从 Pinecone 迁移数据到 Qdrant |
| `ssh-tunnel.sh` | SSH 隧道连接 Mac Mini Qdrant |

## MCP Server 工具列表

| 工具 | 说明 |
|------|------|
| `store_memory` | 存储记忆，自动去重（相似度>0.92跳过） |
| `search_memory` | 语义搜索 + importance 加权 + 去重 |
| `keyword_search` | 关键词精确搜索（content + tags） |
| `delete_memory` | 精确删除（MD5）或语义模糊删除 |
| `list_memories` | 按分类浏览记忆 |
| `memory_stats` | 统计信息（总数、分类、重要性分布） |
| `search_openclaw_memory` | 跨系统搜索 OpenClaw 记忆集合 |

## OpenClaw Plugin 功能

### autoRecall（自动回忆注入）

每次用户发消息时自动触发：

1. **时间性请求检测** - 匹配"回忆/昨天/之前/上次/remember"等关键词
2. **普通请求** - 向量搜索 top 5 + 关键词 fallback
3. **时间性请求** - 向量+时间融合策略：
   - Vector search top 15（话题相关性）
   - Time-based fetch recent 20（时间覆盖）
   - 合并去重，近 48h 记忆 1.3x 加权
   - 注入 top 10 条记忆

### autoCapture（自动记录）

每轮对话结束自动存储，含 importance 自动分级、噪音过滤。

### 6 个工具

`memory_store` / `memory_search` / `memory_keyword_search` / `memory_stats` / `memory_forget` / `memory_search_claude`

## 配置

### Qdrant 集合

| 集合 | 维度 | 模型 | 用途 | 状态 |
|------|------|------|------|------|
| `claude-memory-v3` | 1024 | text-embedding-v4 | Claude Code V3 | **当前使用** |
| `openclaw_memories_v3` | 1024 | text-embedding-v4 | OpenClaw V3 | **当前使用** |
| `claude-memory` | 512 | bge-small-zh-v1.5 | Claude Code V2 | 历史备份 |
| `openclaw_memories` | 384 | all-MiniLM-L6-v2 | OpenClaw V2 | 历史备份 |

### importance 权重

| 分类 | importance | 权重 |
|------|-----------|------|
| project / architecture / solution / preference / summary | high | 1.3x |
| debug / general / fact / entity | medium | 1.0x |
| conversation | low | 0.7x |

## 部署

### 前置要求

- Qdrant 数据库运行在 Mac Mini (localhost:6333)
- 阿里云 DashScope API Key（用于 text-embedding-v4）

### Claude Code MCP Server

```bash
cd mcp-qdrant-memory
pip install -r requirements.txt
export DASHSCOPE_API_KEY=sk-xxx
python server_v3.py
```

在 `~/.claude.json` 中配置：

```json
{
  "mcpServers": {
    "qdrant-memory-v3": {
      "command": "python",
      "args": ["/path/to/server_v3.py"],
      "env": {
        "DASHSCOPE_API_KEY": "sk-xxx"
      }
    }
  }
}
```

### OpenClaw Plugin

将 `openclaw-plugin-v3/` 内容复制到 Mac Mini：

```bash
tar -czf - openclaw-plugin-v3 | ssh macmini "tar -xzf - -C ~/.openclaw/extensions/ && mv ~/.openclaw/extensions/openclaw-plugin-v3 ~/.openclaw/extensions/memory-qdrant-v3"
cd ~/.openclaw/extensions/memory-qdrant-v3 && npm install
```

在 `~/.openclaw/openclaw.json` 中启用：

```json
{
  "plugins": {
    "slots": {
      "memory": "openclaw-memory-qdrant-v3"
    },
    "entries": {
      "openclaw-memory-qdrant-v3": {
        "enabled": true,
        "config": {
          "qdrantUrl": "http://localhost:6333",
          "collectionName": "openclaw_memories_v3",
          "autoRecall": true,
          "autoCapture": true,
          "captureMaxChars": 5000,
          "dashscopeApiKey": "sk-xxx"
        }
      }
    }
  }
}
```

重启 gateway：

```bash
launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/ai.openclaw.gateway.plist
```

### 数据迁移

从 V2 迁移到 V3（重新向量化所有记忆）：

```bash
# Claude Code 集合
export DASHSCOPE_API_KEY=sk-xxx
python migrate_to_v3.py

# OpenClaw 集合（纯 REST API，兼容 Python 3.9）
python3 migrate_openclaw_v3.py
```

迁移脚本会保留旧数据，确认无误后可手动删除旧集合。
