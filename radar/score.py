#!/usr/bin/env python3
"""
radar/score.py — 现成方案雷达 · 评分与结构化层

把 fetch.py 的原始数据转成「能直接做决定」的决策清单。
每个方案补齐四个决策必填维度：
    成本（用什么代价换） / 收益（能拿到什么） / 来源（在哪、证据是什么） / 风险（不能忽视的红线）

评分不是玄学权重，每一档都对应一个可验证的事实信号。
用法:
    python3 radar/score.py --in data/raw.json --out data/entries.json --limit 40
"""

import argparse
import json
import math
import os
import re
import time

# ---- 维度一：成本。用什么代价换这个东西 ----
COST_SIGNALS = [
    # (正则, 成本标签, 成本描述模板)
    (r"\b(api|self[- ]host|hosted|cloud)\b", "需自建/托管", "需要自己部署或购买云服务，有持续运维成本"),
    (r"\b(gpu|cuda|training|inference|gpu-required)\b", "需GPU", "需要 GPU 资源，成本随推理量线性上升"),
    (r"\b(enterprise|commercial[- ]use|pro|self[- ]hosted[- ]enterprise)\b", "企业版收费", "企业版需单独付费"),
    (r"\b(license[- ]key|paid[- ]plan|subscription)\b", "订阅制", "需订阅或购买授权"),
]

# ---- 维度二：收益。能拿到什么 ----
VALUE_SIGNALS = [
    (r"\b(rag|retrieval|vector|embedding)\b", "检索增强", "解决知识注入与检索问题"),
    (r"\b(agent|autonomous|workflow|orchestrat)\b", "Agent编排", "可支撑自主任务执行与编排"),
    (r"\b(mcp|model[- ]context[- ]protocol)\b", "MCP协议", "可作为 MCP 服务接入任意客户端"),
    (r"\b(observability|tracing|eval|monitor)\b", "可观测/评测", "提供调用链追踪与效果评测能力"),
    (r"\b(scrap|crawler|extract|parser|crawl)\b", "数据抓取", "可承担网页/接口数据采集"),
    (r"\b(sdk|api|client)\b", "有SDK/API", "提供程序化调用接口，可嵌入自有系统"),
    (r"\b(local|offline|on[- ]device|privacy)\b", "可本地部署", "数据不出本地，隐私合规成本低"),
    (r"\b(community|discord|slack)\b", "社区活跃", "有社区支持，问题可被快速响应"),
]

# ---- 维度四：风险。红线 ----
RISK_SIGNALS = [
    (r"\b(beta|experimental|alpha|preview)\b", "尚在早期", "项目未到稳定期，API 可能随时变更"),
    (r"\b(archived|deprecated|unmaintained)\b", "已弃用", "项目已停止维护"),
]


def match_signals(text, table):
    """返回命中的 (标签, 描述) 列表。"""
    hits = []
    for pat, label, desc in table:
        if re.search(pat, text, re.I):
            hits.append((label, desc))
    return hits


def repo_text(repo):
    """把一个仓库拼成可检索的文本。"""
    parts = [
        repo.get("description") or "",
        repo.get("full_name") or "",
        " ".join(repo.get("topics") or []),
        (repo.get("license") or {}).get("note") or "",
        repo.get("language") or "",
    ]
    return " ".join(parts)


