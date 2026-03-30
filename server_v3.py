"""
Qdrant Memory MCP Server V3
基于 V2.1，升级 embedding 模型：
  bge-small-zh-v1.5 (512维, 本地) → text-embedding-v4 (1024维, 阿里云API)

改进：
- 语义理解大幅提升（大模型级别 vs 小模型）
- 长文本支持 8192 Token（原 512）
- 支持 query/document 区分优化检索
- 新 collection claude-memory-v3，不影响旧数据
"""

import os
import sys

# 防止 httpx/qdrant 走系统代理连接本地 Qdrant
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

import hashlib
import json
import threading
import time
from datetime import datetime

import httpx
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchText,
    MatchValue,
    PointStruct,
    Range,
    TextIndexParams,
    TokenizerType,
    VectorParams,
)

# ── 配置 ──────────────────────────────────────────────
QDRANT_URL = "http://localhost:6333"
_TEST_MODE = "--test" in sys.argv
COLLECTION_NAME = "unified_memories_v3_test" if _TEST_MODE else "unified_memories_v3"
VECTOR_DIM = 1024

if _TEST_MODE:
    import logging as _log
    _log.warning(f"[server_v3] ⚠️  TEST MODE: 使用测试集合 {COLLECTION_NAME}")

# 阿里云百炼 Embedding API
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "")
EMBEDDING_MODEL = "text-embedding-v4"
EMBEDDING_API_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings"

# importance 权重
IMPORTANCE_WEIGHTS = {
    "high": 1.3,
    "medium": 1.0,
    "low": 0.7,
}

# category → importance 自动映射
CATEGORY_IMPORTANCE = {
    "project": "high",
    "architecture": "high",
    "solution": "high",
    "preference": "high",
    "debug": "high",       # debug 方案稀少珍贵，升为 high
    "feedback": "high",    # 用户纠正行为，升为 high
    "decision": "high",    # 架构/技术决策，升为 high
    "fact": "medium",
    "general": "medium",
    "other": "low",
    "conversation": "low",
    "summary": "high",
}

# 去重阈值
DEDUP_THRESHOLD = 0.92

# Graphiti MCP server（本机，HTTP transport via SSH tunnel）
GRAPHITI_BASE = "http://localhost:18001"
GRAPHITI_MCP_URL = f"{GRAPHITI_BASE}/mcp"

# ── Embedding 客户端 ─────────────────────────────────
_http_client = httpx.Client(timeout=30)


def get_embedding(text: str, text_type: str = "document") -> list[float]:
    """调用阿里云 text-embedding-v4 生成向量。

    Args:
        text: 输入文本
        text_type: "query" 用于搜索查询，"document" 用于存储文档
    """
    resp = _http_client.post(
        EMBEDDING_API_URL,
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": EMBEDDING_MODEL,
            "input": text,
            "dimensions": VECTOR_DIM,
            "encoding_format": "float",
            "extra_body": {"text_type": text_type},
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["data"][0]["embedding"]


# ── Qdrant 初始化 ────────────────────────────────────
client = QdrantClient(url=QDRANT_URL, timeout=30, check_compatibility=False)


def ensure_collection():
    """确保 collection 存在并建立索引。"""
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )

    # 建立 payload 索引（幂等操作，已存在则跳过）
    for field, schema in [
        ("content", TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            min_token_len=2,
            max_token_len=20,
        )),
        ("tags", TextIndexParams(
            type="text",
            tokenizer=TokenizerType.MULTILINGUAL,
            min_token_len=2,
            max_token_len=20,
        )),
        ("category", "keyword"),
        ("importance", "keyword"),
        ("source", "keyword"),
        ("created_at", "keyword"),
        ("timestamp", "integer"),
    ]:
        try:
            client.create_payload_index(
                collection_name=COLLECTION_NAME,
                field_name=field,
                field_schema=schema,
            )
        except Exception:
            pass


try:
    ensure_collection()
except Exception as _e:
    import logging as _logging
    _logging.warning(f"[server_v3] ensure_collection failed at startup (Qdrant not ready?): {_e}")

mcp = FastMCP(
    "Claude Memory V3",
    instructions="""
这是 Claude 的永久向量记忆系统 V3。升级：embedding 模型从本地小模型升级为阿里云 text-embedding-v4。

改进：
- 语义理解大幅提升（大模型级 embedding）
- 长文本支持 8192 Token（原 512）
- query/document 区分优化检索准确度

工具：
1. **store_memory**: 存储记忆，自动去重（相似度>0.92自动跳过），自动 importance 分级
2. **search_memory**: 智能搜索，向量语义 + 重要性加权 + 时间衰减 + 语义去重
3. **keyword_search**: 精确关键词搜索，按重要性+时间排序，适合搜特定术语、项目名、工具名
4. **hybrid_search**: 融合搜索，并行查询 Qdrant 向量 + Graphiti 知识图谱，统一返回
5. **delete_memory**: 支持精确内容删除和语义模糊删除
6. **update_memory**: 原地更新记忆，保留原始 ID 和创建时间
7. **list_memories**: 按分类浏览所有记忆
8. **compact_conversations**: 压缩旧 conversation 记忆，按天合并为摘要
9. **memory_stats**: 查看记忆统计信息（60秒 TTL 缓存）

搜索策略：
- 模糊/语义搜索 → 用 search_memory
- 精确关键词 → 用 keyword_search
- 实体关系/跨项目关联 → 用 hybrid_search
- 找不到时两个都试试
""",
)


