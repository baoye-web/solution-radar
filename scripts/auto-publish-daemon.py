#!/usr/bin/env python3
"""
scripts/auto-publish-daemon.py — 认证完成后自动接管的守护脚本

原理：
    开放平台后台接口需要两个头 —— Cookie（登录态）+ X-Party-Id（主体 ID）。
    实测结论（2026-10-30）：
      · Cookie 登录态**本来就有效**（/api/v2/open-platform/messages 返回 code:0）
      · partyId 为 null，因为 localStorage 里 workbuddy-auth = {"partyId":null,"certMethod":"face"}
        → 即：**账号已登录，但未完成实名认证**，平台不签发主体 ID
      · 未实名时 capabilities/* 返回 403「当前用户无权访问该主体」
        → 这不是权限问题，是缺少 party_id

因此本脚本的策略是**轮询等待**：
    每 N 秒尝试读取 partyId（从页面 localStorage 的 workbuddy-auth 取，
    以及 developer-profile/status 接口取）。一旦拿到，就立即走完
    上传 → 提交审核 → 定价 → 发布 → 建推广渠道 → 拉数据，全链路不再需要人工。

用法：
    # 前台跑（能立刻看到过程）
    python3 scripts/auto-publish-daemon.py --wait --timeout 3600

    # 后台跑（等你去完成认证，它自己接手）
    python3 scripts/auto-publish-daemon.py --wait --timeout 7200 --daemon
"""

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT_DIR = ROOT / "solution-radar" / "scripts"
EGO_DIR = Path.home() / "ego" / "auto-income-20261030"

# 从浏览器 localStorage 取 partyId 的探测脚本
PROBE_JS = r"""
const task = await taskSpace("wb-daemon");
const page = task.page("p1");
await page.goto("https://open.workbuddy.cn/developer-profile");
await new Promise(r => setTimeout(r, 4000));
const out = await page.evaluate(() => {
  let auth = null;
  try { auth = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch(e) {}
  const ls = {};
  for (let i=0;i<localStorage.length;i++){const k=localStorage.key(i); if(/party|auth|user|uid/i.test(k)) ls[k]=String(localStorage.getItem(k)).slice(0,300);}
  return { auth, ls, url: location.href, text: document.body.innerText.slice(0,200).replace(/\n{2,}/g," | ") };
});
console.log(JSON.stringify(out));
"""


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


