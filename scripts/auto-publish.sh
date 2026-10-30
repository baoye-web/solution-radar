#!/usr/bin/env bash
# scripts/auto-publish.sh — 认证完成后自动接管的守护脚本
#
# 原理（实测于 2026-10-30，open.workbuddy.cn）：
#   · 登录态本来就有效 —— /api/v2/open-platform/messages 返回 code:0
#   · partyId 为 null，因为 localStorage 的 workbuddy-auth = {"partyId":null,"certMethod":"face"}
#     → 账号已登录但未实名认证，平台不签发主体 ID
#   · 未实名时 capabilities/* 返回 403「当前用户无权访问该主体」——不是权限问题
#   · 伪造 party_id 会被服务端拒绝：「party_id not owned by current user」
#   · Cookie 在浏览器外不被接受（跳 Keycloak 登录页），所以链路必须在 page.fetch 里跑
#
# 策略：轮询等待 partyId，一拿到就自动走完 上传 → 提审 → 定价 → 发布 → 推广渠道 → 收入校验。
#
# 用法：
#   bash scripts/auto-publish.sh                    # 前台跑，实时看过程
#   bash scripts/auto-publish.sh --daemon           # 后台跑
#   bash scripts/auto-publish.sh --timeout=7200 --interval=25

set -u

ROOT="/Users/happysir/ego/auto-income-20261030/agent-radar"
LOG_DIR="$HOME/ego/auto-income-20261030"
WAIT_TIMEOUT=7200
INTERVAL=25
PRICE=19
DAEMON=0

for a in "$@"; do
  case "$a" in
    --daemon) DAEMON=1 ;;
    --timeout=*) WAIT_TIMEOUT="${a#*=}" ;;
    --interval=*) INTERVAL="${a#*=}" ;;
    --price=*) PRICE="${a#*=}" ;;
  esac
done

if [ "$DAEMON" = "1" ]; then
  mkdir -p "$LOG_DIR"
  LOG="$LOG_DIR/daemon.log"
  # 追加模式启动；用 setsid 脱离当前会话避免被回收
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$0" --timeout="$WAIT_TIMEOUT" --interval="$INTERVAL" --price="$PRICE" >> "$LOG" 2>&1 &
  else
    ( bash "$0" --timeout="$WAIT_TIMEOUT" --interval="$INTERVAL" --price="$PRICE" >> "$LOG" 2>&1 & ) &
  fi
  sleep 2
  echo "守护进程已启动，日志：$LOG"
  echo "查看进度：tail -f $LOG"
  exit 0
fi

PROBE_MJS="$LOG_DIR/_probe_party.mjs"
cat > "$PROBE_MJS" <<'EOF'
const task = await taskSpace("wb-daemon-probe");
const page = task.page("p1");
// 用认证页而非 404 页来读状态
await page.goto("https://open.workbuddy.cn/verification?type=new&from=login-no-party");
await new Promise(r => setTimeout(r, 4000));
const p = await page.evaluate(() => {
  let a = null;
  try { a = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch(e) {}
  return { pid: a?.state?.partyId || a?.partyId || null,
           m: a?.state?.certMethod || a?.certMethod || null,
           l: localStorage.getItem("loggedIn") === "true",
           u: location.href };
});
console.log("RESULT:" + JSON.stringify(p));
EOF

echo "=== 现成方案雷达 · 自动接管守护进程 ==="
echo "等待 partyId（实名认证完成后平台自动签发）"
echo "超时 ${WAIT_TIMEOUT}s，间隔 ${INTERVAL}s，定价 ${PRICE} 元/月"
echo ""
echo "需要你做一件事（一次性，约 15 分钟）："
echo "  打开 https://open.workbuddy.cn/settings/developer"
echo "  未认证会自动跳到 https://open.workbuddy.cn/verification?type=new&from=login-no-party"
echo "  选「个人认证」→ 填姓名/身份证/手机/邮箱 → 人脸识别"
echo "  需：二代身份证 + 已实名手机号 + 能做人脸识别的手机"
echo "  完成后本脚本自动接手直到上架发布，全程无需再操作。"
echo ""

cd "$ROOT" || exit 1

DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))
N=0
PARTY=""
LAST_STATE=""

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  N=$((N+1))
  RAW=$(ego-browser nodejs < "$PROBE_MJS" 2>/dev/null | grep -o 'RESULT:.*' | head -1)
  if [ -z "$RAW" ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：ego-browser 无输出，${INTERVAL}s 后重试"
    sleep "$INTERVAL"
    continue
  fi

  PARTY=$(printf '%s' "$RAW" | sed 's/^RESULT://' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('pid') or '')
except Exception:
    print('')
" 2>/dev/null)

  if [ -n "$PARTY" ]; then
    echo "[$(date +%H:%M:%S)] ✓ 第 $N 次探测拿到 partyId: $PARTY"
    break
  fi

  STATE=$(printf '%s' "$RAW" | sed 's/^RESULT://' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    m = d.get('m') or '未标认证方式'
    l = '已登录' if d.get('l') else '未登录'
    print(f\"{l}，待认证方式={m}\")
except Exception:
    print('解析失败')
" 2>/dev/null)

  if [ "$STATE" != "$LAST_STATE" ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：$STATE"
    LAST_STATE="$STATE"
  elif [ $((N % 8)) -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：仍在等待（$STATE）"
  fi
  sleep "$INTERVAL"
done

if [ -z "$PARTY" ]; then
  echo ""
  echo "✗ 超时未拿到 partyId。"
  echo "  请确认已在 https://open.workbuddy.cn/verification 完成个人认证后重跑本脚本。"
  exit 2
fi

echo ""
echo "=== 开始自动上架（预检 → 打包 → 上传 → 提审 → 定价 → 发布 → 推广渠道 → 收入校验）==="
cd "$ROOT"
ego-browser nodejs < solution-radar/scripts/publish.mjs -- --submit --publish --price "$PRICE" --monthly
RC=$?

echo ""
if [ $RC -eq 0 ]; then
  echo "=== 自动接管完成 ==="
  echo "审核结果：平台通知中心（微信最迟 24 小时返回）"
  echo "若被驳回：需真正修改内容再重提（平台公约 2.1.2 禁止无修改重提）"
  echo "重跑本脚本可继续定价与发布"
else
  echo "=== 上架流程返回码 $RC ==="
  echo "常见原因：审核尚未通过（定价与发布会失败，这属正常）"
  echo "审核通过后重跑本脚本即可完成发布。"
fi
exit $RC