def make_id(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def get_importance(category: str) -> str:
    """根据 category 自动判定 importance。"""
    return CATEGORY_IMPORTANCE.get(category, "medium")


def time_decay_factor(timestamp: int) -> float:
    """根据记忆年龄计算时间衰减系数。越新越高，越旧越低。"""
    now = int(time.time())
    days_old = max(0, (now - timestamp) / 86400)
    if days_old <= 7:
        return 1.2      # 近7天：加权
    elif days_old <= 30:
        return 1.0      # 7-30天：中性
    elif days_old <= 90:
        return 0.9      # 1-3个月：轻微降权
    elif days_old <= 365:
        return 0.8      # 3-12个月：降权
    else:
        return 0.7      # 1年以上：较低权重


def weighted_score(score: float, importance: str, timestamp: int = 0) -> float:
    """对原始向量相似度分数加权（重要性 × 时间衰减）。"""
    weight = IMPORTANCE_WEIGHTS.get(importance, 1.0)
    decay = time_decay_factor(timestamp) if timestamp > 0 else 1.0
    return score * weight * decay


def deduplicate(results: list, threshold: float = 0.75) -> list:
    """去重：文本相似度超过阈值的只保留加权分最高的。

    使用 SequenceMatcher 计算内容相似度（取前500字符），
    比旧的前100字符精确匹配更准确。
    """
    from difflib import SequenceMatcher

    if len(results) <= 1:
        return results

    # 按加权分降序，高分优先保留
    sorted_results = sorted(results, key=lambda x: x["weighted_score"], reverse=True)
    kept = []
    for item in sorted_results:
        is_dup = False
        content_a = item["content"][:500]
        for existing in kept:
            content_b = existing["content"][:500]
            ratio = SequenceMatcher(None, content_a, content_b).ratio()
            if ratio >= threshold:
                is_dup = True
                break
        if not is_dup:
            kept.append(item)

    return kept


@mcp.tool()
def store_memory(content: str, category: str = "general", tags: str = "", source: str = "claude_code") -> str:
    """存储一条记忆到永久向量数据库（V3，text-embedding-v4）。

    Args:
        content: 要记住的内容，尽量描述清楚上下文
        category: 分类，如 project/preference/solution/architecture/debug/general/conversation/summary
        tags: 逗号分隔的标签，如 "python,react,中国象棋"
        source: 来源标识，如 "claude_code" 或 "openclaw"，默认 claude_code
    """
    importance = get_importance(category)
    embedding = get_embedding(content, text_type="document")

    # 去重检查
    existing = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        limit=1,
        with_payload=True,
    )
    if existing.points:
        top = existing.points[0]
        if top.score >= DEDUP_THRESHOLD:
            return (
                f"跳过存储：已存在相似记忆 (相似度: {top.score:.3f})\n"
                f"  已有内容: {top.payload.get('content', '')[:120]}..."
            )

    memory_id = make_id(content)
    metadata = {
        "content": content,
        "category": category,
        "tags": tags,
        "importance": importance,
        "source": source,
        "created_at": datetime.now().isoformat(),
        "timestamp": int(time.time()),
        "version": "v3",
    }
    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[
            PointStruct(
                id=memory_id,
                vector=embedding,
                payload=metadata,
            )
        ],
    )
    return f"记忆已存储 [ID: {memory_id[:8]}] 分类: {category} 重要性: {importance}"


