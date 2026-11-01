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
// ⚠️ 实测（2026-11-01 第 36 轮）：task.page("p1") 在没有可用标签页时
// 不抛错也不返回 null，而是返回 truthy 句柄，错误要到使用时才抛
// （"page label not found: p1" / "page p1 was closed"）。
// 因此「if (!page) 才兜底」的写法是坏的 —— 兜底分支永远进不去，
// 错误会延后到 evaluate() 才炸，守护进程把「标签页没了」误判成「还没实名认证」。
// 正确做法：逐个候选 await p.url() 做健康检查，坏的会在此抛错，正好用来跳过。
let page = null;
{
  const candidates = [];
  try { const p = task.page("p1"); if (p) candidates.push(p); } catch {}
  try { for (const p of (await task.pages()) || []) candidates.push(p); } catch {}
  for (const p of candidates) {
    try { await p.url(); page = p; break; } catch {}
  }
  if (!page) {
    try { const p = await task.newPage(); await p.url(); page = p; } catch {}
  }
}
if (!page) {
  console.log("RESULT:" + JSON.stringify({ l: false, srv: { err: "no_usable_page" }, nav: "NO_PAGE" }));
} else {
let navErr = null;
let cur = "";
try { cur = await page.url(); } catch (e) {}
if (!/open\.workbuddy\.cn/.test(cur)) {
  try {
    await page.goto("https://open.workbuddy.cn/verification?type=new&from=login-no-party");
  } catch (e) {
    navErr = String(e).slice(0, 160);
  }
  await new Promise(r => setTimeout(r, 4000));
}
const p = await page.evaluate(async (nav) => {
  let a = null;
  try { a = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch(e) {}
  // 服务端权威口径：GET /api/v2/open-platform/parties
  // 平台在该列表中按 partyType=personal / verifyStatus=verified 标记已实名主体。
  // 人脸核身通过前，平台会先创建一条 draft 主体（邮箱验证码通过即建），
  // 因此该列表还能区分「从未开始」「邮箱已验、人脸未做」「已实名」三种状态。
  let srv = null;
  try {
    const r = await fetch("/api/v2/open-platform/parties", { credentials: "include" });
    const j = await r.json();
    const list = j?.data?.parties || [];
    const verified = list.find(x => x.partyType === "personal" && x.verifyStatus === "verified");
    const draft = list.find(x => x.partyType === "personal");
    srv = {
      code: j?.code,
      count: list.length,
      verifiedPartyId: verified?.partyId || null,
      draftPartyId: draft?.partyId || null,
      draftStatus: draft?.verifyStatus || null,
      draftNextAction: draft?.nextAction || null,
      governanceStatus: draft?.governanceStatus || null,
      // ⚠️ 前端 chunk 逆向确认（2026-11-01 第 38 轮）：
      // parties 列表里**不存在** contactEmailVerified / operatorVerified 这类字段，
      // 可用判别字段只有 verifyStatus / governanceStatus。
      // 这里把服务端实际返回的 key 原样带出来，便于日志排查。
      partyKeys: list.length ? Object.keys(list[0]) : [],
    };
  } catch(e) { srv = { err: String(e).slice(0, 80) }; }
  return { pid: a?.state?.partyId || a?.partyId || null,
           m: a?.state?.certMethod || a?.certMethod || null,
           l: localStorage.getItem("loggedIn") === "true",
           u: location.href,
           srv: srv,
           nav: nav || null };
}, navErr);
console.log("RESULT:" + JSON.stringify(p));
}
EOF

echo "=== 现成方案雷达 · 自动接管守护进程 ==="
echo "等待 partyId（实名认证完成后平台自动签发）"
echo "超时 ${WAIT_TIMEOUT}s，间隔 ${INTERVAL}s，定价 ${PRICE} 元/月"
echo ""
echo "需要你做一件事（一次性，约 15 分钟）："
echo "  ✅ 浏览器里已为你打开好「个人认证」表单，且已登录（无需再扫码登录）"
echo "     窗口标签页：WorkBuddy 开放平台 → 路径 /verification/personal"
echo "  填 4 项：真实姓名 / 身份证号 / 手机号 / 邮箱 → 点「发送验证码」"
echo "  📬 邮箱验证码本脚本会自动从 Gmail 取，日志里会打印出来，直接粘贴即可"
echo "  勾选同意条款 → 点「核验信息」→ 第 2 步「人脸识别」（用手机对着摄像头）"
echo "  需：二代身份证 + 已实名手机号 + 能做人脸识别的手机"
echo "  完成后本脚本自动接手直到上架发布，全程无需再操作。"
echo ""
echo "  进度可自查：本脚本每轮会打印当前阶段（未开始 / 邮箱已验证待人脸 / 已实名）"
echo ""

cd "$ROOT" || exit 1

DEADLINE=$(( $(date +%s) + WAIT_TIMEOUT ))
N=0
PARTY=""
LAST_STATE=""
LAST_CODE=""

while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  N=$((N+1))
  # 注意：ego-browser nodejs 把 console.log 输出写到 stderr，不是 stdout。
  # 必须用 2>&1 合并后再过滤，否则永远拿不到 RESULT（曾导致守护进程一直误报「无输出」）。
  RAW=$(ego-browser nodejs < "$PROBE_MJS" 2>&1 | grep -o 'RESULT:.*' | head -1)
  if [ -z "$RAW" ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：ego-browser 无输出，${INTERVAL}s 后重试"
    sleep "$INTERVAL"
    continue
  fi

  PARTY=$(printf '%s' "$RAW" | sed 's/^RESULT://' | python3 -c "
import sys, json
try:
    d = json.load(sys.stdin)
    print(d.get('pid') or d.get('srv',{}).get('verifiedPartyId') or '')
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
    s = d.get('srv') or {}
    l = '已登录' if d.get('l') else '未登录'
    n = s.get('count', 0)
    if s.get('verifiedPartyId'):
        stage = '已实名(服务端已签发)'
    elif n == 0:
        # 主体列表为空 = 主体还没建出来。
        # 关键顺序（前端 chunk 逆向确认）：邮箱验证码是在「创建主体」时随 POST /parties
        # 一起提交的（body: {partyType, displayName, contactEmail, emailCode}），
        # 也就是说**先要验证码才能建主体** → 所以 count==0 这一阶段正是用户最需要验证码的时候。
        stage = '未开始认证(主体列表为空,等验证码)'
    else:
        # 有主体但未 verified = 邮箱已通过、正在做人脸核身
        stage = '主体已建,待人脸核身(draft)'
    # needCode=1 = 还没实名，就该去 Gmail 取码（用户可能在等码填表）。
    # 已实名后一律不再取码。
    need = '0' if s.get('verifiedPartyId') else '1'
    print(f\"{l}，{stage}|{need}\")
except Exception:
    print('解析失败|0')
" 2>/dev/null)

  STAGE="${STATE%%|*}"
  NEED_CODE="${STATE##*|}"

  if [ "$STAGE" != "$LAST_STATE" ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：$STAGE"
    LAST_STATE="$STAGE"
  elif [ $((N % 8)) -eq 0 ]; then
    echo "[$(date +%H:%M:%S)] 第 $N 次探测：仍在等待（${STAGE}）"
  fi

  # 邮箱验证码自动获取（Gmail 已登录 imbaoye@gmail.com）。
  # ⚠️ 触发时机已修正（2026-11-01 第 38 轮）：
  #   验证码是在**创建主体**时随 POST /parties 一起提交的，
  #   所以「主体列表为空」这一阶段正是用户最需要验证码的时候（此前逻辑完全相反 → 永远不触发）。
  # 取码会导航 Gmail 抢焦点，故限流：每 4 轮取一次（≈100s），且码变了才打印。
  if [ "$NEED_CODE" = "1" ] && [ -f "$ROOT/scripts/gmail-code.mjs" ] && [ $((N % 4)) -eq 1 ]; then
    CODE=$(ego-browser nodejs < "$ROOT/scripts/gmail-code.mjs" 2>&1 | grep -o 'CODE:[A-Za-z0-9]*' | head -1)
    if [ -n "$CODE" ] && [ "$CODE" != "CODE:none" ]; then
      if [ "$CODE" != "$LAST_CODE" ]; then
        echo "[$(date +%H:%M:%S)] 📬 取到邮箱验证码：${CODE#CODE:}（可直接填入「邮箱验证码」输入框）"
        LAST_CODE="$CODE"
      fi
    fi
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
cd "$ROOT" || exit 1
# ego-browser nodejs 不接受尾随参数，且**不透传自定义环境变量**
# （实测 process.env 里只有 PATH/HOME），所以参数必须走「脚本自己会去读的文件」。
# publish.mjs 的配置候选路径（按顺序取第一个存在的）：
#   1) /Users/happysir/ego/auto-income-20261030/publish-config.json
#   2) /tmp/wb_publish_config.json
# 这里写第 1 个路径；注意不要再用 WB_PUBLISH_CONFIG 传参（传了也没用）。
PUB_CFG="/Users/happysir/ego/auto-income-20261030/publish-config.json"
cat > "$PUB_CFG" <<CFG
{"submit":true,"publish":true,"price":${PRICE},"monthly":true,"resume":true}
CFG

# publish.mjs 是幂等的：按技能名查已有资产并续跑
#   0 = 全部完成（已发布 + 定价 + 推广渠道 + 收入校验）
#   2 = 缺 partyId（未实名）
#   5 = 平台接口拒绝（真错误）
#   6 = 已提交审核，等待平台审核（正常状态，不是失败）
# 审核最长 24 小时，所以这里循环等待：审核一通过就自动完成定价+发布+推广渠道。
PUBLISH_DONE=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
  ego-browser nodejs < solution-radar/scripts/publish.mjs
  RC=$?
  echo ""
  case $RC in
    0)
      echo "=== 自动接管完成 ==="
      echo "审核结果：平台通知中心（微信最迟 24 小时返回）"
      echo "若被驳回：需真正修改内容再重提（平台公约 2.1.2 禁止无修改重提）"
      PUBLISH_DONE=1
      break
      ;;
    6)
      echo "=== 已提交审核，等待平台审核（这属正常，不是失败）==="
      echo "审核通过后本守护会自动完成定价 + 发布 + 推广渠道。"
      echo "下次轮询：$INTERVAL s 后"
      sleep "$INTERVAL"
      continue
      ;;
    13)
      # 查已有资产失败（会话过期 / X-Party-Id 失效）→ 属可重试的瞬时故障，
      # 刻意中止以避免重复上传造成重复资产，因此必须重试而不是放弃。
      echo "查询已有资产失败（会话/X-Party-Id 失效），$INTERVAL s 后重试（已中止上传，不会产生重复资产）。"
      sleep "$INTERVAL"
      continue
      ;;
    *)
      echo "=== 上架流程返回码 $RC ==="
      if [ $RC -eq 2 ]; then
        echo "缺 partyId：实名认证尚未完成（或已失效），继续等待。"
        sleep "$INTERVAL"
        continue
      fi
      if [ $RC -eq 5 ]; then
        echo "平台接口拒绝：请检查上面的返回码与 msg（多为包结构/frontmatter/分类问题）。"
      fi
      echo "本轮结束，不再自动重试。"
      break
      ;;
  esac
done

if [ "$PUBLISH_DONE" -eq 1 ]; then
  exit 0
fi
echo ""
echo "守护结束：上架流程未走完（可能是审核仍在进行，或遇到需人工处理的错误）。"
echo "修好后重跑：bash scripts/auto-publish.sh"
exit 1
