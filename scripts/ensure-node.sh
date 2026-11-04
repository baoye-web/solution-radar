#!/usr/bin/env bash
# scripts/ensure-node.sh — 保证 Clash 当前选中的出口节点「真的能用」
#
# 背景（2026-11-05 第 53 轮巡检实测，故障已复现并修复）：
#   ensure-proxy.sh 只保证「代理地址挂在了当前生效的网络服务上」，
#   但**不保证代理后端节点还活着**。本次故障：
#     · Clash 运行在 global 模式，GLOBAL 组指向 🇯🇵日本 05 | 高级专线
#     · 该节点上游已失效（日延迟测试全部 Timeout）
#     · 表现：curl -x http://127.0.0.1:7897 <任意目标> 全部返回 000
#     · 但 **存量已建立的 TCP 连接仍然可用**（netstat 里 7897 有大量 ESTABLISHED），
#       所以「守护进程用旧标签页发 page.fetch」看起来一切正常，
#       而「新开标签页 page.goto」直接报 net::ERR_CONNECTION_CLOSED
#     · 更隐蔽的后果：Gmail 取码通道（gmail-code.mjs）需要新开标签页导航 Gmail，
#       于是取码**静默失效**（返回 CODE:none），用户却在等验证码 —— 死锁
#
# 做法：
#   1. 用真实 HTTP 请求（走代理）探测出口是否可用，**连续 3 次**都失败才判定节点已死
#      （单次 000 不能作数，见 MEMORY.md 复用做法）
#   2. 节点已死 → 通过 Clash 的 unix socket 控制接口枚举候选节点，逐个测延迟
#   3. 选一个延迟正常（< 3000ms）的节点，切换 GLOBAL
#   4. 切完复测；仍不通则保持原状并如实报告（不无限重试）
#
# 幂等：出口正常时什么都不做，直接 exit 0。
#
# 用法：bash scripts/ensure-node.sh [--quiet]
set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1
say() { [ "$QUIET" = "1" ] || echo "[$(date +%H:%M:%S)] $*" >&2; }

# ── 0. 定位 Clash 控制 socket ───────────────────────────────────────────────
SOCK_CANDIDATES=(
  "/var/run/clash-verge-service/users/501/verge-mihomo.sock"
  "$HOME/.config/clash-verge/verge-mihomo.sock"
)
SOCK=""
for s in "${SOCK_CANDIDATES[@]}"; do
  if [ -S "$s" ]; then SOCK="$s"; break; fi
done
if [ -z "$SOCK" ]; then
  say "未找到 Clash 控制 socket，跳过节点健康检查"
  exit 0
fi

# ── 1. 用真实请求判定出口是否可用 ──────────────────────────────────────────
# 目标选 gstatic 的 204：体积极小、对节点可用性敏感、无 CDN 缓存干扰。
PROBE_URL="http://www.gstatic.com/generate_204"
PROBE_PORT="7897"

# 取 mixed-port（Clash Verge 实际监听端口）
MIXED=$(curl -s --unix-socket "$SOCK" http://localhost/configs --max-time 6 2>/dev/null \
        | python3 -c "import sys,json; print(json.load(sys.stdin).get('mixed-port') or 7897)" 2>/dev/null)
[ -n "${MIXED:-}" ] && PROBE_PORT="$MIXED"

probe_ok() {
  # 单次探测：走代理发一个 HTTP 请求，拿到任意 2xx/3xx 即视为出口可用
  local code
  code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 10 \
         -x "http://127.0.0.1:${PROBE_PORT}" "$PROBE_URL" 2>/dev/null)
  case "$code" in
    2*|3*) return 0 ;;
    *) return 1 ;;
  esac
}

# 连测 3 次，全失败才判死（单次 000 可能只是瞬时抖动）
FAILS=0
for i in 1 2 3; do
  if probe_ok; then
    FAILS=0
    break
  fi
  FAILS=$((FAILS+1))
  sleep 1
done