@mcp.tool()
def search_memory(
    query: str,
    category: str = "",
    top_k: int = 5,
    source: str = "",
    date_from: str = "",
    date_to: str = "",
) -> str:
    """智能搜索记忆（向量语义 + 重要性加权 + 去重）。

    Args:
        query: 搜索内容，用自然语言描述你想找什么
        category: 可选，限定搜索分类
        top_k: 返回结果数量，默认5条
        source: 可选，限定来源：claude_code / openclaw，不填则搜全部
        date_from: 可选，起始日期（YYYY-MM-DD），只搜该日期之后的记忆
        date_to: 可选，结束日期（YYYY-MM-DD），只搜该日期之前的记忆
    """
    embedding = get_embedding(query, text_type="query")

    fetch_k = top_k * 3

    must_conditions = []
    if category:
        must_conditions.append(FieldCondition(key="category", match=MatchValue(value=category)))
    if source:
        must_conditions.append(FieldCondition(key="source", match=MatchValue(value=source)))
    if date_from:
        try:
            parts = [int(x) for x in date_from.split("-")]
            ts_from = int(datetime(*parts, 0, 0, 0).timestamp())
            must_conditions.append(FieldCondition(key="timestamp", range=Range(gte=ts_from)))
        except Exception:
            pass
    if date_to:
        try:
            parts = [int(x) for x in date_to.split("-")]
            ts_to = int(datetime(*parts, 23, 59, 59).timestamp())
            must_conditions.append(FieldCondition(key="timestamp", range=Range(lte=ts_to)))
        except Exception:
            pass
    query_filter = Filter(must=must_conditions) if must_conditions else None

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=query_filter,
        limit=fetch_k,
        with_payload=True,
    )

    if not results.points:
        return "没有找到相关记忆。"

    scored = []
    for point in results.points:
        payload = point.payload
        importance = payload.get("importance", "medium")
        ts = payload.get("timestamp", 0)
        w_score = weighted_score(point.score, importance, ts)
        scored.append({
            "raw_score": point.score,
            "weighted_score": w_score,
            "importance": importance,
            "content": payload.get("content", ""),
            "category": payload.get("category", ""),
            "tags": payload.get("tags", ""),
            "created_at": payload.get("created_at", "")[:10],
        })

    scored = deduplicate(scored)
    scored.sort(key=lambda x: x["weighted_score"], reverse=True)
    scored = scored[:top_k]

    memories = []
    for item in scored:
        imp_tag = {"high": "★", "medium": "☆", "low": "·"}.get(item["importance"], "·")
        memories.append(
            f"[{imp_tag} {item['weighted_score']:.2f}] [{item['category']}] "
            f"{item['content']}\n"
            f"  标签: {item['tags']} | 时间: {item['created_at']}"
        )
    return "\n\n".join(memories)


@mcp.tool()
def keyword_search(keyword: str, category: str = "", limit: int = 5) -> str:
    """按关键词精确搜索记忆内容。适合搜特定项目名、工具名、术语、日期（如2026-03-19）。

    Args:
        keyword: 要搜索的关键词或日期
        category: 可选，限定搜索分类
        limit: 返回数量，默认5条
    """
    import re

    # 构建 OR 条件：content 或 tags 命中均可
    should_conditions = [
        FieldCondition(key="content", match=MatchText(text=keyword)),
        FieldCondition(key="tags", match=MatchText(text=keyword)),
    ]

    # 日期模式检测：支持 2026-03-19、2026/03/19 格式
    date_match = re.match(r"^(\d{4})[-/](\d{2})[-/](\d{2})$", keyword)
    if date_match:
        year, month, day = int(date_match.group(1)), int(date_match.group(2)), int(date_match.group(3))
        day_start = int(datetime(year, month, day, 0, 0, 0).timestamp())
        day_end = int(datetime(year, month, day, 23, 59, 59).timestamp())
        should_conditions.append(
            FieldCondition(key="timestamp", range=Range(gte=day_start, lte=day_end))
        )

    must_conditions = []
    if category:
        must_conditions.append(
            FieldCondition(key="category", match=MatchValue(value=category))
        )

    # 多取一些再排序（scroll 本身无排序）
    fetch_limit = limit * 3
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(should=should_conditions, must=must_conditions if must_conditions else None),
        limit=fetch_limit,
        with_payload=True,
    )

    if not results:
        return f"没有找到包含 '{keyword}' 的记忆。"

    # 按重要性权重 + 时间倒序排序
    imp_order = {"high": 3, "medium": 2, "low": 1}
    sorted_results = sorted(
        results,
        key=lambda p: (
            imp_order.get(p.payload.get("importance", "medium"), 1),
            p.payload.get("timestamp", 0),
        ),
        reverse=True,
    )[:limit]

    memories = []
    for point in sorted_results:
        payload = point.payload
        importance = payload.get("importance", "medium")
        imp_tag = {"high": "★", "medium": "☆", "low": "·"}.get(importance, "·")
        memories.append(
            f"[{imp_tag}] [{payload.get('category', '')}] "
            f"{payload.get('content', '')}\n"
            f"  标签: {payload.get('tags', '')} | 时间: {payload.get('created_at', '')[:10]}"
        )
    return "\n\n".join(memories)


@mcp.tool()
def delete_memory(content: str = "", query: str = "") -> str:
    """删除记忆。支持精确删除和语义模糊删除。

    Args:
        content: 精确删除 — 传入完整的记忆内容（MD5 匹配）
        query: 模糊删除 — 用自然语言描述要删的记忆，会找到最相似的一条删除
    """
    if not content and not query:
        return "请提供 content（精确删除）或 query（模糊删除）其中一个参数。"

    if content:
        memory_id = make_id(content)
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=[memory_id],
        )
        return f"记忆已精确删除 [ID: {memory_id[:8]}]"

    embedding = get_embedding(query, text_type="query")
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        limit=1,
        with_payload=True,
    )

    if not results.points:
        return "没有找到匹配的记忆。"

    top = results.points[0]
    if top.score < 0.5:
        return f"最相似的记忆相关度太低 ({top.score:.3f})，未删除。请用更精确的描述重试。"

    preview = top.payload.get("content", "")[:120]
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=[top.id],
    )
    return (
        f"已删除最相似的记忆 (相似度: {top.score:.3f})\n"
        f"  内容: {preview}..."
    )


