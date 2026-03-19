import hashlib
import time
from datetime import datetime

from fastembed import TextEmbedding
from mcp.server.fastmcp import FastMCP
from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    MatchValue,
    PointStruct,
    VectorParams,
)

# ── 配置 ──────────────────────────────────────────────
QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "claude-memory"
EMBEDDING_MODEL = "BAAI/bge-small-zh-v1.5"
VECTOR_DIM = 512

# ── 初始化 ─────────────────────────────────────────────
client = QdrantClient(url=QDRANT_URL, timeout=30, check_compatibility=False)
embedding_model = TextEmbedding(model_name=EMBEDDING_MODEL)


def ensure_collection():
    """确保 collection 存在，不存在则创建。"""
    collections = [c.name for c in client.get_collections().collections]
    if COLLECTION_NAME not in collections:
        client.create_collection(
            collection_name=COLLECTION_NAME,
            vectors_config=VectorParams(size=VECTOR_DIM, distance=Distance.COSINE),
        )


ensure_collection()

mcp = FastMCP(
    "Claude Memory",
    instructions="""
这是 Claude 的永久向量记忆系统（自建版）。使用指南：

1. **store_memory**: 存储重要信息到长期记忆。适合存：
   - 用户偏好和习惯
   - 项目架构决策
   - 关键技术方案
   - 调试经验和解决方案
   - 任何需要跨会话记住的信息

2. **search_memory**: 用自然语言搜索相关记忆。每次新对话开始时，
   应该主动搜索与当前任务相关的记忆。

3. **delete_memory**: 删除过时或错误的记忆。

4. **list_memories**: 按分类浏览所有记忆。
""",
)


def get_embedding(text: str) -> list[float]:
    """使用本地 bge-small-zh 模型生成向量。"""
    embeddings = list(embedding_model.embed([text]))
    return embeddings[0].tolist()


def make_id(content: str) -> str:
    return hashlib.md5(content.encode()).hexdigest()


@mcp.tool()
def store_memory(content: str, category: str = "general", tags: str = "") -> str:
    """存储一条记忆到永久向量数据库。

    Args:
        content: 要记住的内容，尽量描述清楚上下文
        category: 分类，如 project/preference/solution/architecture/debug/general/conversation
        tags: 逗号分隔的标签，如 "python,react,中国象棋"
    """
    embedding = get_embedding(content)
    memory_id = make_id(content)
    metadata = {
        "content": content,
        "category": category,
        "tags": tags,
        "created_at": datetime.now().isoformat(),
        "timestamp": int(time.time()),
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
    return f"记忆已存储 [ID: {memory_id[:8]}] 分类: {category}"


@mcp.tool()
def search_memory(query: str, category: str = "", top_k: int = 5) -> str:
    """用自然语言搜索相关记忆。

    Args:
        query: 搜索内容，用自然语言描述你想找什么
        category: 可选，限定搜索分类
        top_k: 返回结果数量，默认5条
    """
    embedding = get_embedding(query)

    query_filter = None
    if category:
        query_filter = Filter(
            must=[FieldCondition(key="category", match=MatchValue(value=category))]
        )

    results = client.query_points(
        collection_name=COLLECTION_NAME,
        query=embedding,
        query_filter=query_filter,
        limit=top_k,
        with_payload=True,
    )

    if not results.points:
        return "没有找到相关记忆。"

    memories = []
    for point in results.points:
        payload = point.payload
        score = f"{point.score:.2f}"
        memories.append(
            f"[相关度: {score}] [{payload.get('category', '')}] "
            f"{payload.get('content', '')}\n"
            f"  标签: {payload.get('tags', '')} | 时间: {payload.get('created_at', '')[:10]}"
        )
    return "\n\n".join(memories)


@mcp.tool()
def delete_memory(content: str) -> str:
    """删除一条记忆。

    Args:
        content: 要删除的记忆内容（需要和存储时一致）
    """
    memory_id = make_id(content)
    client.delete(
        collection_name=COLLECTION_NAME,
        points_selector=[memory_id],
    )
    return f"记忆已删除 [ID: {memory_id[:8]}]"


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
        memories.append(
            f"[{payload.get('category', '')}] {payload.get('content', '')}\n"
            f"  标签: {payload.get('tags', '')} | 时间: {payload.get('created_at', '')[:10]}"
        )
    return "\n\n".join(memories)


if __name__ == "__main__":
    mcp.run(transport="stdio")
