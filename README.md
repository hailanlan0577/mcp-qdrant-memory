# 统一记忆系统 v3.5

基于 Qdrant 向量数据库 + Graphiti 知识图谱的 AI 永久记忆系统。Claude Code（MCP Server）和 OpenClaw（飞书插件）双端接入，统一集合，记忆双向互通。

---

## 有无记忆系统的区别

### 没有记忆系统

| | Claude Code (MacBook Pro) | OpenClaw (Mac Mini 飞书) |
|---|---|---|
| 长期记忆 | 无，关窗口全忘 | 无，对话结束即失忆 |
| 跨会话检索 | 只有 MEMORY.md（200行手写笔记） | 无 |
| 跨设备互通 | 不知道飞书聊了什么 | 不知道 Claude Code 聊了什么 |
| 语义搜索 | 无 | 无 |
| 知识推理 | 无法回答"谁供的货""哪些包降过价" | 无 |

### 只有 Qdrant V3（向量语义记忆）

| | Claude Code | OpenClaw |
|---|---|---|
| 长期记忆 | 每轮问答自动存入向量数据库，永久保留 | 飞书对话自动沉淀（autoCapture） |
| 跨会话检索 | 语义搜索，搜"包的管理"能找到"包务模块" | autoRecall 自动注入相关记忆 |
| 跨设备互通 | 统一集合 `unified_memories_v3`，两侧共享 | 同左 |
| 语义理解 | 阿里云 text-embedding-v4，1024维，8192 Token | 同左 |
| 知识推理 | **做不到** — 只能按相似度找文本，无法理解实体关系 | 同左 |

### Qdrant V3 + Graphiti（当前架构）

| | Claude Code | OpenClaw |
|---|---|---|
| 长期记忆 | Qdrant 向量 + Graphiti 知识图谱，双写 | 同左 |
| 跨会话检索 | hybrid_search 融合搜索，两个系统并行查询 | autoRecall 注入 Qdrant 记忆 + Graphiti 图谱 |
| 跨设备互通 | 统一集合 + 统一图谱，两侧共享 | 同左 |
| **实体关系** | Graphiti 自动提取人物、品牌、事件并建立关系 | 同左 |
| **时序推理** | 知道"赵姐第4次购买""阿强要求今天内答复" | 同左 |
| **关联查询** | 搜"张哥"能发现他是供货方（SUPPLIED_BY 关系） | 同左 |

### 一句话总结

- **没有记忆**：两台机器各自失忆，每次从零开始
- **只有 Qdrant**：有了永久记忆和语义搜索，但只能找相似文本，不懂实体关系
- **Qdrant + Graphiti**：不仅记住内容，还理解"谁、什么、什么关系"，能做跨实体推理

---

## 架构

```
MacBook Pro (Claude Code)                    Mac Mini
│                                            │
├── Claude Code 会话                          ├── OpenClaw Gateway (launchd: ai.openclaw.gateway)
│   │                                        │   │
│   ├── MCP: qdrant-memory-v3 (stdio)        │   ├── 插件: openclaw-memory-qdrant-v3
│   │   ├── store_memory ──────────────┐     │   │   ├── autoRecall (before_agent_start)
│   │   ├── search_memory              │     │   │   │   └── 注入 Qdrant top10 + Graphiti 图谱
│   │   ├── keyword_search             │     │   │   ├── autoCapture (agent_end)
│   │   ├── hybrid_search ─────┐       │     │   │   │   └── 双写 Qdrant + Graphiti
│   │   └── ...                │       │     │   │   └── 工具: store/search/keyword/hybrid/...
│   │                          │       │     │   │
│   ├── MCP: graphiti (HTTP)   │       │     │   └── 插件: openclaw-memory-graphiti
│   │   ├── add_memory ────────┼───┐   │     │       └── autoCapture → Graphiti
│   │   ├── search_nodes       │   │   │     │
│   │   └── search_memory_facts│   │   │     │
│   │                          │   │   │     │
│   └── 双写: Qdrant + Graphiti│   │   │     │
│                              │   │   │     │
│   ┌──────────────────────────┘   │   │     │
│   │  hybrid_search 内部:         │   │     │
│   │  ├── 线程1: Qdrant 向量搜索  │   │     │
│   │  └── 线程2: Graphiti 图谱搜索│   │     │
│   │      (Streamable HTTP)       │   │     │
│   └──────────────────────────────┘   │     │
│                                      │     │
└──── SSH Tunnel ──────────────────────┼─────┘
                                       │
              ┌────────────────────────┼────────────────┐
              │                        │                │
              ▼                        ▼                ▼
    ┌──────────────────┐    ┌──────────────┐    ┌──────────────┐
    │ Qdrant :6333      │    │ Graphiti MCP │    │ Neo4j :7687  │
    │                   │    │ :8001 (HTTP) │    │              │
    │ unified_memories  │    │              │───►│ 实体节点      │
    │ _v3               │    │ text→实体→关系│    │ 关系事实      │
    │                   │    │              │    │ 时序元数据    │
    │ 1024维 embedding  │    └──────────────┘    └──────────────┘
    │ text-embedding-v4 │
    └──────────────────┘
```