@mcp.tool()
def update_memory(query: str, new_content: str, new_category: str = "", new_tags: str = "") -> str:
    """原地更新一条记忆，保留原始创建时间和 ID。

    Args:
        query: 用自然语言描述要更新的记忆（语义匹配找到最相似的一条）
        new_content: 更新后的完整内容
        new_category: 可选，更新分类（不填则保留原分类）
        new_tags: 可选，更新标签（不填则保留原标签）
    """
    embedding_q = get_embedding(query, text_type="query")
    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding_q,
        limit=1,
        with_payload=True,
    )

    if not results.points:
        return "没有找到匹配的记忆。"

    top = results.points[0]
    if top.score < 0.5:
        return f"最相似的记忆相关度太低 ({top.score:.3f})，未更新。请用更精确的描述重试。"

    old_payload = top.payload
    old_preview = old_payload.get("content", "")[:120]

    category = new_category if new_category else old_payload.get("category", "general")
    tags = new_tags if new_tags else old_payload.get("tags", "")
    importance = get_importance(category)

    new_embedding = get_embedding(new_content, text_type="document")

    updated_payload = {
        "content": new_content,
        "category": category,
        "tags": tags,
        "importance": importance,
        "source": old_payload.get("source", "claude_code"),
        "created_at": old_payload.get("created_at", datetime.now().isoformat()),
        "timestamp": old_payload.get("timestamp", int(time.time())),
        "updated_at": datetime.now().isoformat(),
        "version": "v3",
    }

    client.upsert(
        collection_name=COLLECTION_NAME,
        points=[PointStruct(
            id=top.id,
            vector=new_embedding,
            payload=updated_payload,
        )],
    )

    return (
        f"记忆已更新 [ID: {str(top.id)[:8]}]\n"
        f"  旧内容: {old_preview}...\n"
        f"  新内容: {new_content[:120]}...\n"
        f"  分类: {category} | 重要性: {importance}"
    )


@mcp.tool()
def list_memories(category: str = "", limit: int = 10) -> str:
    """列出记忆。

    Args:
        category: 可选，按分类筛选
        limit: 返回数量，默认10条
    """
    scroll_filter = None
    if category:
        scroll_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=scroll_filter,
        limit=limit,
        with_payload=True,
    )

    if not results:
        return "暂无记忆。"

    memories = []
    for point in results:
        payload = point.payload
        importance = payload.get("importance", "medium")
        imp_tag = {"high": "★", "medium": "☆", "low": "·"}.get(importance, "·")
        memories.append(
            f"[{imp_tag}] [{payload.get('category', '')}] {payload.get('content', '')}\n"
            f"  标签: {payload.get('tags', '')} | 时间: {payload.get('created_at', '')[:10]}"
        )
    return "\n\n".join(memories)


@mcp.tool()
def search_openclaw_memory(keyword: str) -> str:
    """在 unified_memories_v3 中搜索 OpenClaw 的记忆（source=openclaw）。

    Args:
        keyword: 要搜索的关键词（支持多语言）
    """
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(
            must=[
                FieldCondition(key="source", match=MatchValue(value="openclaw")),
                FieldCondition(key="content", match=MatchText(text=keyword)),
            ]
        ),
        limit=5,
        with_payload=True,
    )

    if not results:
        return f"在 unified_memories_v3 (source=openclaw) 中没有找到包含 '{keyword}' 的记忆。"

    memories = []
    for point in results:
        payload = point.payload
        content = payload.get("content", "")
        category = payload.get("category", "")
        importance = payload.get("importance", "medium")
        created_at = payload.get("created_at", "")[:10]
        memories.append(
            f"[{category}] {content}\n"
            f"  重要性: {importance} | 时间: {created_at}"
        )
    return "\n\n".join(memories)


def call_graphiti_tool(tool_name: str, arguments: dict, timeout: int = 20) -> dict | None:
    """通过 SSE 协议调用单个 Graphiti MCP 工具（含 initialize 握手）。"""
    results = call_graphiti_tools_batch([(tool_name, arguments)], timeout=timeout)
    return results.get(tool_name)


def _parse_sse_body(text: str) -> list[dict]:
    """从 SSE 格式的响应体中提取 JSON-RPC 消息。"""
    messages = []
    for line in text.splitlines():
        if line.startswith("data:"):
            data = line[5:].strip()
            if data:
                try:
                    messages.append(json.loads(data))
                except Exception:
                    pass
    return messages


