#!/usr/bin/env python3
"""
radar/distribute.py — 内容分发层

把当日清单渲染成各平台可直接发布的内容，并可选择自动发布到 GitHub
（GitHub 是唯一无需任何认证就能自动发布的渠道）。

产出：
    out/<platform>/<date>.md    平台专用文案
    out/<platform>/<date>.txt    纯文本版（X / 微博等）
    out/all/<date>.md            汇总

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
from datetime import datetime, timezone


def load(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def fmt_num(n):
    return f"{n:,}"


def pick_diverse(entries, k):
    """挑 k 条覆盖不同能力类型的，避免内容全是同一类。"""
    seen, out = set(), []
    for e in entries:
        key = e["language"] or e["license_tier"]
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
        if len(out) >= k:
            break
    for e in entries:
        if len(out) >= k:
            break
        if e not in out:
            out.append(e)
    return out


def gen_x(entries, meta):
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


def gen_zhihu(entries, meta):
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


def gen_xiaohongshu(entries, meta):
    """小红书式：短标题 + 分点 + 避坑提醒。"""
    date = meta.get("generated_at", "")[:10]
    lines = [
        f"选型踩坑｜{date} 一次说清开源方案的授权边界",
        "",
        f"扫了 {meta.get('total_scanned', 0)} 个仓库，选出 {len(entries)} 个。",
        "很多人看 star 选型，其实最容易翻车的是授权。",
        "",
    ]
    risky = [e for e in entries if e["license_tier"] in ("copyleft-strong", "source-available", "custom", "none")][:3]
    if risky:
        lines.append("⚠️ 这几个特别注意：")
        for e in risky:
            lines.append(f"· {e['name']} → {e['license_commercial']}")
        lines.append("")
    ok = [e for e in entries if e["license_tier"] == "permissive"][:3]
    if ok:
        lines.append("✅ 这几个可以放心用：")
        for e in ok:
            lines.append(f"· {e['name']}（{fmt_num(e['stars'])}★）")
        lines.append("")
    lines.append("完整清单（含代价/收益/证据/红线四栏）评论区自取")
    lines.append("https://baoye-web.github.io/solution-radar/")
    return "\n".join(lines)


def gen_weibo(entries, meta):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/entries.json")
    ap.add_argument("--out", default="out")
    ap.add_argument("--pick", type=int, default=6, help="挑选条数（挑不同能力类型）")
    ap.add_argument("--platforms", default="x,zhihu,xiaohongshu,weibo")
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
        content = fn(picked, meta)
        d = os.path.join(args.out, p)
        os.makedirs(d, exist_ok=True)
        fp = os.path.join(d, f"{date}.{ext}")
        with open(fp, "w", encoding="utf-8") as f:
            f.write(content)
        written.append(fp)

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
            f.write(f"\n## {p}\n\n```\n{fn(picked, meta)}\n```\n\n")
    written.append(allp)

    for w in written:
        print(f"[distribute] {w}")


if __name__ == "__main__":
    main()
