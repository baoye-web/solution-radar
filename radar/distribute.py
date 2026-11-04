#!/usr/bin/env python3
"""
radar/distribute.py — 内容分发层

把当日清单渲染成各平台可直接发布的内容，并可选择自动发布到 GitHub
（GitHub 是唯一无需任何认证就能自动发布的渠道）。

产出：
    out/<platform>/<date>.md    平台专用文案
    out/<platform>/<date>.txt    纯文本版（X / 微博等）
    out/all/<date>.md            汇总
    out/<platform>/latest.<ext>  当日内容的固定文件名副本

「latest」副本的作用：日期文件名每天变，发布者（人或自动脚本）需要一个
不随日期变化的固定地址去取当日文案，否则每次取稿都得先猜今天是几号。

设计约束（来自实证教训）：
    · 不做 AI 批量生成的通用清单体——那已被证明是红海（卷云式图文号）
    · 每条内容必须有可核验的具体数字，不能是"如何做 X"这类空话
    · 每条内容带不同的切入角度，避免同质化被判定为铺量
"""

import argparse
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fmt_num(n):
    return f"{n:,}"


def pick_diverse(entries, k):
    """挑 k 条覆盖不同能力类型的，避免内容全是同一类。"""
    seen, out, names = set(), [], set()
    for e in entries:
        key = e["language"] or e["license_tier"]
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        names.add(e["name"])
        if len(out) >= k:
            break
    for e in entries:
        if len(out) >= k:
            break
        if e["name"] not in names:
            out.append(e)
            names.add(e["name"])
    return out


def gen_x(entries, meta, pool=None):
    """X 帖文：单条聚焦一个方案，280 字内，带可点开链接。返回字符串（多帖以分隔线隔开）。"""
    date = meta.get("generated_at", "")[:10]
    out = []
    for e in entries[:3]:
        lic = e["license_commercial"]
        tag = "可商用" if lic == "可闭源商用" else ("需核对" if "核对" in lic else lic)
        out.append(
            f"{e['name']} · {fmt_num(e['stars'])}★\n\n"
            f"{e['one_liner']}\n\n"
            f"许可证：{tag}\n"
            f"{e['decision']}\n\n"
            f"→ {e['url']}"
        )
    return "\n\n---\n\n".join(out)


def gen_zhihu(entries, meta, pool=None):
    """知乎式长文：结构化对比，末尾放完整清单。"""
    date = meta.get("generated_at", "")[:10]
    lines = [
        f"# {date} 现成方案雷达：{len(entries)} 个方案的实际授权边界",
        "",
        "每天扫一遍 GitHub，把每个方案的**代价、能拿到什么、证据在哪、有什么红线**列清楚。",
        "今天挑这 6 个说：",
        "",
    ]
    for i, e in enumerate(entries[:6], 1):
        lines.append(f"## {i}. {e['name']}（{fmt_num(e['stars'])}★）")
        lines.append("")
        lines.append(f"{e['one_liner']}")
        lines.append("")
        lines.append(f"- **授权边界**：{e['license_commercial']} — {e['license_note']}")
        lines.append(f"- **主要代价**：{e['costs'][0] if e['costs'] else '—'}")
        lines.append(f"- **能拿到什么**：{e['benefits'][0] if e['benefits'] else '—'}")
        if len(e["risks"]) > 1:
            lines.append(f"- **注意**：{e['risks'][1]}")
        lines.append(f"- **结论**：{e['decision']}")
        lines.append(f"- 链接：{e['url']}")
        lines.append("")
    lines.append("---")
    lines.append("")
    lines.append("完整清单（含全部四个维度）：https://baoye-web.github.io/solution-radar/")
    lines.append("")
    lines.append("授权分档速查：https://github.com/baoye-web/solution-radar")
    return "\n".join(lines)


def gen_xiaohongshu(entries, meta, pool=None):
    """小红书式：短标题 + 分点 + 避坑提醒。

    对比段（⚠️ 要注意 / ✅ 放心用）取自**全量排名池** pool，而不是只取
    被挑出来讲的那几条：这两个段落的作用是给读者「授权边界」对照，
    必须覆盖到最值得警示的那类项目。若只看精选条目，宽松档会把分数
    拉高、红线项目全被挤掉，这段文案就只剩好话，失去避坑价值。
    """
    date = meta.get("generated_at", "")[:10]
    pool = pool if pool is not None else entries
    lines = [
        f"选型踩坑｜{date} 一次说清开源方案的授权边界",
        "",
        f"扫了 {meta.get('total_scanned', 0)} 个仓库，挑 {len(entries)} 个说透。",
        "很多人看 star 选型，其实最容易翻车的是授权。",
        "",
    ]
    # 按 star 降序取：授权陷阱的危害与「有多少人会踩进去」成正比
    risky = sorted(
        (e for e in pool if e["license_tier"] in ("copyleft-strong", "source-available", "custom", "other", "none")),
        key=lambda e: e["stars"], reverse=True,
    )[:3]
    if risky:
        lines.append("⚠️ 这几个特别注意：")
        for e in risky:
            lines.append(f"· {e['name']} → {e['license_commercial']}")
        lines.append("")
    ok = sorted((e for e in pool if e["license_tier"] == "permissive"),
                key=lambda e: e["stars"], reverse=True)[:3]
    if ok:
        lines.append("✅ 这几个可以放心用：")
        for e in ok:
            lines.append(f"· {e['name']}（{fmt_num(e['stars'])}★）")
        lines.append("")
    lines.append("完整清单（含代价/收益/证据/红线四栏）评论区自取")
    lines.append("https://baoye-web.github.io/solution-radar/")
    return "\n".join(lines)