### Qdrant vs Graphiti 各自的角色

| | Qdrant 向量数据库 | Graphiti 知识图谱 |
|---|---|---|
| 存什么 | 完整对话文本的向量编码 | 从文本中提取的实体、关系、事实 |
| 怎么搜 | 向量余弦相似度（语义搜索） | 图遍历（实体关系查询） |
| 擅长 | "找类似的内容"（模糊匹配） | "找关联的事物"（精确推理） |
| 举例 | 搜"退货"→找到所有退货相关对话 | 搜"张哥"→发现他是供货方，供了 YSL 链条包 |
| 弱点 | 不理解实体关系 | 不擅长语义模糊匹配 |
| 数据格式 | `{content, category, tags, importance, source, embedding}` | `{节点, 边, 事实, 时间戳}` |
| 底层存储 | Qdrant (port 6333) | Neo4j (port 7687) |

**fusion search（融合搜索）把两者并行查询，统一返回**，弥补各自弱点。

---

## 数据流

### Claude Code 侧

```
用户提问
  │
  ├── 手动存储 ──► store_memory ──► Qdrant V3
  │               add_memory ───► Graphiti
  │
  ├── 搜索 ──► search_memory ──► Qdrant V3 (向量语义)
  │           keyword_search ──► Qdrant V3 (精确关键词)
  │           hybrid_search ──► Qdrant V3 + Graphiti (融合)
  │
  └── Graphiti 单独查询:
      search_nodes ──────► 实体节点
      search_memory_facts ► 关系事实
```

工具列表：

| 工具 | 说明 |
|------|------|
| `store_memory` | 存储记忆，自动去重（相似度>0.92跳过），自动 importance 分级 |
| `search_memory` | 向量语义搜索 + importance 加权 + 时间衰减 + 语义去重 |
| `keyword_search` | 关键词精确搜索（content + tags），按重要性+时间排序 |
| `hybrid_search` | **融合搜索**：并行查询 Qdrant 向量 + Graphiti 知识图谱，统一返回 |
| `delete_memory` | 精确内容删除或语义模糊删除 |
| `update_memory` | 原地更新记忆，保留原始 ID 和创建时间 |
| `list_memories` | 按分类浏览所有记忆 |
| `compact_conversations` | 压缩旧 conversation 记忆，按天合并为摘要 |
| `memory_stats` | 统计信息（60秒 TTL 缓存） |
| `search_openclaw_memory` | 搜索 OpenClaw 来源的记忆（source=openclaw） |
| `global_search` | 全局搜索（忽略 source 过滤） |

### OpenClaw 飞书侧

```
用户在飞书发消息
  │
  ├── before_agent_start (autoRecall)
  │   ├── Qdrant V3 向量搜索 → 注入 top 10 条相关记忆
  │   └── Graphiti 搜索 → 注入知识图谱实体和关系
  │
  ├── AI 处理并回复
  │   └── AI 可主动调用工具: memory_store / memory_search / hybrid_search
  │
  └── agent_end (autoCapture, 双写)
      ├── Qdrant V3: stripInjectedTags → embedding → dedup → upsert
      └── Graphiti: add_memory → 实体提取 → 图谱更新
```

autoCapture 关键处理：

| 步骤 | 说明 |
|------|------|
| `stripInjectedTags` | 剥离 `<your-memories>` 和 `<graphiti-knowledge>` 注入标签，防止 dedup 误判 |
| `DEDUP_THRESHOLD` | 0.92，embedding 相似度超过此值视为重复，跳过存储 |
| 噪音过滤 | 跳过图片、心跳、系统消息 |
| 短文本过滤 | cleanUserText < 5 字符跳过 |