def _post_mcp(
    client: httpx.Client,
    payload: dict,
    session_id: str | None = None,
) -> tuple[dict | None, str | None]:
    """向 Graphiti MCP 发送 JSON-RPC 请求（Streamable HTTP 协议）。

    Returns:
        (parsed_response, session_id)
    """
    headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
    if session_id:
        headers["Mcp-Session-Id"] = session_id

    resp = client.post(GRAPHITI_MCP_URL, json=payload, headers=headers)
    resp.raise_for_status()

    new_session_id = resp.headers.get("mcp-session-id", session_id)
    content_type = resp.headers.get("content-type", "")

    if "text/event-stream" in content_type:
        msgs = _parse_sse_body(resp.text)
        return (msgs[0] if msgs else None), new_session_id
    else:
        try:
            return resp.json(), new_session_id
        except Exception:
            return None, new_session_id


def call_graphiti_tools_batch(
    calls: list[tuple[str, dict]], timeout: int = 20
) -> dict[str, dict | None]:
    """通过 Streamable HTTP 批量调用 Graphiti MCP 工具。

    Args:
        calls: [(tool_name, arguments), ...] 要调用的工具列表
        timeout: 单次请求超时（秒）

    Returns:
        {tool_name: response_dict | None}
    """
    results: dict[str, dict | None] = {name: None for name, _ in calls}

    try:
        with httpx.Client(timeout=timeout, trust_env=False) as client:
            # ── 握手 ──
            init_resp, session_id = _post_mcp(client, {
                "jsonrpc": "2.0", "id": 0,
                "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {}},
                    "clientInfo": {"name": "hybrid_search", "version": "2.0"},
                },
            })
            if not session_id:
                return results

            _post_mcp(client, {
                "jsonrpc": "2.0",
                "method": "notifications/initialized",
            }, session_id)

            # ── 逐个调用工具 ──
            for tool_name, arguments in calls:
                try:
                    resp, session_id = _post_mcp(client, {
                        "jsonrpc": "2.0", "id": tool_name,
                        "method": "tools/call",
                        "params": {"name": tool_name, "arguments": arguments},
                    }, session_id)
                    if resp:
                        results[tool_name] = resp
                except Exception:
                    pass

    except Exception:
        pass

    return results


def parse_graphiti_text(result: dict | None) -> str:
    """从 Graphiti MCP 返回结果中提取并格式化内容。"""
    if not result or "result" not in result:
        return ""
    content = result["result"].get("content", [])
    raw = ""
    for item in content:
        if isinstance(item, dict) and item.get("type") == "text":
            raw = item.get("text", "")
            break
    if not raw:
        return ""
    # 尝试解析 JSON 并友好格式化
    try:
        data = json.loads(raw)
        # 节点列表
        if "nodes" in data:
            nodes = data["nodes"]
            if not nodes:
                return ""
            lines = []
            for n in nodes[:5]:  # 最多5个
                summary = (n.get("summary") or "")[:120].replace("\n", " ")
                lines.append(f"• **{n['name']}** ({', '.join(n.get('labels', []))})\n  {summary}")
            return "\n".join(lines)
        # 事实列表
        if "facts" in data:
            facts = data["facts"]
            if not facts:
                return ""
            lines = []
            for f in facts[:5]:  # 最多5条
                lines.append(f"• [{f.get('name','')}] {f.get('fact','')[:120]}")
            return "\n".join(lines)
    except Exception:
        pass
    return raw[:500]