def score_repo(repo):
    """给单个仓库打分并补齐决策字段。返回 (score, entry)。"""
    text = repo_text(repo)
    stars = repo.get("stars", 0) or 0
    days = repo.get("days_since_push", 9999)
    lic = repo.get("license") or {}

    # --- 基础分：星标（对数压缩 + 封顶，避免头部项目单项碾压其他维度） ---
    base = min(math.log10(stars + 1) * 8, 40)  # 1k星≈24, 100k星≈40（封顶）

    # --- 活跃度：近 30 天有提交 +12；90 天 +6；365 天 -4；更久 -14 ---
    activity = 0
    if days <= 30:
        activity += 12
    elif days <= 90:
        activity += 6
    elif days <= 365:
        activity -= 4
    else:
        activity -= 14

    # --- 成熟度：有 release +6（说明有发布纪律） ---
    maturity = 6 if repo.get("has_releases") else 0

    # --- 许可证友好度：宽松 +12 / 弱copyleft +2 / 强copyleft -10 / 需授权 -6 / 需核对 -14 ---
    tier = lic.get("tier")
    lic_score = {"permissive": 12, "copyleft-weak": 2, "copyleft-strong": -10,
                 "source-available": -6, "custom": -14, "none": -16, "other": -12}.get(tier, -12)

    # --- 决策价值：值类型越多，泛用性越高 ---
    value_hits = match_signals(text, VALUE_SIGNALS)
    value_score = min(len(value_hits) * 6, 24)

    # --- 风险扣分 ---
    risk_hits = match_signals(text, RISK_SIGNALS)
    risk_score = -len(risk_hits) * 10
    if repo.get("archived"):
        risk_score -= 18

    # --- 社区健康：fork/star 比例高说明有真实用户在改 ---
    forks = repo.get("forks", 0) or 0
    fork_ratio = forks / stars if stars else 0
    community = 6 if fork_ratio > 0.15 else (3 if fork_ratio > 0.06 else 0)

    total = base + activity + maturity + lic_score + value_score + risk_score + community
    total = max(0, min(100, round(total)))

    # ---- 成本维度：许可证 + 部署形态 + 规模要求 ----
    costs = []
    if lic.get("commercial") and lic["commercial"] != "可闭源商用":
        costs.append(f"许可证：{lic['commercial']}（{lic.get('spdx') or '自定义'}）")
    cost_hits = match_signals(text, COST_SIGNALS)
    for label, desc in cost_hits:
        costs.append(f"{label}：{desc}")
    if stars >= 50000:
        costs.append("规模：万人级项目，文档与社区资源丰富，但也意味着定制成本高")
    if not repo.get("has_releases"):
        costs.append("工程：无正式 release，升级需自行评估破坏性变更")
    if not costs:
        costs.append("许可证：宽松，可闭源商用；无明显基础设施依赖")

    # ---- 收益维度 ----
    benefits = [d for _, d in value_hits] or ["通用工具，需自行评估与具体场景的契合度"]

    # ---- 来源维度：证据链必须可核 ----
    sources = [
        f"仓库：{repo['full_name']}",
        f"Star {stars:,} / Fork {forks:,}",
        f"最近提交：{repo.get('pushed_at', '未知')[:10]}（{days} 天前）",
    ]
    if repo.get("language"):
        sources.append(f"主语言：{repo['language']}")
    if repo.get("has_releases"):
        sources.append("有正式 release，说明有发布纪律")
    if repo.get("topics"):
        sources.append("topics：" + "、".join(repo["topics"][:6]))

    # ---- 风险维度 ----
    risks = []
    for label, desc in risk_hits:
        risks.append(f"{label}：{desc}")
    if lic.get("needs_manual_review"):
        risks.append(f"许可证需人工核对：{lic.get('note')}")
    if lic.get("tier") == "copyleft-strong":
        risks.append("强 copyleft：若做 SaaS 对外提供服务，须开源或购买商业许可")
    if not risks:
        risks.append("未发现明显红线，但依赖评估仍需自行验证")

    entry = {
        "name": repo["full_name"],
        "url": repo.get("html_url"),
        "one_liner": (repo.get("description") or "无描述")[:120],
        "score": total,
        "stars": stars,
        "forks": forks,
        "days_since_push": days,
        "language": repo.get("language"),
        "license_tier": tier,
        "license_commercial": lic.get("commercial"),
        "license_note": lic.get("note"),
        "costs": costs,
        "benefits": benefits,
        "sources": sources,
        "risks": risks,
        "decision": decide(total, tier, lic, risks),
    }
    return total, entry


def decide(score, lic_tier, lic, risks):
    """给出一句话结论。门槛按前面实证的「后果可验证」原则设定。

    硬红线（无论多高分都降级）：无许可证、强 copyleft 且要做服务化、已弃用。
    """
    if lic_tier == "none":
        return "不推荐：无许可证，默认不可商用"
    if lic_tier == "copyleft-strong":
        if score >= 70:
            return "有条件推荐：能力强，但对外提供服务前必须先解决授权（AGPL 类网络开源义务）"
        return "不推荐：强 copyleft，商用路径不清晰"
    if any("已弃用" in r for r in risks):
        return "不推荐：项目已停止维护"
    if lic_tier in ("custom", "other"):
        return f"待核对：许可证为自定义条款（{lic.get('spdx') or '未知'}），需读原文确认商用边界"
    if lic_tier == "source-available":
        return "谨慎使用：许可证允许自用但限制对外服务/转售，商用前需确认条款边界"
    if score >= 75:
        return "推荐：可直接进入评估"
    if score >= 62:
        return "可考虑：先验证一个具体场景能否跑通"
    if score >= 48:
        return "观望：作为备选，先看同赛道更高分的方案"
    return "不推荐：综合信号偏弱"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in", dest="inp", default="data/raw.json")
    ap.add_argument("--out", default="data/entries.json")
    ap.add_argument("--limit", type=int, default=40)
    args = ap.parse_args()

    with open(args.inp, encoding="utf-8") as f:
        raw = json.load(f)

    entries = []
    for repo in raw.get("repos", []):
        _, e = score_repo(repo)
        entries.append(e)

    entries.sort(key=lambda e: e["score"], reverse=True)
    entries = entries[: args.limit]

    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({
            "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "topics": raw.get("topics", []),
            "total_scanned": raw.get("count", 0),
            "entries": entries,
        }, f, ensure_ascii=False, indent=2)

    print(f"[score] 扫描 {raw.get('count')} → 输出 {len(entries)} 条 → {args.out}")
    top = [e for e in entries if e["score"] >= 70]
    print(f"[score] 高分(≥70) {len(top)} 条，需人工核对许可证的 {sum(1 for e in entries if e['license_tier'] in ('custom','none','other'))} 条")


if __name__ == "__main__":
    main()