def extract_session():
    """从浏览器导出 Cookie（httpOnly 需 CDP）。返回 cookie 字符串。"""
    ex = SCRIPT_DIR / "extract-session.mjs"
    if not ex.exists():
        return None
    try:
        r = subprocess.run(["ego-browser", "nodejs", str(ex)],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None
    for line in r.stdout.split("\n"):
        line = line.strip()
        if line.startswith("export WB_COOKIE="):
            val = line.split("=", 1)[1].strip()
            return val.strip("'").replace("'\\''", "'")
    return None


def probe_party_id():
    """打开开发者页面，读取 partyId。返回 (party_id, extra_dict)。"""
    probe_file = EGO_DIR / "_probe_party.mjs"
    probe_file.parent.mkdir(parents=True, exist_ok=True)
    probe_file.write_text(PROBE_JS, encoding="utf-8")
    try:
        r = subprocess.run(["ego-browser", "nodejs", str(probe_file)],
                           capture_output=True, text=True, timeout=180)
    except subprocess.TimeoutExpired:
        return None, {"error": "probe timeout"}
    out = r.stdout.strip()
    # 取最后一行 JSON
    for line in reversed(out.split("\n")):
        line = line.strip()
        if line.startswith("{"):
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            auth = d.get("auth") or {}
            pid = auth.get("partyId")
            if pid:
                return pid, d
            return None, d
    return None, {"raw": out[-500:], "stderr": r.stderr[-300:]}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wait", action="store_true", help="轮询等待 partyId（认证完成后自动继续）")
    ap.add_argument("--timeout", type=int, default=3600, help="等待超时秒数")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒数")
    ap.add_argument("--price", type=int, default=19)
    ap.add_argument("--monthly", action="store_true", default=True)
    ap.add_argument("--daemon", action="store_true", help="放到后台跑")
    args = ap.parse_args()

    if args.daemon:
        # 后台化：nohup 不可用（会被 session 回收），改用 setsid 风格的 detached
        logf = open(EGO_DIR / "daemon.log", "a", encoding="utf-8")
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()),
             "--wait", "--timeout", str(args.timeout),
             "--interval", str(args.interval),
             "--price", str(args.price)],
            stdout=logf, stderr=logf, stdin=subprocess.DEVNULL,
            start_new_session=True,
        )
        log(f"守护进程已启动，日志：{EGO_DIR/'daemon.log'}")
        return

    log("=== 现成方案雷达 · 自动接管守护进程 ===")
    log(f"等待 partyId（实名认证完成后平台自动签发），超时 {args.timeout}s，间隔 {args.interval}s")
    log("你现在需要做的：打开 https://open.workbuddy.cn/settings/developer 完成实名认证")
    log("（需要二代身份证 + 已实名手机号 + 能做人脸识别的手机。做完就不用管了。）")
    log("")

    # 先拿一次 Cookie（登录态本已有效，只是缺 partyId）
    cookie = extract_session()
    if cookie:
        os.environ["WB_COOKIE"] = cookie
        log(f"✓ 已从浏览器提取 Cookie（{len(cookie)} 字符）")
    else:
        log("⚠ 未能提取 Cookie，稍后重试")

    party_id = None
    deadline = time.time() + args.timeout
    tries = 0

    while time.time() < deadline:
        tries += 1
        pid, extra = probe_party_id()
        if pid:
            party_id = pid
            log(f"✓ 拿到 partyId: {pid}（第 {tries} 次探测）")
            break
        auth = (extra or {}).get("auth") or {}
        state = "已登录未实名" if auth.get("partyId") is None else "状态未知"
        log(f"  [{tries}] 未取到 partyId（{state}），{args.interval}s 后重试")
        # Cookie 可能过期，定期重取
        if tries % 4 == 0:
            c = extract_session()
            if c:
                os.environ["WB_COOKIE"] = c
        time.sleep(args.interval)

    if not party_id:
        log("✗ 超时未拿到 partyId。请确认已完成实名认证。")
        sys.exit(2)

    # 拿到 partyId 后，走完整上架链路
    env = dict(os.environ)
    env["WB_PARTY_ID"] = party_id
    log("")
    log("=== 开始自动上架 ===")

    # 1) 上传 + 提交审核
    log("[1/4] 上传并提交审核")
    r1 = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "publish-to-openplatform.py"),
         "--skill-dir", str(ROOT / "solution-radar"), "--submit"],
        env=env, capture_output=True, text=True, timeout=600)
    log(r1.stdout[-1500:])
    if r1.returncode != 0:
        log(r1.stderr[-800:])
        log("上传阶段出错，停止。")
        sys.exit(r1.returncode)

    # 2) 定价 + 发布（审核可能还没过，失败不致命——等通过后可重跑）
    log("[2/4] 尝试定价并发布（审核未过时会失败，属正常）")
    r2 = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "publish-to-openplatform.py"),
         "--skill-dir", str(ROOT / "solution-radar"),
         "--price", str(args.price), "--monthly", "--publish"],
        env=env, capture_output=True, text=True, timeout=600)
    log(r2.stdout[-1200:])
    log(r2.stderr[-500:])

    # 3) 建推广渠道
    log("[3/4] 创建推广渠道（分享链接 + 小程序码）")
    r3 = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "promote.py"), "--party-id", party_id],
        env=env, capture_output=True, text=True, timeout=300)
    log(r3.stdout[-1000:])
    log(r3.stderr[-400:])

    # 4) 拉一次收入数据（此时应该是 0，但能验证接口通）
    log("[4/4] 验证收入接口")
    r4 = subprocess.run(
        [sys.executable, str(SCRIPT_DIR / "check-income.py"), "--party-id", party_id],
        env=env, capture_output=True, text=True, timeout=300)
    log(r4.stdout[-800:])
    log(r4.stderr[-300:])

    log("")
    log("=== 自动接管完成 ===")
    log("后续：审核通过后重跑一次 publish-to-openplatform.py --price N --publish 即可上架")
    log("查看审核状态与收入：用 check-income.py 与 promote.py")


if __name__ == "__main__":
    main()
