#!/usr/bin/env bash
# scripts/auto-publish.sh — 认证完成后自动接管的守护脚本
#
# 原理：开放平台后台接口需要 Cookie（登录态）+ X-Party-Id（主体）。
# 实测（2026-10-30）：
#   · 登录态本来就有效（messages 接口返回 code:0）
#   · partyId 为 null，因为 localStorage 的 workbuddy-auth = {"partyId":null,"certMethod":"face"}
#     → 账号已登录但未实名认证，平台不签发主体 ID
#   · 未实名时 capabilities/* 返回 403「当前用户无权访问该主体」——不是权限问题
#   · Cookie 在浏览器外不被接受（跳 Keycloak 登录页），所以整条链路必须在 page.fetch 里跑
#
# 策略：轮询等待 partyId，一拿到就自动走完上传 → 提审 → 定价 → 发布 → 推广渠道 → 收入接口。
#
# 用法：
#   bash scripts/auto-publish.sh              # 前台跑，能实时看过程
#   bash scripts/auto-publish.sh --daemon     # 后台跑，日志在 ~/ego/auto-income-20261030/daemon.log

set -u

ROOT="/Users/happysir/ego/auto-income-20261030/agent-radar"
LOG_DIR="$HOME/ego/auto-income-20261030"
WAIT_TIMEOUT=7200
INTERVAL=30
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
  # 用 setsid 脱离当前会话，避免被父进程回收
  if command -v setsid >/dev/null 2>&1; then
    setsid bash "$0" --timeout="$WAIT_TIMEOUT" --interval="$INTERVAL" --price="$PRICE" \
      >> "$LOG_DIR/daemon.log" 2>&1 &
  else
    nohup bash "$0" --timeout="$WAIT_TIMEOUT" --interval="$INTERVAL" --price="$PRICE" \
      >> "$LOG_DIR/daemon.log" 2>&1 &
    disown
  fi
  echo "守护进程已启动，日志：$LOG_DIR/daemon.log"
  exit 0
fi

echo "=== 现成方案雷达 · 自动接管守护进程 ==="
echo "等待 partyId（实名认证完成后平台自动签发）"
echo "超时 ${WAIT_TIMEOUT}s，间隔 ${INTERVAL}s"
echo ""
echo "需要你做一件事（一次性）："
echo "  打开 https://open.workbuddy.cn/settings/developer 完成实名认证"
echo "  需：二代身份证 + 已实名手机号 + 能做人脸识别的手机"
echo "  完成后本脚本会自动接手，直到上架发布，全程无需再操作。"
echo ""

cd "$ROOT" || exit 1

# 检查 zip 是否已存在（publish.mjs 会自己打包，这里只做提示）
DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))
N=0

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  N=$((N+1))
  echo "[$(date +%H:%M:%S)] 第 $N 次探测 partyId…"

  # 探测 partyId（读 localStorage）
  PID=$(cat > /tmp/_pid_probe.mjs <<'EOF'
const task = await taskSpace("wb-daemon-probe");
const page = task.page("p1");
await page.goto("https://open.workbuddy.cn/developer-profile");
await new Promise(r => setTimeout(r, 3500));
const p = await page.evaluate(() => {
  let a = null;
  try { a = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch(e) {}
  return { pid: a?.partyId || null, m: a?.certMethod || null, l: localStorage.getItem("loggedIn") === "true" };
});
console.log("RESULT:" + JSON.stringify(p));
EOF
ego-browser nodejs < /tmp/_pid_probe.mjs 2>/dev/null | grep -o 'RESULT:.*' | sed 's/RESULT://')

  if echo "$PID" | grep -q '"pid":"[0-9a-f]'; then
    PARTY=$(echo "$PID" | python3 -c "import sys,json;print(json.load(sys.stdin)['pid'])" 2>/dev/null)
    if [ -n "$PARTY" ]; then
      echo "[$(date +%H:%M:%S)] ✓ 拿到 partyId: $PARTY"
      break
    fi
  fi

  # 未拿到时，用 publish.mjs 的 --wait 模式跑一次（它内部会带预检+打包+等待）
  # 这里只做轻量等待，避免频繁打包
  sleep "$INTERVAL"
done

if ! echo "$PID" | grep -q '"pid":"[0-9a-f]'; then
  echo ""
  echo "✗ 超时未拿到 partyId。"
  echo "  请确认已在 https://open.workbuddy.cn/settings/developer 完成实名认证后重跑。"
  exit 2
fi

echo ""
echo "=== 开始自动上架（上传 → 提审 → 定价 → 发布 → 推广渠道）==="
cd "$ROOT"
ego-browser nodejs < solution-radar/scripts/publish.mjs -- --submit --publish --price "$PRICE" --monthly
RC=$?

echo ""
if [ $RC -eq 0 ]; then
  echo "=== 自动接管完成 ==="
  echo "审核结果：平台通知中心（最迟 24 小时）"
  echo "若被驳回，需真正修改内容再重提（平台公约 2.1.2 禁止无修改重提）"
else
  echo "=== 上架流程返回码 $RC ==="
  echo "常见原因：审核尚未通过（定价与发布会失败，这是正常的）"
  echo "重新运行本脚本可继续。"
fi
exit $RC
