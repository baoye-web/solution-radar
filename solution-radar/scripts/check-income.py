#!/usr/bin/env python3
"""
scripts/check-income.py — 收入与订单查询

开放平台结算接口：
    GET /api/v1/open-platform/charge-developer/merchant/income-stats?partyId=
        总收入 / 余额 / 可提现 / 收入结构（专家 vs 技能）/ 每日收入
    GET /api/v1/open-platform/charge-developer/merchant/orders?partyId=&pageSize=&cursor=
        订单明细 + 月度统计
    GET /api/v1/open-platform/charge-developer/merchant/order-sources?partyId=
        订单来源分布
    GET /api/v1/open-platform/charge-developer/merchant/query-sub-merchant?partyId=
        收款账户进件状态
    GET /api/v1/open-platform/charge-developer/merchant/cert-status?partyId=
        签约状态
    GET /api/v1/open-platform/charge-developer/merchant/account?partyId=
        账户信息
    POST /api/v1/open-platform/charge-developer/merchant/withdraw
        发起提现（仅触发，实际到账需微信侧确认）

用法：
    export WB_COOKIE='...' WB_PARTY_ID='...'
    python3 scripts/check-income.py
    python3 scripts/check-income.py --orders
    python3 scripts/check-income.py --withdraw
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WB_BASE", "https://open.workbuddy.cn")
V1 = f"{BASE}/api/v1/open-platform/charge-developer/merchant"


def call(method, url, body=None, cookie=None, party=None):
    cookie = cookie or os.environ.get("WB_COOKIE", "")
    party = party or os.environ.get("WB_PARTY_ID", "")
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Origin": BASE,
        "Referer": f"{BASE}/monetization",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    if party:
        headers["X-Party-Id"] = party
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(txt)
            except json.JSONDecodeError:
                return r.status, {"_raw": txt[:500]}
    except urllib.error.HTTPError as e:
        t = ""
        try:
            t = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        return e.code, {"_http_error": e.code, "body": t}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--party-id", default="")
    ap.add_argument("--orders", action="store_true", help="订单明细")
    ap.add_argument("--sources", action="store_true", help="订单来源")
    ap.add_argument("--status", action="store_true", help="收款与签约状态")
    ap.add_argument("--withdraw", action="store_true", help="发起提现")
    args = ap.parse_args()

    if not os.environ.get("WB_COOKIE"):
        print("缺少 WB_COOKIE 环境变量")
        sys.exit(2)
    pid = args.party_id or os.environ.get("WB_PARTY_ID", "")
    if not pid:
        print("缺少 partyId（需先完成实名认证）")
        sys.exit(2)

    if args.withdraw:
        st, r = call("POST", f"{V1}/withdraw?partyId={pid}", {"partyId": pid}, party=pid)
        print(f"[withdraw] {st} {json.dumps(r, ensure_ascii=False)[:400]}")
        print("注意：API 仅触发提现，实际到账需在微信侧确认。")
        return

    if args.orders:
        st, r = call("GET", f"{V1}/orders?partyId={pid}&pageSize=20", party=pid)
        print(f"[orders] {st}")
        d = (r.get("data") or {}) if isinstance(r, dict) else {}
        items = d.get("items") or []
        if not items:
            print(json.dumps(r, ensure_ascii=False)[:600])
        for o in items:
            print(f"  {o.get('paidAt','')[:10]}  {o.get('sourceType',''):<8} "
                  f"{o.get('source','')[:26]:<26} ¥{o.get('paidAmount','?'):<8} "
                  f"{o.get('status','')} payBy={o.get('payMode','')}")
        ms = d.get("monthStats") or []
        for m in ms:
            print(f"  月度 {m.get('yearMonth')}: 收入 {m.get('income')} 已提现 {m.get('withdrawn')}")
        return

    if args.sources:
        st, r = call("GET", f"{V1}/order-sources?partyId={pid}", party=pid)
        print(f"[sources] {st} {json.dumps(r, ensure_ascii=False)[:800]}")
        return

    if args.status:
        for name, path in [
            ("进件状态", f"{V1}/query-sub-merchant?partyId={pid}"),
            ("签约状态", f"{V1}/cert-status?partyId={pid}"),
            ("账户信息", f"{V1}/account?partyId={pid}"),
        ]:
            st, r = call("GET", path, party=pid)
            print(f"[{name}] {st} {json.dumps(r, ensure_ascii=False)[:300]}")
        return

    # 默认：收入概览
    st, r = call("GET", f"{V1}/income-stats?partyId={pid}", party=pid)
    print(f"[income-stats] {st}")
    print(json.dumps(r, ensure_ascii=False, indent=2)[:2000])


if __name__ == "__main__":
    main()
