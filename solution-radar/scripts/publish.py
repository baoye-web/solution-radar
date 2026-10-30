#!/usr/bin/env python3
"""
radar/publish.py — 现成方案雷达 · 发布层

把 score.py 的结构化结果渲染成三种产物：
    1. site/index.html          —— 静态站（GitHub Pages 托管，零成本，自动更新）
    2. site/feed.json           —— 增量订阅源（供付费 skill / 第三方拉取）
    3. reports/YYYY-MM-DD.md    —— 当日决策清单（可发内容、可归档）

设计原则：产物必须是「可交付的物」，不是营销话术。
每个条目都能独立回答：这是什么 / 代价是什么 / 能拿到什么 / 证据在哪 / 有什么红线。
用法:
    python3 radar/publish.py --in data/entries.json --outdir site
"""

import argparse
import html
import json
import os
import time
from datetime import datetime, timezone

TIER_LABEL = {
    "permissive": "宽松商用",
    "copyleft-weak": "弱 Copyleft",
    "copyleft-strong": "强 Copyleft",
    "source-available": "商用受限",
    "custom": "自定义条款",
    "none": "无许可证",
    "other": "待核对",
}


def esc(s):
    return html.escape(str(s or ""))


def render_html(entries, meta):
    """渲染静态站首页。刻意不依赖任何前端框架与外链 CDN——保证离线可读、无追踪。"""
    gen = meta.get("generated_at", "")
    date = gen[:10]
    cards = []
    for e in entries:
        tier = e.get("license_tier", "other")
        badge = TIER_LABEL.get(tier, tier)
        badge_cls = {
            "permissive": "ok", "copyleft-weak": "warn", "copylist-strong": "bad",
            "copyleft-strong": "bad", "source-available": "warn",
            "custom": "bad", "none": "bad", "other": "bad",
        }.get(tier, "warn")
        risk_items = "".join(f"<li>{esc(r)}</li>" for r in e.get("risks", []))
        cost_items = "".join(f"<li>{esc(c)}</li>" for c in e.get("costs", []))
        ben_items = "".join(f"<li>{esc(b)}</li>" for b in e.get("benefits", []))
        src_items = "".join(f"<li>{esc(s)}</li>" for s in e.get("sources", []))
        cards.append(f"""<article class="card">
  <div class="head">
    <div class="score"><span class="num">{e['score']}</span><span class="lbl">综合分</span></div>
    <div class="titles">
      <h2><a href="{esc(e['url'])}" target="_blank" rel="noopener">{esc(e['name'])}</a></h2>
      <p class="one">{esc(e['one_liner'])}</p>
    </div>
    <span class="badge {badge_cls}">{esc(badge)}</span>
  </div>
  <div class="decision">{esc(e['decision'])}</div>
  <div class="grid">
    <section><h3>代价</h3><ul>{cost_items}</ul></section>
    <section><h3>能拿到什么</h3><ul>{ben_items}</ul></section>
    <section><h3>证据链</h3><ul>{src_items}</ul></section>
    <section class="risk"><h3>红线</h3><ul>{risk_items}</ul></section>
  </div>
  <div class="foot">★ {e['stars']:,} · ⑂ {e['forks']:,} · 最后提交 {e['days_since_push']} 天前 · {esc(e.get('language') or '—')}</div>
</article>""")

    topics = "、".join(meta.get("topics", [])) or "—"
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>现成方案雷达 · {esc(date)}</title>
<meta name="description" content="每条方案都补齐代价、收益、来源、红线四个维度的开源方案决策清单，每日自动更新。">
<style>
:root{{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--bd:#e3e2de;--ok:#1a7f4b;--warn:#9a6b00;--bad:#b03030;--card:#fff;--shadow:0 1px 2px rgba(0,0,0,.04)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#e8e7e3;--mut:#98978f;--bd:#2e2e2b;--ok:#5cc98d;--warn:#d9a441;--bad:#e07a7a;--card:#1d1d1b;--shadow:none}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft Yahei",sans-serif}}
.wrap{{max-width:920px;margin:0 auto;padding:40px 20px 80px}}
header{{border-bottom:1px solid var(--bd);padding-bottom:24px;margin-bottom:32px}}
h1{{margin:0 0 8px;font-size:26px;letter-spacing:-.02em}}
.meta{{color:var(--mut);font-size:13px;display:flex;gap:18px;flex-wrap:wrap}}
.meta b{{color:var(--fg);font-weight:600}}
.rules{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px 20px;margin:24px 0 32px;font-size:13.5px;box-shadow:var(--shadow)}}
.rules h2{{font-size:14px;margin:0 0 8px;letter-spacing:.02em}}
.rules ol{{margin:0;padding-left:20px;color:var(--mut)}}
.rules li{{margin:3px 0}}
.rules b{{color:var(--fg)}}
.card{{background:var(--card);border:1px solid var(--bd);border-radius:12px;padding:20px;margin-bottom:18px;box-shadow:var(--shadow)}}
.head{{display:flex;gap:16px;align-items:flex-start}}
.score{{flex:0 0 56px;text-align:center;background:var(--bg);border:1px solid var(--bd);border-radius:9px;padding:8px 0}}
.score .num{{display:block;font-size:22px;font-weight:700;line-height:1.1}}
.score .lbl{{display:block;font-size:10px;color:var(--mut);margin-top:2px}}
.titles{{flex:1;min-width:0}}
.titles h2{{margin:0 0 4px;font-size:16.5px;font-weight:650}}
.titles h2 a{{color:var(--fg);text-decoration:none;border-bottom:1px solid transparent}}
.titles h2 a:hover{{border-bottom-color:var(--fg)}}
.one{{margin:0;color:var(--mut);font-size:13.5px;overflow-wrap:anywhere}}
.badge{{flex:0 0 auto;font-size:11.5px;padding:3px 9px;border-radius:20px;border:1px solid}}
.badge.ok{{color:var(--ok);border-color:currentColor}}
.badge.warn{{color:var(--warn);border-color:currentColor}}
.badge.bad{{color:var(--bad);border-color:currentColor}}
.decision{{margin:14px 0 4px;padding:9px 13px;background:var(--bg);border-left:3px solid var(--fg);border-radius:0 6px 6px 0;font-size:13.5px;font-weight:550}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px;margin-top:16px}}
@media(max-width:640px){{.grid{{grid-template-columns:1fr}}}}
.grid h3{{margin:0 0 5px;font-size:12px;text-transform:uppercase;letter-spacing:.06em;color:var(--mut);font-weight:600}}
.grid ul{{margin:0;padding-left:16px;font-size:13px}}
.grid li{{margin:2px 0;color:var(--fg)}}
.grid section.risk li{{color:var(--bad)}}
.foot{{margin-top:14px;padding-top:12px;border-top:1px solid var(--bd);color:var(--mut);font-size:12px}}
footer{{margin-top:44px;padding-top:20px;border-top:1px solid var(--bd);color:var(--mut);font-size:12.5px}}
footer code{{background:var(--card);padding:1.5px 5px;border-radius:4px;border:1px solid var(--bd)}}
</style></head><body><div class="wrap">
<header>
  <h1>现成方案雷达</h1>
  <div class="meta">
    <span>更新：<b>{esc(date)}</b></span>
    <span>垂类：<b>{esc(topics)}</b></span>
    <span>扫描：<b>{meta.get('total_scanned', 0)}</b> 个仓库</span>
    <span>入选：<b>{len(entries)}</b> 条</span>
  </div>
