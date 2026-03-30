#!/bin/bash
# ============================================================
# 记忆系统一键诊断 & 恢复脚本
# 用法: bash ~/mcp-qdrant-memory/healthcheck.sh [--fix]
# --fix: 自动修复可修复的问题
# ============================================================

FIX_MODE=false
[[ "${1:-}" == "--fix" ]] && FIX_MODE=true

GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

PASS=0
FAIL=0
WARN=0

check() {
    local name="$1" result="$2" fix_hint="${3:-}"
    if [[ "$result" == "ok" ]]; then
        echo -e "  ${GREEN}✅${NC} $name"
        PASS=$((PASS + 1))
    elif [[ "$result" == "warn" ]]; then
        echo -e "  ${YELLOW}⚠️${NC}  $name"
        [[ -n "$fix_hint" ]] && echo -e "     修复: $fix_hint"
        WARN=$((WARN + 1))
    else
        echo -e "  ${RED}❌${NC} $name"
        [[ -n "$fix_hint" ]] && echo -e "     修复: $fix_hint"
        FAIL=$((FAIL + 1))
    fi
}

echo ""
echo "=============================="
echo " 记忆系统链路诊断"
echo " $(date '+%Y-%m-%d %H:%M:%S')"
echo "=============================="

# ── 1. SSH 隧道 ──────────────────────────────
echo ""
echo "📡 1. SSH 隧道 & 网络"

