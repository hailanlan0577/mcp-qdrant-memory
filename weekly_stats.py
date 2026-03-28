#!/usr/bin/env /Users/chenyuanhai/mcp-qdrant-memory/venv/bin/python
"""
E5.2.1 + E5.2.2 + E5.2.3 - 每周记忆健康度报告
- 统计各分类/来源/重要性分布
- debug/feedback 写入率监控（低于阈值时输出 ALERT）
- 写入 project 类记忆存档
"""
import os
import sys
import subprocess
import time
from datetime import datetime, timedelta
from qdrant_client import QdrantClient
from qdrant_client.models import Filter, FieldCondition, MatchValue, Range

os.environ.setdefault("NO_PROXY", "localhost,127.0.0.1")
os.environ.setdefault("no_proxy", "localhost,127.0.0.1")

QDRANT_URL = "http://localhost:6333"
COLLECTION_NAME = "unified_memories_v3" if "--prod" in sys.argv else "unified_memories_v3_test"
RECORD_QA_PYTHON = "/Users/chenyuanhai/mcp-qdrant-memory/venv/bin/python"
RECORD_QA_SCRIPT = "/Users/chenyuanhai/mcp-qdrant-memory/record_qa.py"
DASHSCOPE_API_KEY = os.environ.get("DASHSCOPE_API_KEY", "sk-0da94cd0218b4224aaebc5cf4a24c39f")

# debug/feedback 每周最低写入阈值
DEBUG_WEEKLY_MIN = 1
FEEDBACK_WEEKLY_MIN = 1

client = QdrantClient(url=QDRANT_URL, timeout=30, check_compatibility=False)
today = datetime.now().strftime("%Y-%m-%d")
now_ts = int(time.time())
week_ago_ts = now_ts - 7 * 86400

# ── 全量统计 ───────────────────────────────────────────
info = client.get_collection(collection_name=COLLECTION_NAME)
total = info.points_count

categories = {}
importances = {"high": 0, "medium": 0, "low": 0, "unknown": 0}
sources = {}
offset = None

while True:
    points, offset = client.scroll(
        collection_name=COLLECTION_NAME,
        limit=200,
        offset=offset,
        with_payload=True,
    )
    if not points:
        break
    for p in points:
        cat = p.payload.get("category", "unknown")
        categories[cat] = categories.get(cat, 0) + 1
        imp = p.payload.get("importance", "unknown")
        importances[imp] = importances.get(imp, 0) + 1
        src = p.payload.get("source", "unknown")
        sources[src] = sources.get(src, 0) + 1
    if offset is None:
        break

# ── 近7天 debug/feedback 计数 ─────────────────────────
def count_recent(category: str) -> int:
    results, _ = client.scroll(
        collection_name=COLLECTION_NAME,
        scroll_filter=Filter(must=[
            FieldCondition(key="category", match=MatchValue(value=category)),
            FieldCondition(key="timestamp", range=Range(gte=week_ago_ts)),
        ]),
        limit=500,
        with_payload=False,
    )
    return len(results)

debug_7d = count_recent("debug")
feedback_7d = count_recent("feedback")

# ── 计算健康度指标 ─────────────────────────────────────
conv_count = categories.get("conversation", 0)
high_value = sum(categories.get(c, 0) for c in ["debug", "feedback", "solution", "architecture", "project", "preference"])
conv_ratio = conv_count / total * 100 if total > 0 else 0
high_ratio = high_value / total * 100 if total > 0 else 0

# ── 格式化报告 ────────────────────────────────────────
cat_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(categories.items(), key=lambda x: -x[1]))
imp_lines = "\n".join(f"  {k}: {importances.get(k, 0)}" for k in ["high", "medium", "low"])
src_lines = "\n".join(f"  {k}: {v}" for k, v in sorted(sources.items(), key=lambda x: -x[1]))

# debug/feedback 告警
alerts = []
if debug_7d < DEBUG_WEEKLY_MIN:
    alerts.append(f"⚠️  ALERT: 近7天 debug 记忆仅 {debug_7d} 条，低于阈值 {DEBUG_WEEKLY_MIN}！请检查 bug 修复是否漏存")
if feedback_7d < FEEDBACK_WEEKLY_MIN:
    alerts.append(f"⚠️  ALERT: 近7天 feedback 记忆仅 {feedback_7d} 条，低于阈值 {FEEDBACK_WEEKLY_MIN}！请检查用户纠正是否漏存")

alert_block = "\n".join(alerts) if alerts else "✓ debug/feedback 写入率正常"

content = f"""[周报 {today}] unified_memories_v3 记忆健康度报告

总记忆数: {total}

按来源:
{src_lines}

按分类:
{cat_lines}

按重要性:
{imp_lines}

健康度指标:
  高价值记忆占比: {high_ratio:.1f}% (debug/feedback/solution/architecture/project/preference)
  conversation 占比: {conv_ratio:.1f}%
  近7天 debug 记忆: {debug_7d} 条
  近7天 feedback 记忆: {feedback_7d} 条

{alert_block}
"""

print(content)

# 输出告警到 stderr 以触发 cron 邮件通知
if alerts:
    for a in alerts:
        print(a, file=sys.stderr)

# ── 存储到 Qdrant ─────────────────────────────────────
env = os.environ.copy()
env["DASHSCOPE_API_KEY"] = DASHSCOPE_API_KEY
env["NO_PROXY"] = "localhost,127.0.0.1"

result = subprocess.run(
    [RECORD_QA_PYTHON, RECORD_QA_SCRIPT,
     f"[自动] 每周记忆健康度报告 {today}",
     content,
     "project",
     f"{today},weekly-stats,memory-report,health-check"],
    env=env,
    capture_output=True,
    text=True,
    timeout=30,
)
if result.returncode == 0:
    print(f"✓ 健康度报告已写入 Qdrant (category=project)")
else:
    print(f"✗ 写入失败: {result.stderr.strip()}", file=sys.stderr)
    sys.exit(1)

sys.exit(1 if alerts else 0)