</header>
<div class="rules">
  <h2>这份清单怎么读</h2>
  <ol>
    <li><b>代价</b>不是星标数，是你要付出什么：授权成本、基础设施成本、维护成本。</li>
    <li><b>能拿到什么</b>写的是能力，不是形容词。</li>
    <li><b>证据链</b>每一条都可点开核验，不含推断。</li>
    <li><b>红线</b>是硬约束：AGPL 类网络开源义务、无许可证、已弃用项目，看到就先停。</li>
    <li>分数只用来<b>排序</b>，不用来替代判断。星标高不等于适合你。</li>
  </ol>
</div>
{"".join(cards)}
<footer>
  数据来源：GitHub 公开 API，每日自动重扫。评分逻辑见仓库 <code>radar/score.py</code>，可自行复算。<br>
  本清单不构成投资建议。许可证条款以各项目原文为准。
</footer>
</div></body></html>"""


def render_markdown(entries, meta):
    """渲染当日 markdown 清单——这份是可直接发内容的物料。"""
    date = meta.get("generated_at", "")[:10]
    lines = [
        f"# 现成方案雷达 · {date}",
        "",
        f"> 垂类：{'、'.join(meta.get('topics', [])) or '—'} ｜ 扫描 {meta.get('total_scanned', 0)} 个仓库，入选 {len(entries)} 条",
        "> 每条补齐四个决策维度：代价 / 收益 / 来源 / 红线。",
        "",
    ]
    for i, e in enumerate(entries, 1):
        lines.append(f"## {i}. {e['name']}  `综合分 {e['score']}`")
        lines.append("")
        lines.append(f"**{e['one_liner']}**")
        lines.append("")
        lines.append(f"- 🔗 {e['url']}")
        lines.append(f"- 📊 {e['stars']:,}★ · {e['forks']:,}⑂ · 最后提交 {e['days_since_push']} 天前 · {e.get('language') or '—'}")
        lines.append(f"- ⚖️ 许可证：{TIER_LABEL.get(e['license_tier'], e['license_tier'])} — {e.get('license_note') or ''}")
        lines.append("")
        lines.append("**代价**")
        for c in e["costs"]:
            lines.append(f"- {c}")
        lines.append("")
        lines.append("**能拿到什么**")
        for b in e["benefits"]:
            lines.append(f"- {b}")
        lines.append("")
        lines.append("**证据链**")
        for s in e["sources"]:
            lines.append(f"- {s}")
        lines.append("")
        lines.append("**红线**")
        for r in e["risks"]:
            lines.append(f"- {r}")
        lines.append("")
        lines.append(f"→ **{e['decision']}**")
        lines.append("")
        lines.append("---")
        lines.append("")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/entries.json")
    ap.add_argument("--outdir", default="site")
    ap.add_argument("--reports", default="reports")
    ap.add_argument("--limit", type=int, default=20, help="首页展示条数")
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as f:
        data = json.load(f)
    entries = data.get("entries", [])
    meta = {k: v for k, v in data.items() if k != "entries"}

    os.makedirs(args.outdir, exist_ok=True)
    os.makedirs(args.reports, exist_ok=True)

    with open(os.path.join(args.outdir, "index.html"), "w", encoding="utf-8") as f:
        f.write(render_html(entries[: args.limit], meta))

    # 订阅源：只放结构化字段，供付费 skill 拉取
    feed = {
        "generated_at": meta.get("generated_at"),
        "topics": meta.get("topics"),
        "total_scanned": meta.get("total_scanned"),
        "count": len(entries),
        "entries": entries,
    }
    with open(os.path.join(args.outdir, "feed.json"), "w", encoding="utf-8") as f:
        json.dump(feed, f, ensure_ascii=False, indent=2)

    date = meta.get("generated_at", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    with open(os.path.join(args.reports, f"{date}.md"), "w", encoding="utf-8") as f:
        f.write(render_markdown(entries, meta))

    print(f"[publish] site/index.html（{min(len(entries), args.limit)} 条）+ site/feed.json（{len(entries)} 条）+ reports/{date}.md")


if __name__ == "__main__":
    main()