# Graphiti 隧道 (localhost:18001 → macmini:8001)
# 注意: Graphiti 返回 text/event-stream, curl -f 会误判，用 -o /dev/null -w '%{http_code}' 判断
GRAPHITI_HTTP_CODE=$(curl -s --max-time 5 -o /dev/null -w '%{http_code}' \
    http://localhost:18001/mcp -X POST \
    -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" \
    -d '{"jsonrpc":"2.0","id":0,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"healthcheck","version":"1.0"}}}' \
    2>/dev/null || echo "000")
if [[ "$GRAPHITI_HTTP_CODE" == "200" ]]; then
    check "Graphiti 隧道 (localhost:18001)" "ok"
else
    check "Graphiti 隧道 (localhost:18001)" "fail" \
        "ssh -fNL 18001:localhost:8001 macmini  或检查 launchd: com.graphiti.tunnel"
    if $FIX_MODE; then
        echo "     🔧 尝试建立 SSH 隧道..."
        ssh -fNL 18001:localhost:8001 macmini 2>/dev/null && \
            echo -e "     ${GREEN}已修复${NC}" || echo -e "     ${RED}修复失败${NC}"
    fi
fi

# Qdrant 隧道 (localhost:6333 → macmini:6333)
if curl -sf --max-time 3 http://localhost:6333/collections > /dev/null 2>&1; then
    check "Qdrant 隧道 (localhost:6333)" "ok"
else
    check "Qdrant 隧道 (localhost:6333)" "fail" \
        "ssh -fNL 6333:localhost:6333 macmini  或检查 launchd 隧道服务"
    if $FIX_MODE; then
        echo "     🔧 尝试建立 SSH 隧道..."
        ssh -fNL 6333:localhost:6333 macmini 2>/dev/null && \
            echo -e "     ${GREEN}已修复${NC}" || echo -e "     ${RED}修复失败${NC}"
    fi
fi

# ── 2. Mac Mini 服务 ─────────────────────────
echo ""
echo "🖥️  2. Mac Mini 后端服务"

# Qdrant
QDRANT_COLLECTIONS=$(curl -sf --max-time 5 http://localhost:6333/collections 2>/dev/null || echo "FAIL")
if [[ "$QDRANT_COLLECTIONS" == *"unified_memories_v3"* ]]; then
    check "Qdrant (unified_memories_v3)" "ok"
else
    if [[ "$QDRANT_COLLECTIONS" == "FAIL" ]]; then
        check "Qdrant 服务" "fail" "ssh macmini 'systemctl restart qdrant' 或检查 port 6333"
    else
        check "Qdrant collection unified_memories_v3" "fail" "collection 不存在，需重新创建"
    fi
fi

# Graphiti MCP Server（复用第1步的隧道检测结果）
if [[ "$GRAPHITI_HTTP_CODE" == "200" ]]; then
    check "Graphiti MCP Server (HTTP transport)" "ok"
else
    check "Graphiti MCP Server" "fail" \
        "ssh macmini 'cd ~/graphiti/mcp_server && venv/bin/python src/graphiti_mcp_server.py --transport http --port 8001'"
fi

# ── 3. Claude Code 侧 MCP 配置 ──────────────
echo ""
echo "⚙️  3. Claude Code MCP 配置"

CLAUDE_JSON="$HOME/.claude.json"
if [[ -f "$CLAUDE_JSON" ]]; then
    # Graphiti 注册
    if grep -q '"graphiti"' "$CLAUDE_JSON" && grep -q 'localhost:18001/mcp' "$CLAUDE_JSON"; then
        check "Graphiti MCP 注册 (~/.claude.json)" "ok"
    else
        check "Graphiti MCP 注册" "fail" \
            "claude mcp add graphiti --transport http http://localhost:18001/mcp"
    fi
    # qdrant-memory-v3 注册
    if grep -q 'qdrant-memory' "$CLAUDE_JSON" && grep -q 'server_v3.py' "$CLAUDE_JSON"; then
        check "qdrant-memory-v3 MCP 注册" "ok"
    else
        check "qdrant-memory-v3 MCP 注册" "warn" "未在 ~/.claude.json 找到 qdrant-memory-v3 配置"
    fi
else
    check "~/.claude.json 文件" "fail" "文件不存在"
fi

# ── 4. server_v3.py 代码完整性 ──────────────
echo ""
echo "📝 4. server_v3.py 代码完整性"

SERVER_FILE="$HOME/mcp-qdrant-memory/server_v3.py"
if [[ -f "$SERVER_FILE" ]]; then
    # 检查是否使用 Streamable HTTP（不是 SSE）
    if grep -q 'GRAPHITI_MCP_URL' "$SERVER_FILE" && grep -q '_post_mcp' "$SERVER_FILE"; then
        check "hybrid_search: Streamable HTTP 协议" "ok"
    elif grep -q 'GET.*\/sse' "$SERVER_FILE"; then
        check "hybrid_search: 仍在用 SSE 协议" "fail" \
            "git checkout server_v3.py (恢复到最新提交) 或重新应用修复"
        if $FIX_MODE; then
            echo "     🔧 尝试从 git 恢复..."
            cd "$HOME/mcp-qdrant-memory" && git checkout server_v3.py && \
                echo -e "     ${GREEN}已恢复${NC}" || echo -e "     ${RED}恢复失败${NC}"
        fi
    else
        check "hybrid_search: 代码结构异常" "warn" "既没有 HTTP 也没有 SSE，请手动检查"
    fi

    # 检查 GRAPHITI_BASE URL
    if grep -q 'GRAPHITI_BASE = "http://localhost:18001"' "$SERVER_FILE"; then
        check "GRAPHITI_BASE URL (localhost:18001)" "ok"
    else
        check "GRAPHITI_BASE URL" "warn" "URL 不是 localhost:18001，请确认"
    fi
else
    check "server_v3.py 文件" "fail" "文件不存在: $SERVER_FILE"
fi

# ── 5. 端到端搜索测试 ────────────────────────
echo ""
echo "🔍 5. 端到端搜索测试"

# 测试 Qdrant embedding + search
EMBEDDING_TEST=$(curl -sf --max-time 10 \
    -X POST http://localhost:6333/collections/unified_memories_v3/points/scroll \
    -H "Content-Type: application/json" \
    -d '{"limit":1,"with_payload":true}' 2>/dev/null || echo "FAIL")
if [[ "$EMBEDDING_TEST" == *"content"* ]]; then
    check "Qdrant 数据可读" "ok"
elif [[ "$EMBEDDING_TEST" == "FAIL" ]]; then
    check "Qdrant 数据可读" "fail" "无法连接或查询 Qdrant"
else
    check "Qdrant 数据可读" "warn" "连接正常但无数据"
fi

# 测试 Graphiti search_nodes（通过 Python httpx 调用，与 hybrid_search 一致）
if [[ "$GRAPHITI_HTTP_CODE" == "200" ]]; then
    GRAPHITI_SEARCH=$(python3 -c "
import httpx, json
try:
    c = httpx.Client(timeout=15, trust_env=False)
    r = c.post('http://localhost:18001/mcp', json={'jsonrpc':'2.0','id':0,'method':'initialize','params':{'protocolVersion':'2024-11-05','capabilities':{},'clientInfo':{'name':'hc','version':'1.0'}}}, headers={'Accept':'application/json, text/event-stream'})
    sid = r.headers.get('mcp-session-id','')
    r2 = c.post('http://localhost:18001/mcp', json={'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'search_nodes','arguments':{'query':'test','max_nodes':1}}}, headers={'Mcp-Session-Id':sid,'Accept':'application/json, text/event-stream'})
    print('ok' if 'result' in r2.text else 'warn')
except Exception as e:
    print('fail')
" 2>/dev/null || echo "fail")
    check "Graphiti search_nodes (Python httpx)" "$GRAPHITI_SEARCH" "Graphiti 服务异常"
else
    check "Graphiti search_nodes" "fail" "隧道不通，跳过"
fi

# ── 6. 记忆统计健康度 ───────────────────────
echo ""
echo "📊 6. 记忆统计健康度"

STATS_JSON=$(python3 -c "
import httpx, json
try:
    c = httpx.Client(timeout=10, trust_env=False)
    r = c.post('http://localhost:6333/collections/unified_memories_v3/points/count',
        json={'exact': True}, headers={'Content-Type': 'application/json'})
    total = r.json()['result']['count']
    r2 = c.post('http://localhost:6333/collections/unified_memories_v3/points/count',
        json={'filter': {'must': [{'key': 'category', 'match': {'value': 'conversation'}}]}, 'exact': True},
        headers={'Content-Type': 'application/json'})
    conv = r2.json()['result']['count']
    pct = round(conv / total * 100) if total > 0 else 0
    print(f'{total},{conv},{pct}')
except: print('0,0,0')
" 2>/dev/null)
IFS=',' read -r TOTAL CONV PCT <<< "$STATS_JSON"
if [[ $TOTAL -gt 0 ]]; then
    check "记忆总量: ${TOTAL} 条" "ok"
    if [[ $PCT -gt 60 ]]; then
        check "conversation 占比 ${PCT}%" "warn" "超过 60%，建议运行 compact_conversations"
    else
        check "conversation 占比 ${PCT}%" "ok"
    fi
else
    check "记忆统计" "fail" "无法读取记忆数据"
fi

# ── 7. Embedding API 可用性 ─────────────────
echo ""
echo "🔑 7. Embedding API"

EMBED_OK=$(python3 -c "
import httpx, os
try:
    key = os.environ.get('DASHSCOPE_API_KEY', '')
    if not key: print('nokey'); exit()
    r = httpx.post('https://dashscope.aliyuncs.com/compatible-mode/v1/embeddings',
        headers={'Authorization': f'Bearer {key}', 'Content-Type': 'application/json'},
        json={'model': 'text-embedding-v4', 'input': 'test', 'dimensions': 1024},
        timeout=10)
    print('ok' if r.status_code == 200 else 'fail')
except: print('fail')
" 2>/dev/null)
check "DashScope Embedding API" "$EMBED_OK" "检查 DASHSCOPE_API_KEY 或网络连接"

# ── 8. 备份新鲜度 ──────────────────────────
echo ""
echo "💾 8. 备份新鲜度"

LATEST_BACKUP=$(ls -t "$HOME/backups"/macbookpro-backup-*.tar.gz 2>/dev/null | head -1)
if [[ -n "$LATEST_BACKUP" ]]; then
    BACKUP_AGE=$(( ($(date +%s) - $(stat -f %m "$LATEST_BACKUP")) / 3600 ))
    if [[ $BACKUP_AGE -le 48 ]]; then
        check "最近备份: ${BACKUP_AGE}h 前" "ok"
    else
        check "最近备份: ${BACKUP_AGE}h 前" "warn" "超过 48 小时未备份，运行 bash ~/backups/backup-macbookpro.sh"
    fi
else
    check "MacBook Pro 备份" "fail" "未找到任何备份"
fi

# ── 9. 技术文档 ──────────────────────────────
echo ""
echo "📄 9. 技术文档"

DOC_FILE="$HOME/.claude/docs/memory-system.md"
if [[ -f "$DOC_FILE" ]]; then
    if grep -q 'Streamable HTTP' "$DOC_FILE" && grep -q 'hybrid_search 内部架构' "$DOC_FILE"; then
        check "memory-system.md (含 hybrid_search 架构)" "ok"
    else
        check "memory-system.md 内容" "warn" "文档可能过时，缺少 hybrid_search 架构说明"
    fi
else
    check "memory-system.md" "fail" "文件不存在"
fi

# ── 汇总 ─────────────────────────────────────
echo ""
echo "=============================="
echo -e " 结果: ${GREEN}${PASS} 通过${NC}  ${RED}${FAIL} 失败${NC}  ${YELLOW}${WARN} 警告${NC}"
if [[ $FAIL -eq 0 ]]; then
    echo -e " ${GREEN}✅ 全链路正常！${NC}"
else
    echo -e " ${RED}❌ 有 ${FAIL} 项需要修复${NC}"
    if ! $FIX_MODE; then
        echo " 提示: 运行 bash ~/mcp-qdrant-memory/healthcheck.sh --fix 尝试自动修复"
    fi
fi
echo "=============================="
echo ""
