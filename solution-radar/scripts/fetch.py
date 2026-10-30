#!/usr/bin/env python3
"""
radar/fetch.py — 现成方案雷达 · 数据采集层

从 GitHub 公开 API 拉取指定垂类的候选项目，抽取决策所需的原始字段。
只用标准库，无需 pip install（GitHub Actions 里可直接跑）。

用法:
    python3 radar/fetch.py --topics "ai-agent,mcp-server" --min-stars 200 --out data/raw.json
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"
UA = "solution-radar/1.0 (+https://github.com/baoye-web)"

# 许可证分类：决定能否商用，这是"决策清单"里最关键的一列
LICENSE_TIERS = {
    "mit": ("宽松", "可闭源商用", "MIT/Apache-2.0/BSD 类，商用无强制开源义务"),
    "apache-2.0": ("宽松", "可闭源商用", "含专利授权，最被企业接受"),
    "bsd-3-clause": ("宽松", "可闭源商用", "BSD 三条款"),
    "bsd-2-clause": ("宽松", "可闭源商用", "BSD 两条款"),
    "isc": ("宽松", "可闭源商用", "ISC"),
    "unlicense": ("宽松", "可闭源商用", "公共领域"),
    "cc0-1.0": ("宽松", "可闭源商用", "CC0，放弃著作权"),
    "agpl-3.0": ("copyleft-strong", "网络服务须开源", "AGPL：提供网络服务即触发开源义务，云厂商白嫖需买商业许可"),
    "gpl-3.0": ("copyleft-strong", "分发须开源", "GPL：分发衍生作品须开源"),
    "gpl-2.0": ("copyleft-strong", "分发须开源", "GPL v2"),
    "lgpl-3.0": ("copyleft-weak", "动态链接可闭源", "LGPL：弱copyleft"),
    "lgpl-2.1": ("copyleft-weak", "动态链接可闭源", "LGPL 2.1"),
    "mpl-2.0": ("copyleft-weak", "文件级copyleft", "MPL 2.0"),
    "bsl-1.0": ("source-available", "商用需授权", "BSL：100人以上组织或商用需单独购买授权"),
    "busl-1.1": ("source-available", "商用需授权", "BUSL：生产环境使用需授权"),
    "sspl-1.0": ("source-available", "商用受限", "SSPL：服务化即视为分发"),
    "elastic-2.0": ("source-available", "商用受限", "Elastic License 2.0"),
    "other": ("unknown", "需自行核对", "未识别或自定义许可证"),
}

# GitHub 用 spdx_id = "noassertion" 表示「自定义许可证」（非 SPDX 标准）。
# 这些是最需要人工核对的一档——恰恰是 n8n / dify 这类大项目。
# 已知的高频自定义许可证，按仓库名精确匹配（避免误伤同名 fork）。
CUSTOM_LICENSES = {
    "n8n-io/n8n": ("source-available", "商用受限", "Sustainable Use License：可自用可商用，但不得用于对外服务/转售宿主形态"),
    "langgenius/dify": ("source-available", "商用受限", "Dify Open Source License：基于 Apache 2.0，附加多租户限制"),
    "n8n-io/design-system": ("permissive", "可闭源商用", "MIT"),
}


def http_json(url, token=None, retries=3):
    """带退避重试的 JSON GET。GitHub 无 token 时限流 60/h，失败会返回 403。"""
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=25) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (403, 429) and attempt < retries - 1:
                wait = 20 * (attempt + 1)
                print(f"  [rate-limit] {e.code} → 等待 {wait}s", file=sys.stderr)
                time.sleep(wait)
                continue
            if e.code == 404:
                return None
            print(f"  [http {e.code}] {url[:90]}", file=sys.stderr)
            return None
        except Exception as e:  # noqa: BLE001
            print(f"  [err] {str(e)[:80]}", file=sys.stderr)
            if attempt < retries - 1:
                time.sleep(3)
                continue
            return None
    return None


def search_repos(query, token, per_page=30, pages=2, sort="stars"):
    """分页搜索仓库。"""
    out = []
    for p in range(1, pages + 1):
        q = urllib.parse.urlencode({
            "q": query,
            "sort": sort,
            "order": "desc",
            "per_page": per_page,
            "page": p,
        })
        data = http_json(f"{API}/search/repositories?{q}", token)
        if not data:
            break
        items = data.get("items") or []
        out.extend(items)
        if len(items) < per_page:
            break
        time.sleep(2.0 if token else 7.0)
    return out


def repo_detail(full_name, token):
    """取单个仓库详情（star/fork/watch 数需要单独接口）。"""
    return http_json(f"{API}/repos/{full_name}", token)


def classify_license(spdx_id, license_obj, full_name=None):
    """把 SPDX 标识映射到决策用的三档分类。

    优先级：已知自定义许可证 → 标准 SPDX → 名称模糊匹配 → 未知。
    "noassertion" 表示 GitHub 判定为非标准许可证，必须显式标注「需人工核对」。
    """
    lic_obj = license_obj or {}
    sid = (spdx_id or "").lower()
    name = (lic_obj.get("name") or "").lower()

    if full_name and full_name in CUSTOM_LICENSES:
        tier, commercial, note = CUSTOM_LICENSES[full_name]
        return {"tier": tier, "commercial": commercial, "note": note,
                "spdx": sid or name, "needs_manual_review": False}

    if sid == "noassertion":
        return {"tier": "custom", "commercial": "需人工核对", "spdx": sid or name,
                "note": f"GitHub 标记为非标准许可证（{lic_obj.get('name') or '未命名'}），商用条款必须逐条读原文",
                "needs_manual_review": True}

    for key, val in LICENSE_TIERS.items():
        if sid == key or (key in name and len(key) > 4):
            return {"tier": val[0], "commercial": val[1], "note": val[2],
                    "spdx": sid or name, "needs_manual_review": False}

    if not sid:
        return {"tier": "none", "commercial": "无许可证=保留所有权利", "spdx": "",
                "note": "仓库未声明许可证，默认不可商用（著作权默认保留）",
                "needs_manual_review": True}

    return {"tier": "other", "commercial": "需自行核对", "spdx": sid,
            "note": f"未识别许可证：{sid}", "needs_manual_review": True}


def fetch_one(repo, token):
    """单个仓库的完整抽取。"""
    full = repo.get("full_name")
    lic_obj = repo.get("license") or {}   # 无许可证仓库该字段为 None
    lic = classify_license(lic_obj.get("spdx_id"), lic_obj, full)
    pushed = repo.get("pushed_at") or ""
    created = repo.get("created_at") or ""
    days_since_push = 0
    if pushed:
        try:
            from datetime import datetime, timezone
            dt = datetime.fromisoformat(pushed.replace("Z", "+00:00"))
            days_since_push = (datetime.now(timezone.utc) - dt).days
        except Exception:  # noqa: BLE001
            days_since_push = 9999

    # 维护信号：近 90 天有提交 = 活；无 release = 可能只是玩具
    has_releases = False
    rel = http_json(f"{API}/repos/{full}/releases?per_page=1", token)
    if rel:
        has_releases = True

    topics = repo.get("topics") or []
    return {
        "full_name": full,
        "html_url": repo.get("html_url"),
        "description": (repo.get("description") or "").strip(),
        "stars": repo.get("stargazers_count", 0),
        "forks": repo.get("forks_count", 0),
        "open_issues": repo.get("open_issues_count", 0),
        "watchers": repo.get("watchers_count", 0),
        "language": repo.get("language"),
        "topics": topics,
        "license": lic,
        "created_at": created,
        "pushed_at": pushed,
        "days_since_push": days_since_push,
        "has_releases": has_releases,
        "archived": repo.get("archived", False),
        "homepage": repo.get("homepage"),
        "owner_type": (repo.get("owner") or {}).get("type"),
    }


def build_queries(topics, min_stars, extra):
    """按主题生成检索式。多主题分别检索再合并，避免一个宽泛 query 淹掉结果。"""
    qs = []
    for t in topics:
        qs.append(f"topic:{t} stars:>={min_stars}")
        qs.append(f"{t} in:name,description stars:>={min_stars}")
    if extra:
        qs.append(extra)
    return qs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topics", required=True, help="逗号分隔的 topic 列表")
    ap.add_argument("--min-stars", type=int, default=200)
    ap.add_argument("--pages", type=int, default=2)
    ap.add_argument("--per-page", type=int, default=30)
    ap.add_argument("--extra", default="")
    ap.add_argument("--out", default="data/raw.json")
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    topics = [t.strip() for t in args.topics.split(",") if t.strip()]
    queries = build_queries(topics, args.min_stars, args.extra)

    seen = {}
    for q in queries:
        print(f"[fetch] {q}", file=sys.stderr)
        for repo in search_repos(q, token, per_page=args.per_page, pages=args.pages):
            fn = repo.get("full_name")
            if not fn or fn in seen:
                continue
            seen[fn] = fetch_one(repo, token)

    rows = list(seen.values())
    # 排序：星标为主，活跃度与许可证友好度为辅
    rows.sort(key=lambda r: (r["stars"], -r["days_since_push"]), reverse=True)
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump({"generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                   "topics": topics, "count": len(rows), "repos": rows}, f,
                  ensure_ascii=False, indent=2)
    print(f"[fetch] 落盘 {len(rows)} 条 → {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
