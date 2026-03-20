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

# 防止 httpx/qdrant 走系统代理连接本地 Qdrant
os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

import hashlib
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
COLLECTION_NAME = "claude-memory-v3"
VECTOR_DIM = 1024

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
    "debug": "medium",
    "general": "medium",
    "conversation": "low",
    "summary": "high",
}

# 去重阈值
DEDUP_THRESHOLD = 0.92

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


ensure_collection()

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
2. **search_memory**: 智能搜索，向量语义 + 重要性加权 + 去重
3. **keyword_search**: 精确关键词搜索，适合搜特定术语、项目名、工具名
4. **delete_memory**: 支持精确内容删除和语义模糊删除
5. **list_memories**: 按分类浏览所有记忆
6. **memory_stats**: 查看记忆统计信息

搜索策略：
- 模糊/语义搜索 → 用 search_memory
- 精确关键词 → 用 keyword_search
- 找不到时两个都试试
""",
)


def make_id(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


def get_importance(category: str) -> str:
    """根据 category 自动判定 importance。"""
    return CATEGORY_IMPORTANCE.get(category, "medium")


def weighted_score(score: float, importance: str) -> float:
    """对原始向量相似度分数加权。"""
    weight = IMPORTANCE_WEIGHTS.get(importance, 1.0)
    return score * weight


def deduplicate(results: list, threshold: float = DEDUP_THRESHOLD) -> list:
    """去重：相似度超过阈值的只保留加权分最高的。"""
    if len(results) <= 1:
        return results

    kept = []
    for item in results:
        is_dup = False
        for existing in kept:
            if abs(item["raw_score"] - existing["raw_score"]) < 0.01:
                content_a = item["content"][:100]
                content_b = existing["content"][:100]
                if content_a == content_b:
                    is_dup = True
                    break
        if not is_dup:
            kept.append(item)

    return kept


@mcp.tool()
def store_memory(content: str, category: str = "general", tags: str = "") -> str:
    """存储一条记忆到永久向量数据库（V3，text-embedding-v4）。

    Args:
        content: 要记住的内容，尽量描述清楚上下文
        category: 分类，如 project/preference/solution/architecture/debug/general/conversation/summary
        tags: 逗号分隔的标签，如 "python,react,中国象棋"
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
def search_memory(query: str, category: str = "", top_k: int = 5) -> str:
    """智能搜索记忆（向量语义 + 重要性加权 + 去重）。

    Args:
        query: 搜索内容，用自然语言描述你想找什么
        category: 可选，限定搜索分类
        top_k: 返回结果数量，默认5条
    """
    embedding = get_embedding(query, text_type="query")

    fetch_k = top_k * 3

    query_filter = None
    if category:
        query_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

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
        w_score = weighted_score(point.score, importance)
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

    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(should=should_conditions, must=must_conditions if must_conditions else None),
        limit=limit,
        with_payload=True,
    )

    if not results:
        return f"没有找到包含 '{keyword}' 的记忆。"

    memories = []
    for point in results:
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
    """在 openclaw_memories 集合中按关键词搜索记忆。

    Args:
        keyword: 要搜索的关键词（支持多语言）
    """
    results, _ = client.scroll(
        collection_name="openclaw_memories",
        scroll_filter=Filter(
            must=[
                FieldCondition(key="text", match=MatchText(text=keyword))
            ]
        ),
        limit=5,
        with_payload=True,
    )

    if not results:
        return f"在 openclaw_memories 中没有找到包含 '{keyword}' 的记忆。"

    memories = []
    for point in results:
        payload = point.payload
        text = payload.get("text", "")
        category = payload.get("category", "")
        importance = payload.get("importance_level", "")
        raw_ts = payload.get("createdAt")
        if raw_ts and isinstance(raw_ts, (int, float)):
            created_at = datetime.fromtimestamp(raw_ts / 1000).strftime("%Y-%m-%d %H:%M")
        else:
            created_at = str(raw_ts or "")[:10]
        memories.append(
            f"[{category}] {text}\n"
            f"  重要性: {importance} | 时间: {created_at}"
        )
    return "\n\n".join(memories)


@mcp.tool()
def memory_stats() -> str:
    """查看记忆统计信息：总数、各分类数量、各重要性等级数量。"""
    info = client.get_collection(collection_name=COLLECTION_NAME)
    total = info.points_count

    categories = {}
    importances = {"high": 0, "medium": 0, "low": 0}
    v1_count = 0

    offset = None
    while True:
        points, offset = client.scroll(
            collection_name=COLLECTION_NAME,
            limit=100,
            offset=offset,
            with_payload=True,
        )
        if not points:
            break
        for p in points:
            cat = p.payload.get("category", "unknown")
            categories[cat] = categories.get(cat, 0) + 1
            imp = p.payload.get("importance")
            if imp:
                importances[imp] = importances.get(imp, 0) + 1
            else:
                v1_count += 1
        if offset is None:
            break

    lines = [f"总记忆数: {total}"]
    lines.append("\n按分类:")
    for cat, count in sorted(categories.items(), key=lambda x: -x[1]):
        lines.append(f"  {cat}: {count}")
    lines.append("\n按重要性:")
    for imp in ["high", "medium", "low"]:
        lines.append(f"  {imp}: {importances[imp]}")
    if v1_count:
        lines.append(f"  未分级(v1): {v1_count}")

    return "\n".join(lines)


if __name__ == "__main__":
    mcp.run(transport="stdio")