@mcp.tool()
def hybrid_search(query: str, top_k: int = 5) -> str:
    """融合搜索：Qdrant 向量语义 + Graphiti 知识图谱，并行查询统一返回。

    Args:
        query: 搜索内容，用自然语言描述
        top_k: Qdrant 返回结果数量，默认5条
    """
    # ── 并行搜索两个系统 ──────────────────────────────
    qdrant_scored: list = []
    graphiti_nodes_text = ""
    graphiti_facts_text = ""

    def search_qdrant():
        embedding = get_embedding(query, text_type="query")
        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=embedding,
            limit=top_k * 3,
            with_payload=True,
        )
        for point in results.points:
            payload = point.payload
            importance = payload.get("importance", "medium")
            ts = payload.get("timestamp", 0)
            w_score = weighted_score(point.score, importance, ts)
            qdrant_scored.append({
                "raw_score": point.score,
                "weighted_score": w_score,
                "importance": importance,
                "content": payload.get("content", ""),
                "category": payload.get("category", ""),
                "tags": payload.get("tags", ""),
                "created_at": payload.get("created_at", "")[:10],
            })

    def search_graphiti_batch():
        nonlocal graphiti_nodes_text, graphiti_facts_text
        results = call_graphiti_tools_batch([
            ("search_nodes", {"query": query, "group_ids": ["claude_code", "openclaw"]}),
            ("search_memory_facts", {"query": query, "group_ids": ["claude_code", "openclaw"]}),
        ], timeout=15)
        graphiti_nodes_text = parse_graphiti_text(results.get("search_nodes"))
        graphiti_facts_text = parse_graphiti_text(results.get("search_memory_facts"))

    threads = [
        threading.Thread(target=search_qdrant),
        threading.Thread(target=search_graphiti_batch),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=18)

    # ── Qdrant 结果排序 ───────────────────────────────
    deduped = deduplicate(qdrant_scored)
    deduped.sort(key=lambda x: x["weighted_score"], reverse=True)
    deduped = deduped[:top_k]

    sections = []

    if deduped:
        lines = ["## 📦 Qdrant 向量记忆"]
        for item in deduped:
            imp_tag = {"high": "★", "medium": "☆", "low": "·"}.get(item["importance"], "·")
            lines.append(
                f"[{imp_tag} {item['weighted_score']:.2f}] [{item['category']}] "
                f"{item['content']}\n"
                f"  标签: {item['tags']} | 时间: {item['created_at']}"
            )
        sections.append("\n\n".join(lines))
    else:
        sections.append("## 📦 Qdrant 向量记忆\n（无结果）")

    # ── Graphiti 结果 ─────────────────────────────────
    if graphiti_nodes_text.strip():
        sections.append(f"## 🕸️ Graphiti 实体节点\n{graphiti_nodes_text.strip()}")
    else:
        sections.append("## 🕸️ Graphiti 实体节点\n（无结果或超时）")

    if graphiti_facts_text.strip():
        sections.append(f"## 🔗 Graphiti 关系事实\n{graphiti_facts_text.strip()}")
    else:
        sections.append("## 🔗 Graphiti 关系事实\n（无结果或超时）")

    return "\n\n---\n\n".join(sections)


@mcp.tool()
def compact_conversations(before_days: int = 7, dry_run: bool = True) -> str:
    """压缩旧 conversation 记忆：按天合并为摘要，删除原始记录。

    Args:
        before_days: 压缩多少天前的 conversation（默认7天前，保留近7天不动）
        dry_run: 试运行（默认True），只展示会压缩什么，不真正执行
    """
    cutoff_ts = int(time.time()) - before_days * 86400

    # 收集所有需要压缩的 conversation
    all_convs: list[dict] = []
    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            scroll_filter=Filter(must=[
                FieldCondition(key="category", match=MatchValue(value="conversation")),
                FieldCondition(key="timestamp", range=Range(lt=cutoff_ts)),
            ]),
            limit=100,
            offset=offset,
            with_payload=True,
        )
        if not points:
            break
        for p in points:
            ts = p.payload.get("timestamp", 0)
            date_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d") if ts else "unknown"
            all_convs.append({
                "id": p.id,
                "date": date_str,
                "content": p.payload.get("content", ""),
                "tags": p.payload.get("tags", ""),
            })
        if offset is None:
            break

    if not all_convs:
        return f"没有 {before_days} 天前的 conversation 记忆需要压缩。"

    # 按日期分组
    by_date: dict[str, list[dict]] = {}
    for conv in all_convs:
        by_date.setdefault(conv["date"], []).append(conv)

    if dry_run:
        lines = [f"[试运行] 共 {len(all_convs)} 条待压缩，分布在 {len(by_date)} 天："]
        for date, convs in sorted(by_date.items()):
            total_chars = sum(len(c["content"]) for c in convs)
            lines.append(f"  {date}: {len(convs)} 条 ({total_chars} 字符)")
        lines.append(f"\n设置 dry_run=False 执行压缩。")
        return "\n".join(lines)

    # 执行压缩：每天合并为一条 summary
    compressed = 0
    deleted = 0
    for date, convs in sorted(by_date.items()):
        # 提取每条的核心内容（取前200字符避免超长）
        snippets = []
        all_tags = set()
        for conv in convs:
            content = conv["content"]
            # 提取 [问] 和 [答] 的摘要行
            lines_raw = content.split("\n")
            summary_lines = []
            for line in lines_raw:
                stripped = line.strip()
                if stripped.startswith("[问]") or stripped.startswith("[答]") or stripped.startswith("用户:") or stripped.startswith("Claude:"):
                    summary_lines.append(stripped[:150])
            snippet = " | ".join(summary_lines) if summary_lines else content[:200]
            snippets.append(snippet)
            for tag in conv["tags"].split(","):
                tag = tag.strip()
                if tag and tag != date:
                    all_tags.add(tag)

        # 合并内容（限制总长度 4000 字符）
        merged = f"[{date} 对话摘要] 共 {len(convs)} 轮对话\n\n"
        remaining = 4000 - len(merged)
        for i, snippet in enumerate(snippets):
            entry = f"({i+1}) {snippet}\n"
            if len(entry) > remaining:
                merged += f"... 及其他 {len(snippets) - i} 条\n"
                break
            merged += entry
            remaining -= len(entry)

        tags = f"{date},daily-summary,compressed," + ",".join(sorted(all_tags)[:5])

        # 存储压缩记忆
        embedding = get_embedding(merged, text_type="document")
        memory_id = make_id(merged)
        client.upsert(
            collection_name=COLLECTION_NAME,
            points=[PointStruct(
                id=memory_id,
                vector=embedding,
                payload={
                    "content": merged,
                    "category": "summary",
                    "tags": tags,
                    "importance": "high",
                    "source": "claude_code",
                    "created_at": f"{date}T00:00:00",
                    "timestamp": int(datetime(
                        *[int(x) for x in date.split("-")]
                    ).timestamp()),
                    "version": "v3",
                },
            )],
        )
        compressed += 1

        # 删除原始记录
        ids_to_delete = [conv["id"] for conv in convs]
        client.delete(
            collection_name=COLLECTION_NAME,
            points_selector=ids_to_delete,
        )
        deleted += len(convs)

    return (
        f"压缩完成：\n"
        f"  原始记录: {deleted} 条已删除\n"
        f"  生成摘要: {compressed} 条（每天一条，category=summary, importance=high）\n"
        f"  净减少: {deleted - compressed} 条"
    )