---

## 搜索策略指南

| 场景 | 推荐工具 | 原因 |
|------|---------|------|
| 模糊/语义搜索（"退货相关的包"） | `search_memory` | 向量相似度，语义理解强 |
| 精确关键词（"PR200156"、"2026-03-26"） | `keyword_search` | 文本精确匹配 |
| 实体关系（"张哥和我们什么关系"） | `hybrid_search` | Graphiti 图谱推理 |
| 跨项目关联（"哪些供应商供过货"） | `hybrid_search` | Graphiti 关系遍历 |
| 什么都搜不到时 | 两个都试 | 向量和关键词互补 |

---

## v3.5 核心升级（相对 v3.0）

| | v3.0 | v3.5 |
|---|---|---|
| **知识图谱** | 无 | **Graphiti (Neo4j)，自动实体提取和关系建立** |
| **融合搜索** | 无 | **hybrid_search (Qdrant + Graphiti 并行)** |
| **集合** | 分离 (claude-memory-v3 + openclaw_memories_v3) | **统一 (unified_memories_v3)** |
| **autoCapture** | 直接存 userText（含注入标签） | **stripInjectedTags 后存 cleanText** |
| **Graphiti 协议** | 无 | **Streamable HTTP (POST /mcp + Mcp-Session-Id)** |
| **存档恢复** | 无 | **healthcheck.sh + save_restore.sh** |

## v3.0 vs v2.3

| | v2.3 | v3.0 |
|---|---|---|
| **Embedding** | 本地小模型 (bge-small-zh / all-MiniLM-L6-v2) | **阿里云 text-embedding-v4** |
| **向量维度** | 512 / 384 | **1024** |
| **最大 Token** | 512 | **8,192** |
| **集合** | 分离 (claude-memory + openclaw_memories) | **统一 (unified_memories_v3)** |
| **知识图谱** | 无 | **Graphiti (Neo4j)** |
| **融合搜索** | 无 | **hybrid_search (Qdrant + Graphiti)** |
| **跨系统搜索** | 关键词匹配（维度不同） | **统一集合，source 字段区分** |
| **autoCapture** | 直接存 userText | **stripInjectedTags 后存 cleanText** |

---

## 文件说明

### Claude Code MCP Server

| 文件 | 说明 |
|------|------|
| `server_v3.py` | **当前使用** — V3 MCP Server, 含 hybrid_search |
| `healthcheck.sh` | 11项诊断脚本，支持 `--fix` 自动修复 SSH tunnel |
| `server_v2_1.py` | V2.1 版本（历史备份） |
| `server_v2.py` | V2.0 版本（历史备份） |
| `server.py` | V1.0 版本（历史备份） |
| `requirements.txt` | Python 依赖 |

### OpenClaw Plugin (Mac Mini)

| 目录/文件 | 说明 |
|------|------|
| `~/.openclaw/extensions/openclaw-memory-qdrant-v3/` | **当前使用** — V3 插件 |
| `~/.openclaw/extensions/openclaw-memory-graphiti/` | Graphiti 知识图谱插件 |
| `~/openclaw-snapshots/save_restore.sh` | 存档/恢复/健康检查脚本 |

### 工具脚本

| 文件 | 说明 |
|------|------|
| `migrate_to_v3.py` | Claude Code 集合迁移 |
| `migrate_openclaw_v3.py` | OpenClaw 集合迁移 |
| `compact_v3.py` | 记忆压缩/合并工具 |
| `capacity_alert.py` | 容量告警 |
| `cleanup_text_field.py` | 清理文本字段 |

---

## 部署

### 前置要求

- **Qdrant** 运行在 Mac Mini (localhost:6333)
- **Neo4j** 运行在 Mac Mini (localhost:7687) — Graphiti 依赖
- **Graphiti MCP Server** 运行在 Mac Mini (port 8001, `--transport http`)
- **SSH Tunnel**: MacBook Pro localhost:18001 → Mac Mini :8001（Graphiti 访问）
- **阿里云 DashScope API Key**（text-embedding-v4）

### Claude Code MCP Server

```bash
cd ~/mcp-qdrant-memory
pip install -r requirements.txt
```

在 `~/.claude.json` 中配置两个 MCP：

