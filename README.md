# Qdrant Memory System v2.3

基于 Qdrant 向量数据库的 AI 永久记忆系统，支持 Claude Code（MCP Server）和 OpenClaw（插件）双端接入，记忆双向互通。

## 架构

```
┌─────────────────────┐          ┌─────────────────────┐
│   MacBook Pro        │          │   Mac Mini           │
│                     │          │                     │
│  Claude Code        │          │  OpenClaw Gateway   │
│  ├─ server_v2_1.py  │          │  ├─ index.js        │
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
   │ claude-memory    │          │ openclaw_memories     │
   │ 512-dim          │          │ 384-dim               │
   │ bge-small-zh     │          │ all-MiniLM-L6-v2      │
   └─────────────────┘          └──────────────────────┘
              └──────────┬───────────┘
                    Qdrant DB
                 localhost:6333
```

## 版本历史

### v2.3 (2026-03-19) - 当前版本

**双向记忆互通 + 时间感知回忆**

- **双向跨系统搜索**: Claude Code 可搜 OpenClaw 记忆 (`search_openclaw_memory`)，OpenClaw 可搜 Claude Code 记忆 (`memory_search_claude`)
- **时间感知 autoRecall**: 检测"回忆/昨天/上次/remember"等关键词，自动触发向量+时间融合策略
- **向量+时间融合**: vector search top 15 + time-based fetch recent 20，合并去重，近期记忆 1.3x 加权，注入 top 10
- **强化记忆注入 prompt**: `<your-memories>` 标签 + 明确指令防止模型说"想不起来"
- **Plugin ID 修复**: OpenClaw 插件 manifest ID 与目录名对齐

### v2.1 (2026-03-17)

**智能去重 + importance 分级**

- **自动去重存储**: 相似度 > 0.92 自动跳过，防止重复记忆
- **importance 自动分级**: category → importance 映射（high/medium/low）
- **加权搜索**: 向量相似度 × importance 权重，高重要性记忆优先
- **关键词搜索**: `keyword_search` 精确匹配，与语义搜索互补
- **模糊删除**: `delete_memory` 支持语义模糊匹配删除

### v2.0 (2026-03-17)

- 从 v1 升级，增加 importance 字段和加权排序

### v1.0 (2026-03-13)

- 初始版本，基础 store/search/delete/list

## 文件说明

### Claude Code MCP Server

| 文件 | 说明 |
|------|------|
| `server_v2_1.py` | **当前使用** - MCP Server，含全部 v2.3 功能 |
| `server_v2.py` | v2.0 版本（历史备份） |
| `server.py` | v1.0 版本（历史备份） |
| `requirements.txt` | Python 依赖 |

### OpenClaw Plugin

| 文件 | 说明 |
|------|------|
| `openclaw-plugin/index.js` | OpenClaw 记忆插件，含 autoRecall/autoCapture |
| `openclaw-plugin/openclaw.plugin.json` | 插件 manifest |

### 工具脚本

| 文件 | 说明 |
|------|------|
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
   - 合并去重，近 24h 记忆 1.3x 加权
   - 注入 top 10 条记忆

### autoCapture（自动记录）

每轮对话结束自动存储，含 importance 自动分级。

### 6 个工具

`memory_store` / `memory_search` / `memory_keyword_search` / `memory_stats` / `memory_forget` / `memory_search_claude`

## 配置

### Qdrant 集合

| 集合 | 维度 | 模型 | 用途 |
|------|------|------|------|
| `claude-memory` | 512 | bge-small-zh-v1.5 | Claude Code 记忆 |
| `openclaw_memories` | 384 | all-MiniLM-L6-v2 | OpenClaw 记忆 |

> 两个集合向量维度不同，跨系统搜索使用关键词匹配而非向量搜索。

### importance 权重

| 分类 | importance | 权重 |
|------|-----------|------|
| project / architecture / solution / summary | high | 1.3x |
| debug / general | medium | 1.0x |
| conversation | low | 0.7x |

## 部署

### Claude Code MCP Server

```bash
cd mcp-qdrant-memory
pip install -r requirements.txt
python server_v2_1.py
```

在 `~/.claude/settings.local.json` 中配置：

```json
{
  "mcpServers": {
    "qdrant-memory-v2.1": {
      "command": "python",
      "args": ["/path/to/server_v2_1.py"],
      "type": "stdio"
    }
  }
}
```

### OpenClaw Plugin

将 `openclaw-plugin/` 内容复制到 Mac Mini：

```bash
scp openclaw-plugin/* macmini:~/.openclaw/extensions/memory-qdrant-v2/
```

在 `~/.openclaw/openclaw.json` 中启用：

```json
{
  "plugins": {
    "slots": {
      "memory": "openclaw-memory-qdrant-v2"
    }
  }
}
```