# ── memory_stats 缓存 ────────────────────────────────
_stats_cache: dict = {"result": None, "expires": 0}
_STATS_TTL = 60  # 缓存60秒
_KNOWN_CATEGORIES = [
    "conversation", "general", "project", "preference", "solution",
    "architecture", "debug", "feedback", "decision", "summary", "fact",
]
_KNOWN_IMPORTANCES = ["high", "medium", "low"]


def _count_filtered(key: str, value: str) -> int:
    """用 Qdrant count API 按单个字段值计数（O(1)，无需遍历）。"""
    result = client.count(
        collection_name=COLLECTION_NAME,
        count_filter=Filter(must=[FieldCondition(key=key, match=MatchValue(value=value))]),
        exact=True,
    )
    return result.count


@mcp.tool()
def memory_stats(force_refresh: bool = False) -> str:
    """查看记忆统计信息：总数、各分类数量、各重要性等级数量。

    Args:
        force_refresh: 强制刷新缓存（默认 False，使用60秒 TTL 缓存）
    """
    now = time.time()
    if not force_refresh and _stats_cache["result"] and now < _stats_cache["expires"]:
        return _stats_cache["result"] + "\n\n（缓存结果，60秒内有效）"

    info = client.get_collection(collection_name=COLLECTION_NAME)
    total = info.points_count

    # 并行计数：分类 + 重要性（每个 count API 调用都是 O(1)）
    categories: dict[str, int] = {}
    importances: dict[str, int] = {}
    results_lock = threading.Lock()

    def count_category(cat: str) -> None:
        c = _count_filtered("category", cat)
        if c > 0:
            with results_lock:
                categories[cat] = c

    def count_importance(imp: str) -> None:
        c = _count_filtered("importance", imp)
        with results_lock:
            importances[imp] = c

    threads = []
    for cat in _KNOWN_CATEGORIES:
        t = threading.Thread(target=count_category, args=(cat,))
        threads.append(t)
        t.start()
    for imp in _KNOWN_IMPORTANCES:
        t = threading.Thread(target=count_importance, args=(imp,))
        threads.append(t)
        t.start()
    for t in threads:
        t.join(timeout=10)

    categorized_total = sum(categories.values())
    uncategorized = total - categorized_total
    importance_total = sum(importances.values())
    v1_count = total - importance_total

    lines = [f"总记忆数: {total}"]
    lines.append("\n按分类:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    if uncategorized > 0:
        lines.append(f"  其他: {uncategorized}")
    lines.append("\n按重要性:")
    for imp in _KNOWN_IMPORTANCES:
        lines.append(f"  {imp}: {importances.get(imp, 0)}")
    if v1_count > 0:
        lines.append(f"  未分级(v1): {v1_count}")

    result = "\n".join(lines)
    _stats_cache["result"] = result
    _stats_cache["expires"] = now + _STATS_TTL

    return result


@mcp.tool()
def global_search(query: str, top_k: int = 5) -> str:
    """全局搜索：同时搜索 claude_code 和 openclaw 两端记忆，各返回最相关 top_k 条，合并排序。

    Args:
        query: 搜索内容，用自然语言描述
        top_k: 每端返回数量，默认5条（总计最多 top_k*2 条）
    """
    embedding = get_embedding(query, text_type="query")
    fetch_k = top_k * 3

    claude_scored: list = []
    openclaw_scored: list = []

    def search_source(source: str, out: list):
        results = client.query_points(
            collection_name=COLLECTION_NAME,
            query=embedding,
            query_filter=Filter(must=[FieldCondition(key="source", match=MatchValue(value=source))]),
            limit=fetch_k,
            with_payload=True,
        )
        for point in results.points:
            payload = point.payload
            importance = payload.get("importance", "medium")
            ts = payload.get("timestamp", 0)
            w_score = weighted_score(point.score, importance, ts)
            out.append({
                "source": source,
                "raw_score": point.score,
                "weighted_score": w_score,
                "importance": importance,
                "content": payload.get("content", ""),
                "category": payload.get("category", ""),
                "tags": payload.get("tags", ""),
                "created_at": payload.get("created_at", "")[:10],
            })

    t1 = threading.Thread(target=search_source, args=("claude_code", claude_scored))
    t2 = threading.Thread(target=search_source, args=("openclaw", openclaw_scored))
    t1.start(); t2.start()
    t1.join(timeout=15); t2.join(timeout=15)

    def format_section(label: str, items: list) -> str:
        deduped = deduplicate(items)
        deduped.sort(key=lambda x: x["weighted_score"], reverse=True)
        deduped = deduped[:top_k]
        if not deduped:
            return f"## {label}\n（无结果）"
        lines = [f"## {label}"]
        for item in deduped:
            imp_tag = {"high": "★", "medium": "☆", "low": "·"}.get(item["importance"], "·")
            lines.append(
                f"[{imp_tag} {item['weighted_score']:.2f}] [{item['category']}] "
                f"{item['content']}\n"
                f"  标签: {item['tags']} | 时间: {item['created_at']}"
            )
        return "\n\n".join(lines)

    claude_section = format_section("Claude Code 记忆", claude_scored)
    openclaw_section = format_section("OpenClaw 记忆", openclaw_scored)
    return f"{claude_section}\n\n---\n\n{openclaw_section}"


# ── 多模态搜索 ─────────────────────────────────────────

MULTIMODAL_COLLECTION = "multimodal_memories"
MULTIMODAL_MODEL = "tongyi-embedding-vision-plus-2026-03-06"
MULTIMODAL_API_URL = "https://dashscope.aliyuncs.com/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding"
MULTIMODAL_DIM = 1024


def get_multimodal_embedding(text: str) -> list[float]:
    """通过多模态模型编码纯文本 query（确保与融合向量在同一空间）。"""
    resp = _http_client.post(
        MULTIMODAL_API_URL,
        headers={
            "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": MULTIMODAL_MODEL,
            "input": {"contents": [{"text": text}]},
            "parameters": {"dimension": MULTIMODAL_DIM},
        },
    )
    resp.raise_for_status()
    data = resp.json()
    return data["output"]["embeddings"][0]["embedding"]


def ensure_multimodal_collection() -> bool:
    """确保多模态 collection 存在并建立索引，返回是否可用。"""
    try:
        collections = [c.name for c in client.get_collections().collections]
        if MULTIMODAL_COLLECTION not in collections:
            client.create_collection(
                collection_name=MULTIMODAL_COLLECTION,
                vectors_config=VectorParams(size=MULTIMODAL_DIM, distance=Distance.COSINE),
            )
        for field, schema in [
            ("has_image", "keyword"),
            ("image_key", "keyword"),
            ("source", "keyword"),
            ("sender", "keyword"),
            ("tags", TextIndexParams(
                type="text",
                tokenizer=TokenizerType.MULTILINGUAL,
                min_token_len=2,
                max_token_len=20,
            )),
        ]:
            try:
                client.create_payload_index(
                    MULTIMODAL_COLLECTION, field_name=field, field_schema=schema,
                )
            except Exception:
                pass
        return True
    except Exception:
        return False


@mcp.tool()
def search_multimodal_memory(query: str, top_k: int = 5) -> str:
    """搜索多模态记忆（文搜图）。用文字描述查找包含图片的记忆。

    适用于：根据文字描述搜索包包图片、查找之前发过的图文消息等。
    向量空间与纯文本记忆不同，此工具专搜多模态融合向量。

    Args:
        query: 搜索内容，用自然语言描述你想找的图文内容
        top_k: 返回结果数量，默认5条
    """
    if not ensure_multimodal_collection():
        return "多模态 collection 不可用。"

    try:
        embedding = get_multimodal_embedding(query)
    except Exception as e:
        return f"多模态 embedding 生成失败: {e}"

    results = client.query_points(
        collection_name=MULTIMODAL_COLLECTION,
        query=embedding,
        limit=top_k,
        with_payload=True,
    )

    if not results.points:
        return "没有找到相关的多模态记忆。"

    memories = []
    for point in results.points:
        payload = point.payload
        score = point.score
        content = payload.get("content", payload.get("text", ""))
        has_image = payload.get("has_image", "false")
        image_key = payload.get("image_key", "")
        tags = payload.get("tags", "")
        created_at = payload.get("created_at", "")[:10]

        img_tag = "🖼️" if has_image == "true" else "📝"
        memories.append(
            f"[{img_tag} {score:.2f}] {content}\n"
            f"  image_key: {image_key} | 标签: {tags} | 时间: {created_at}"
        )

    return "\n\n".join(memories)


if __name__ == "__main__":
    mcp.run(transport="stdio")