```jsonc
{
  "mcpServers": {
    // Qdrant V3 向量记忆 (stdio, 本机 Python)
    "qdrant-memory-v3": {
      "command": "python",
      "args": ["/Users/chenyuanhai/mcp-qdrant-memory/server_v3.py"],
      "env": {
        "DASHSCOPE_API_KEY": "sk-xxx"
      }
    },
    // Graphiti 知识图谱 (HTTP, 通过 SSH tunnel)
    "graphiti": {
      "type": "streamableHttp",
      "url": "http://localhost:18001/mcp"
    }
  }
}
```

### OpenClaw Plugin (Mac Mini)

`~/.openclaw/openclaw.json` 中启用两个插件：

```jsonc
{
  "plugins": {
    "entries": {
      // Qdrant V3 向量记忆
      "openclaw-memory-qdrant-v3": {
        "enabled": true,
        "config": {
          "qdrantUrl": "http://localhost:6333",
          "collectionName": "unified_memories_v3",
          "autoRecall": true,
          "autoCapture": true
        }
      },
      // Graphiti 知识图谱
      "openclaw-memory-graphiti": {
        "enabled": true,
        "config": {
          "graphitiUrl": "http://localhost:8001/mcp",
          "groupId": "openclaw",
          "autoCapture": true
        }
      }
    }
  }
}
```

重启 gateway：

```bash
launchctl stop ai.openclaw.gateway && sleep 2 && launchctl start ai.openclaw.gateway
```

---

## 存档与恢复

### Claude Code 侧

```bash
cd ~/mcp-qdrant-memory
./healthcheck.sh          # 11项诊断
./healthcheck.sh --fix    # 自动修复 SSH tunnel
git log --oneline -5      # 查看版本
git checkout <commit>     # 回滚
```

### Mac Mini OpenClaw 侧

```bash
~/openclaw-snapshots/save_restore.sh check              # 7项健康检查
~/openclaw-snapshots/save_restore.sh save "说明"         # 创建快照
~/openclaw-snapshots/save_restore.sh list                # 列出快照
~/openclaw-snapshots/save_restore.sh restore <快照名>    # 恢复（自动先备份）
```

---

## importance 权重

| 分类 | importance | 搜索权重 |
|------|-----------|---------|
| project / architecture / solution / preference / debug / feedback / decision | high | 1.3x |
| fact / general | medium | 1.0x |
| conversation | low | 0.7x |

---

## 使用场景和玩法

这套系统的核心价值：**日常碎片化对话，自动变成结构化的商业智能**。不需要手动录入 ERP，不需要填表，正常聊天就好——记忆系统在背后默默积累、整理、关联。

### 飞书个人对话 → 随身智能助理

**语音备忘录 → 结构化知识**

飞书支持语音转文字。在外面看货时语音说"刚看了一只 Birkin 25 奶昔白，阿姨报价 85000，皮面有个小划痕，我觉得最多值 78000"，OpenClaw 自动：
- 存入 Qdrant（语义可搜）
- Graphiti 建立实体：Birkin 25 → 报价 85000 → 供应商"阿姨" → 评估 78000
- 之后在 Claude Code 问"阿姨最近报了什么货"，全部调出来

**碎片化决策积累**

每天飞书随口说"Gucci 最近不好卖""粉色的以后别拿了""Kelly 比 Birkin 好出手"——这些碎片 Graphiti 都会提取成关系事实。三个月后问"哪些品牌好卖哪些不好卖"，它能给出一张完整的品牌热度图谱。

### 飞书群 → 团队知识沉淀

**群聊自动纪要**

把 OpenClaw bot 拉进业务群。团队讨论"下周要不要参加二手包拼场""那只 Kelly 要不要降价"，讨论结束后所有决策、分歧、待办自动进入记忆。下次谁问"上次拼场的结论是什么"，bot 直接答。

**多人协作记忆**

店员小王在群里说"张姐今天来看了 CF 但嫌贵没买"，老板在 Claude Code 问"张姐最近什么情况"，这条自动出来。团队的碎片信息变成共享知识。

**客户跟进提醒**

OpenClaw 支持 Cron 定时任务。Graphiti 知道"孙姐下周三来付款"，可以设定周二晚自动飞书提醒"明天孙姐要来取 Celine Nano Luggage，记得准备好"。

### Claude Code → 深度分析引擎

**经营复盘**

