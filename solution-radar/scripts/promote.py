#!/usr/bin/env python3
"""
scripts/promote.py — 推广渠道与效果查询

开放平台「推广管理」相关接口：
    POST /api/v2/open-platform/share/channels/create     创建推广渠道
    POST /api/v2/open-platform/share/channels/list       渠道列表
    POST /api/v2/open-platform/share/analytics/query     推广漏斗（clicks→installs→newUsers）
    POST /api/v2/open-platform/share/analytics/export-events  导出明细 CSV

三种分享载体（share_way）：
    link      分享链接（网页/图文/社群）
    wxacode   小程序码（海报/扫码）
    qrCode    二维码

用法：
    export WB_COOKIE='...' WB_PARTY_ID='...'
    python3 scripts/promote.py --create --name 现成方案雷达
    python3 scripts/promote.py --list
    python3 scripts/promote.py --analytics --days 30
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

BASE = os.environ.get("WB_BASE", "https://open.workbuddy.cn")
V2 = f"{BASE}/api/v2/open-platform"


def call(method, path, body=None, cookie=None, party=None, raw_csv=False):
    cookie = cookie or os.environ.get("WB_COOKIE", "")
    party = party or os.environ.get("WB_PARTY_ID", "")
    headers = {
        "Cookie": cookie,
        "Content-Type": "application/json",
        "Accept": "application/json" if not raw_csv else "text/csv",
        "Origin": BASE,
        "Referer": f"{BASE}/monetization",
        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    }
    if party:
        headers["X-Party-Id"] = party
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(f"{BASE}{path}", data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            txt = r.read().decode("utf-8", "replace")
            if raw_csv:
                return r.status, txt
            try:
                return r.status, json.loads(txt)
            except json.JSONDecodeError:
                return r.status, {"_raw": txt[:500]}
    except urllib.error.HTTPError as e:
        body_txt = ""
        try:
            body_txt = e.read().decode("utf-8", "replace")[:500]
        except Exception:  # noqa: BLE001
            pass
        return e.code, {"_http_error": e.code, "body": body_txt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--create", action="store_true", help="创建推广渠道")
    ap.add_argument("--list", action="store_true", help="列出渠道")
    ap.add_argument("--analytics", action="store_true", help="推广效果")
    ap.add_argument("--export", action="store_true", help="导出事件明细 CSV")
    ap.add_argument("--name", default="现成方案雷达", help="渠道名称")
    ap.add_argument("--tag", default="GitHub", help="渠道标签（用于区分来源）")
    ap.add_argument("--asset-id", default="", help="资产 ID（创建渠道时必填）")
    ap.add_argument("--asset-type", default="skill", choices=["skill", "expert", "connector"])
    ap.add_argument("--days", type=int, default=30, choices=[7, 30, 90])
    ap.add_argument("--party-id", default="")
    args = ap.parse_args()

    if not os.environ.get("WB_COOKIE"):
        print("缺少 WB_COOKIE 环境变量")
        sys.exit(2)

    if args.create:
        if not args.asset_id:
            print("创建渠道需要 --asset-id（先跑 publish-to-openplatform.py 拿到 assetId）")
            sys.exit(2)
        for way in ("link", "wxacode"):
            st, r = call("POST", f"{V2}/share/channels/create", {
                "assetId": args.asset_id,
                "assetType": args.asset_type,
                "channelTag": f"{args.tag}-{way}",
                "shareWay": way,
                "channelName": args.name,
            }, party=args.party_id)
            print(f"[create:{way}] {st} {json.dumps(r, ensure_ascii=False)[:400]}")
        return

    if args.list:
        st, r = call("POST", f"{V2}/share/channels/list", {"page": 1, "pageSize": 50},
                     party=args.party_id)
        print(f"[list] {st}")
        data = (r.get("data") or {}) if isinstance(r, dict) else {}
        items = data.get("list") or data.get("items") or []
        if not items:
            print(json.dumps(r, ensure_ascii=False)[:600])
        for it in items:
            print(f"  {it.get('channelTag','?')[:20]:<20} clicks={it.get('clicks',0):<6} "
                  f"installs={it.get('installs',0):<6} newUsers={it.get('newUsers',0):<6} "
                  f"id={it.get('channelId','')[:16]}")
        return

    if args.analytics:
        st, r = call("POST", f"{V2}/share/analytics/query", {
            "periodDays": args.days,
            "page": 1,
            "pageSize": 50,
        }, party=args.party_id)
        print(f"[analytics:{args.days}d] {st}")
        print(json.dumps(r, ensure_ascii=False, indent=2)[:2500])
        return

    if args.export:
        st, txt = call("POST", f"{V2}/share/analytics/export-events", {
            "periodDays": args.days,
        }, party=args.party_id, raw_csv=True)
        out = Path(__file__).parent.parent / "reports" / f"promote-events-{args.days}d.csv"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(txt, encoding="utf-8-sig")  # 带 BOM，Excel 友好
        print(f"[export] {st} → {out}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