def gen_weibo(entries, meta, pool=None):
    date = meta.get("generated_at", "")[:10]
    top = entries[0]
    return (
        f"{date} 今日开源方案雷达（{len(entries)} 条）\n\n"
        f"最值得看：{top['name']}（{fmt_num(top['stars'])}★）\n"
        f"{top['one_liner']}\n"
        f"授权：{top['license_commercial']}\n"
        f"→ {top['url']}\n\n"
        f"全清单：https://baoye-web.github.io/solution-radar/"
    )


PLATFORMS = {
    "x": (gen_x, "txt"),
    "zhihu": (gen_zhihu, "md"),
    "xiaohongshu": (gen_xiaohongshu, "md"),
    "weibo": (gen_weibo, "txt"),
}


PLATFORM_LABEL = {
    "x": "X 短帖（3 条，单方案深挖）",
    "zhihu": "知乎长文（授权边界对比 + 完整清单）",
    "xiaohongshu": "小红书避坑向短文（别用这些 / 放心用这些）",
    "weibo": "微博单条（当日最值得看的一个）",
}


def gen_index(date, meta, platforms, picked_count):
    """分发文案索引页。样式与站点首页保持一致（同一套 CSS 变量）。"""
    rows = []
    for p in [x.strip() for x in platforms.split(",")]:
        if p not in PLATFORMS:
            continue
        _, ext = PLATFORMS[p]
        rows.append(
            f'<li><a href="{p}/latest.{ext}">{esc(PLATFORM_LABEL.get(p, p))}</a>'
            f'<span class="meta">— <code>{p}/latest.{ext}</code></span></li>'
        )
    topics = "、".join(meta.get("topics", [])) or "—"
    return f"""<!doctype html>
<html lang="zh-CN"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>分发文案 · 现成方案雷达 · {esc(date)}</title>
<meta name="description" content="现成方案雷达每日生成的四平台分发文案，可直接取用。">
<style>
:root{{--bg:#fbfbfa;--fg:#1a1a19;--mut:#6b6b68;--bd:#e3e2de;--ok:#1a7f4b;--card:#fff;--shadow:0 1px 2px rgba(0,0,0,.04)}}
@media (prefers-color-scheme:dark){{:root{{--bg:#161615;--fg:#e8e7e3;--mut:#98978f;--bd:#2e2e2b;--ok:#5cc98d;--card:#1d1d1b;--shadow:none}}}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--fg);font:15px/1.7 -apple-system,BlinkMacSystemFont,"Segoe UI","PingFang SC","Microsoft Yahei",sans-serif}}
.wrap{{max-width:760px;margin:0 auto;padding:40px 20px 80px}}
header{{border-bottom:1px solid var(--bd);padding-bottom:24px;margin-bottom:28px}}
h1{{margin:0 0 8px;font-size:24px;letter-spacing:-.02em}}
.meta{{color:var(--mut);font-size:13px;display:flex;gap:18px;flex-wrap:wrap}}
.meta b{{color:var(--fg);font-weight:600}}
.rules{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:16px 20px;margin:0 0 28px;font-size:13.5px;box-shadow:var(--shadow)}}
.rules h2{{font-size:14px;margin:0 0 8px}}
.rules ol{{margin:0;padding-left:20px;color:var(--mut)}}
.rules li{{margin:3px 0}}
.rules b{{color:var(--fg)}}
ul.list{{list-style:none;padding:0;margin:0}}
ul.list li{{background:var(--card);border:1px solid var(--bd);border-radius:10px;padding:14px 18px;margin-bottom:10px;box-shadow:var(--shadow)}}
ul.list a{{color:var(--fg);font-weight:600;text-decoration:none;border-bottom:1px solid transparent}}
ul.list a:hover{{border-bottom-color:var(--fg)}}
ul.list .meta{{margin-top:4px;font-size:12.5px}}
code{{font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;color:var(--mut)}}
footer{{margin-top:32px;color:var(--mut);font-size:12.5px}}
</style></head>
<body><div class="wrap">
<header>
  <h1>分发文案 · {esc(date)}</h1>
  <div class="meta">
    <span>扫了 <b>{meta.get('total_scanned', 0)}</b> 个仓库</span>
    <span>精选 <b>{picked_count}</b> 条</span>
    <span>垂类 <b>{esc(topics)}</b></span>
  </div>
</header>
<div class="rules">
  <h2>取稿说明</h2>
  <ol>
    <li>下面每个链接指向<b>当日最新</b>文案，文件名固定（<code>latest.*</code>），每天自动覆盖 —— 自动化取稿直接用这些地址即可。</li>
    <li>历史文案保留 <b>14 天</b>，按日期文件名访问（如 <code>{esc(date)}.md</code>）。</li>
    <li>文案里的项目、数据、许可证结论都来自当日实采，<b>每条数字都可点开核验</b>。</li>
  </ol>
</div>
<ul class="list">
{chr(10).join(rows)}
</ul>
<footer>完整清单（含代价 / 收益 / 来源 / 红线四栏）：<a href="../">https://baoye-web.github.io/solution-radar/</a></footer>
</div></body></html>
"""


def esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def prune_old(out_dir, keep_days):
    """删掉过期的日期文件，只保留最近 keep_days 天的历史 + latest 副本。

    分发目录会被部署到公开站点，每天一个日期文件会无限增长。
    历史文案的价值随时间快速衰减（昨天的清单今天就没用了），
    因此只保留一个短窗口。
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=keep_days)).strftime("%Y-%m-%d")
    removed = 0
    # all/ 汇总目录与各平台目录同样按日期堆文件，必须一起清理
    for p in list(PLATFORMS) + ["all"]:
        d = os.path.join(out_dir, p)
        if not os.path.isdir(d):
            continue
        for fn in os.listdir(d):
            if fn.startswith("latest."):
                continue
            stem = fn.rsplit(".", 1)[0]
            # 只处理 YYYY-MM-DD.ext 形态的文件，其他一律不动
            if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", stem):
                continue
            if stem < cutoff:
                os.remove(os.path.join(d, fn))
                removed += 1
    return removed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/entries.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--pick", type=int, default=6, help="挑选条数（挑不同能力类型）")
    ap.add_argument("--platforms", default="x,zhihu,xiaohongshu,weibo")
    ap.add_argument("--keep-days", type=int, default=14,
                    help="保留多少天的历史文案（日期文件名），0 表示不清理")
    args = ap.parse_args()

    data = load(args.inp)
    entries = data.get("entries", [])
    meta = {k: v for k, v in data.items() if k != "entries"}
    picked = pick_diverse(entries, args.pick)
    date = meta.get("generated_at", "")[:10] or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    written = []
    for p in args.platforms.split(","):
        p = p.strip()
        if p not in PLATFORMS:
            continue
        fn, ext = PLATFORMS[p]
        content = fn(picked, meta, entries)
        d = os.path.join(args.out, p)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{date}.{ext}")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(fp)
        # 固定文件名副本：内容与当日文件完全一致，供自动化取稿用固定 URL
        latest = os.path.join(d, f"latest.{ext}")
        with open(latest, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(latest)

    # 汇总
    os.makedirs(os.path.join(args.out, "all"), exist_ok=True)
    allp = os.path.join(args.out, "all", f"{date}.md")
    with open(allp, "w", encoding="utf-8") as f:
        f.write(f"# {date} 分发包\n\n")
        for p in args.platforms.split(","):
            p = p.strip()
            if p not in PLATFORMS:
                continue
            fn, ext = PLATFORMS[p]
            f.write(f"\n## {p}\n\n```\n{fn(picked, meta, entries)}\n```\n\n")
    written.append(allp)
    all_latest = os.path.join(args.out, "all", "latest.md")
    with open(all_latest, "w", encoding="utf-8") as f:
        f.write(f"# {date} 分发包\n\n")
        for p in args.platforms.split(","):
            p = p.strip()
            if p not in PLATFORMS:
                continue
            fn, ext = PLATFORMS[p]
            f.write(f"\n## {p}\n\n```\n{fn(picked, meta, entries)}\n```\n\n")
    written.append(all_latest)

    if args.keep_days > 0:
        removed = prune_old(args.out, args.keep_days)
        if removed:
            print(f"[distribute] 清理 {removed} 个过期文案文件（保留 {args.keep_days} 天）")

    # 索引页：让人能直接打开看到当日可发文案，不必猜 URL
    idx = os.path.join(args.out, "index.html")
    with open(idx, "w", encoding="utf-8") as f:
        f.write(gen_index(date, meta, args.platforms, len(picked)))
    written.append(idx)

    for w in written:
        print(f"[distribute] {w}")


if __name__ == "__main__":
    main()