月底问"这个月成交了几单，总利润多少，哪些亏了"，hybrid_search 从 Qdrant 拉出所有交易记录，Graphiti 拉出所有成交关系，自动生成月报。

**客户画像**

问"赵姐是什么样的客户"，Graphiti 返回：
- 购买 4 次（Chanel Boy, CF, ...）
- 偏好小尺寸经典款
- 付款方式都是微信转账
- 平均客单价 25000

这些全是从碎片对话中自动积累的，从不需要手动录入。

**供应链分析**

问"我的供应商都有谁，各自供了什么"，Graphiti 图谱直接出关系网：

```
阿强 → Balenciaga City
老周 → 日本回流 LV/Chanel
李姐 → Hermès Kelly
张哥 → YSL 链条包
```

进一步问"哪个供应商最靠谱"，结合成交记录和退货记录做分析。

**定价策略**

问"Gucci Marmont 历史售价和成交情况"，系统拉出所有记录：
- 粉色：进价 3800，压货 82 天，降到 3200 出
- 裸粉：进价 31000，持有 6 个月，亏 6000
- 黑色：卖完了
- → 结论：**Marmont 只拿黑色，非经典色系必亏**

### 跨端联动

**飞书收货 → Claude Code 定价**

在外看货，飞书语音"新到一只 Dior Saddle 老花，进价 12000"。回到电脑前，Claude Code 自动知道这只包，还能搜历史："Dior Saddle 上次卖了 21000，周转 1 天"，帮你定价。

**Claude Code 分析 → 飞书执行**

Claude Code 分析出"Celine Luggage 压货 100 天了，建议降到 11000 参加拼场"，在飞书群直接说"帮我发一条降价通知到客户群"。

**飞书客户咨询 → 自动查库存**

客户飞书问"有没有黑色的 Chanel"，OpenClaw 自动从记忆中调出所有在库的黑色 Chanel，带价格、成色直接回复。

**竞品情报收集**

建一个"行业情报"飞书群，把同行报价、市场动态随手发进去。Graphiti 自动提取品牌、价格、趋势，Claude Code 可以问"最近市场上 Hermès 的价格趋势怎样"。

### 更远的想象

| 场景 | 实现方式 |
|------|---------|
| **自动周报** | Cron 每周日晚自动生成：本周成交 X 单，利润 X 元，滞销包 X 只，降价建议。飞书推送 |
| **客户生日营销** | 记忆积累客户信息后，Cron 提醒"赵姐下周生日，推荐发个新到货消息" |
| **新员工培训** | 新店员问 bot"热销品是哪些""常见客户有哪些"，记忆系统秒答。经营经验变成可查询的知识库 |
| **多店扩展** | 每个分店一个飞书群+一个 bot，数据汇入同一个 Qdrant + Graphiti。总部 Claude Code 看全局 |
| **智能调货** | 跨店库存可见，A 店的滞销款在 B 店可能是爆款，系统自动建议调货 |

---

## 版本历史

### v3.5 (2026-03-26) — 当前版本

- **Graphiti 知识图谱集成**: 双写 Qdrant + Graphiti，自动实体提取和关系建立
- **hybrid_search 融合搜索**: Qdrant 向量 + Graphiti 图谱并行查询，统一返回
- **统一集合**: `unified_memories_v3`，Claude Code 和 OpenClaw 共享，source 字段区分
- **Streamable HTTP**: hybrid_search 内部调 Graphiti 从 SSE 改为 HTTP 协议
- **autoCapture 修复**: stripInjectedTags 剥离 recall 注入标签，防止 dedup 误判
- **存档恢复**: healthcheck.sh（Claude Code）+ save_restore.sh（Mac Mini）

### v3.0 (2026-03-20)

- **Embedding 升级**: 阿里云 text-embedding-v4, 1024维, 8192 Token
- **新集合**: claude-memory-v3 + openclaw_memories_v3
- **query/document 区分**: 搜索用 query 类型，存储用 document 类型

### v2.3 (2026-03-19)

- 双向跨系统搜索: Claude Code <-> OpenClaw
- 时间感知 autoRecall
- Plugin ID 修复

### v2.1 (2026-03-17)

- 自动去重（相似度 > 0.92 跳过）
- importance 自动分级 + 加权搜索
- 关键词搜索 + 语义模糊删除

### v1.0 (2026-03-13)

- 初始版本，基础 store/search/delete/list