if [ "$FAILS" -lt 3 ]; then
  say "出口节点正常（${FAILS} 次失败 < 3），无需切换"
  exit 0
fi

say "⚠️  出口节点探测连续 3 次失败（代理端口 ${PROBE_PORT}），判定节点已失效，开始切换"

# ── 2. 枚举候选节点 ────────────────────────────────────────────────────────
# GLOBAL 的 all 列表包含 DIRECT / REJECT / 各订阅节点。
# DIRECT 与 REJECT 不能作为「代理出口」（前者是直连，后者是拒绝），
# 但直连在国内反而更稳 —— 这里只挑真正的代理节点。
CANDIDATES=$(curl -s --unix-socket "$SOCK" http://localhost/proxies --max-time 8 2>/dev/null \
  | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    g = (d.get('proxies') or {}).get('GLOBAL') or {}
    now = g.get('now') or ''
    all_names = g.get('all') or []
    bad = {'DIRECT', 'REJECT', 'COMPATIBLE', 'GLOBAL', 'PASS'}
    out = [n for n in all_names if n not in bad and n != now]
    print('\n'.join(out))
except Exception:
    pass
" 2>/dev/null)

if [ -z "$CANDIDATES" ]; then
  say "⚠️  未能枚举候选节点（Clash 控制接口异常），保持现状"
  exit 0
fi

# ── 3. 逐个测延迟，挑第一个正常的 ──────────────────────────────────────────
BEST=""
BEST_DELAY=999999
while IFS= read -r node; do
  [ -n "$node" ] || continue
  enc=$(python3 -c "import urllib.parse,sys; print(urllib.parse.quote(sys.argv[1]))" "$node" 2>/dev/null)
  [ -n "$enc" ] || continue
  resp=$(curl -s --unix-socket "$SOCK" \
         "http://localhost/proxies/${enc}/delay?timeout=5000&url=http%3A%2F%2Fwww.gstatic.com%2Fgenerate_204" \
         --max-time 10 2>/dev/null)
  delay=$(printf '%s' "$resp" | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    v = d.get('delay')
    print(v if isinstance(v, int) else '')
except Exception:
    print('')
" 2>/dev/null)
  if [ -n "$delay" ] && [ "$delay" -lt 3000 ] 2>/dev/null; then
    say "   候选可用：${node}（${delay}ms）"
    BEST="$node"
    BEST_DELAY="$delay"
    break
  fi
done <<EOF
$CANDIDATES
EOF

if [ -z "$BEST" ]; then
  say "⚠️  所有候选节点均不可用，保持当前节点不变"
  exit 0
fi

# ── 4. 切换并复测 ──────────────────────────────────────────────────────────
OLD=$(curl -s --unix-socket "$SOCK" http://localhost/proxies/GLOBAL --max-time 6 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('now') or '')" 2>/dev/null)

HTTP=$(curl -s -o /dev/null -w "%{http_code}" -X PUT --unix-socket "$SOCK" \
       -H "Content-Type: application/json" \
       -d "$(python3 -c "import json,sys; print(json.dumps({'name': sys.argv[1]}))" "$BEST")" \
       "http://localhost/proxies/GLOBAL" --max-time 8 2>/dev/null)

if [ "$HTTP" != "204" ] && [ "$HTTP" != "200" ]; then
  say "⚠️  切换 GLOBAL 到「${BEST}」失败（HTTP ${HTTP:-none}），保持原节点"
  exit 0
fi

sleep 2
NOW=$(curl -s --unix-socket "$SOCK" http://localhost/proxies/GLOBAL --max-time 6 2>/dev/null \
      | python3 -c "import sys,json; print(json.load(sys.stdin).get('now') or '')" 2>/dev/null)

if probe_ok; then
  say "✅ 已把出口节点从「${OLD:-未知}」切到「${NOW}」（${BEST_DELAY}ms），出口复测通过"
  exit 0
fi

say "⚠️  已切换到「${NOW}」但出口复测仍失败，可能上游整体不可用"
exit 0
