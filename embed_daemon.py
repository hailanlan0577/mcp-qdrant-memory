"""本地 Embedding Daemon — Qwen3-VL-Embedding-8B-mlx-bf16

替代阿里云 text-embedding-v4 (1024 维) → 本地 MLX 4096 维。
常驻进程，模型一次加载,后续请求直接推理。

启动方式:
    python3 embed_daemon.py
    # 或后台:
    nohup python3 embed_daemon.py > /tmp/embed_daemon.log 2>&1 &

调用方式:
    curl -X POST http://127.0.0.1:8765/embed \
         -H "Content-Type: application/json" \
         -d '{"text": "你好世界", "text_type": "query"}'
"""
import asyncio
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager

# Monkey-patch transformers 5.7.0 缺失的 Qwen3-VL Processor 属性
from transformers.models.qwen3_vl.processing_qwen3_vl import Qwen3VLProcessor

Qwen3VLProcessor.image_ids = [151655]
Qwen3VLProcessor.video_ids = [151656]
Qwen3VLProcessor.audio_ids = []

import mlx.core as mx  # noqa: E402
import numpy as np  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from mlx_embeddings.utils import load  # noqa: E402
from pydantic import BaseModel, Field  # noqa: E402

MODEL_PATH = "/Users/chenyuanhai/models/Qwen3-VL-Embedding-8B-mlx-bf16"
QUERY_INSTRUCTION = "Retrieve images or text relevant to the user's query."
HOST = "127.0.0.1"
PORT = 8765

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("embed_daemon")

# MLX 的 GPU stream 是线程本地的,加载和推理必须在同一个线程
# 用单 worker 线程池强制串行化,避免跨线程 stream 错误
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx-infer")
_state: dict = {"model": None, "processor": None, "loaded_at": None}


def _load_model_sync() -> None:
    log.info(f"加载模型: {MODEL_PATH}")
    t0 = time.time()
    model, processor = load(MODEL_PATH)
    elapsed = time.time() - t0
    _state["model"] = model
    _state["processor"] = processor
    _state["loaded_at"] = time.time()
    log.info(f"模型加载完成,耗时 {elapsed:.1f}s")


@asynccontextmanager
async def lifespan(app: FastAPI):
    loop = asyncio.get_event_loop()
    await loop.run_in_executor(_executor, _load_model_sync)
    yield
    log.info("daemon 关闭")
    _executor.shutdown(wait=True)


app = FastAPI(lifespan=lifespan, title="Qwen3-VL Embedding Daemon")


class EmbedRequest(BaseModel):
    text: str = Field(..., description="待编码的文本", min_length=1)
    text_type: str = Field(
        default="document",
        description="'query' 加 instruction 前缀, 'document' 不加",
    )


class EmbedResponse(BaseModel):
    embedding: list[float]
    dim: int
    latency_ms: float


# OpenAI 兼容: /v1/embeddings
class OpenAIEmbedRequest(BaseModel):
    model: str = Field(default="qwen3-vl-embedding-8b")
    input: str | list[str]
    encoding_format: str = Field(default="float")


class OpenAIEmbedDataItem(BaseModel):
    object: str = "embedding"
    embedding: list[float]
    index: int


class OpenAIEmbedUsage(BaseModel):
    prompt_tokens: int = 0
    total_tokens: int = 0


class OpenAIEmbedResponse(BaseModel):
    object: str = "list"
    data: list[OpenAIEmbedDataItem]
    model: str
    usage: OpenAIEmbedUsage


def _infer_sync(text: str, text_type: str) -> list[float]:
    if text_type == "query":
        inp = [{"text": text, "instruction": QUERY_INSTRUCTION}]
    else:
        inp = [{"text": text}]
    emb = _state["model"].process(inp, processor=_state["processor"])
    mx.eval(emb)
    return np.array(emb).astype(np.float32)[0].tolist()


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    if _state["model"] is None:
        raise HTTPException(503, "模型尚未加载完成")
    if req.text_type not in ("query", "document"):
        raise HTTPException(400, f"text_type 必须是 query 或 document, 收到: {req.text_type}")

    t0 = time.time()
    loop = asyncio.get_event_loop()
    try:
        vec = await loop.run_in_executor(_executor, _infer_sync, req.text, req.text_type)
    except Exception as e:
        log.exception("推理失败")
        raise HTTPException(500, f"推理失败: {e}") from e

    latency_ms = (time.time() - t0) * 1000
    return EmbedResponse(embedding=vec, dim=len(vec), latency_ms=latency_ms)


@app.post("/v1/embeddings", response_model=OpenAIEmbedResponse)
async def openai_embeddings(req: OpenAIEmbedRequest) -> OpenAIEmbedResponse:
    """OpenAI 兼容端点(供 Graphiti 等用 OpenAI SDK 的客户端使用)。"""
    if _state["model"] is None:
        raise HTTPException(503, "模型尚未加载完成")
    texts: list[str] = req.input if isinstance(req.input, list) else [req.input]
    if not texts or not all(t for t in texts):
        raise HTTPException(400, "input 不能为空")

    loop = asyncio.get_event_loop()
    items: list[OpenAIEmbedDataItem] = []
    for idx, text in enumerate(texts):
        try:
            vec = await loop.run_in_executor(_executor, _infer_sync, text, "document")
        except Exception as e:
            log.exception(f"OpenAI 兼容端点推理失败 [idx={idx}]")
            raise HTTPException(500, f"推理失败 idx={idx}: {e}") from e
        items.append(OpenAIEmbedDataItem(embedding=vec, index=idx))

    total_chars = sum(len(t) for t in texts)
    return OpenAIEmbedResponse(
        data=items,
        model=req.model,
        usage=OpenAIEmbedUsage(prompt_tokens=total_chars, total_tokens=total_chars),
    )


@app.get("/health")
def health() -> dict:
    return {
        "status": "ok" if _state["model"] is not None else "loading",
        "model": "Qwen3-VL-Embedding-8B-mlx-bf16",
        "dim": 4096,
        "loaded_at": _state["loaded_at"],
        "uptime_sec": time.time() - _state["loaded_at"] if _state["loaded_at"] else None,
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=HOST, port=PORT, log_level="info")
