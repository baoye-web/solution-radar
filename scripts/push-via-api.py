#!/usr/bin/env python3
"""push-via-api.py — 用 GitHub REST API 提交文件，绕过 git over HTTPS 的代理问题。

用法:
    python3 scripts/push-via-api.py --repo baoye-web/solution-radar --dir . --message "..."
"""
import argparse
import base64
import fnmatch
import json
import os
import subprocess
import sys

# .gitignore 里的排除项。推 API 时不会读 .gitignore，必须显式排除，
# 否则 out/ 、*.zip 之类的构建产物会被提交进公开仓库。
GITIGNORE_PATTERNS = [
    "data/raw.json",
    "*.zip",
    "__pycache__/",
    "*.pyc",
    ".DS_Store",
    "probe_cwd.mjs",
    "out/",
]


def ignored(rel_path):
    """rel_path 形如 'out/all/x.md' 或 'data/raw.json'。"""
    for pat in GITIGNORE_PATTERNS:
        if pat.endswith("/"):
            if rel_path == pat[:-1] or rel_path.startswith(pat):
                return True
        elif fnmatch.fnmatch(os.path.basename(rel_path), pat):
            return True
        elif rel_path == pat:
            return True
    return False


def gh(*args, input_json=None):
    cmd = ["gh", "api"] + list(args)
    if input_json is not None:
        cmd += ["--input", "-"]
    r = subprocess.run(cmd, input=input_json, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"gh api failed: {r.stderr.strip()[:400]}")
    out = r.stdout.strip()
    return json.loads(out) if out and out[0] in "[{" else out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", required=True)
    ap.add_argument("--dir", default=".")
    ap.add_argument("--message", required=True)
    ap.add_argument("--branch", default="main")
    args = ap.parse_args()

    # 1. 取当前 ref
    ref = gh(f"repos/{args.repo}/git/ref/heads/{args.branch}")
    base_sha = ref["object"]["sha"]
    base_commit = gh(f"repos/{args.repo}/git/commits/{base_sha}")
    base_tree = base_commit["tree"]["sha"]

    # 2. 收集要提交的文件
    files = []
    root = os.path.abspath(args.dir)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in (".git", "__pycache__", "node_modules")]
        for fn in filenames:
            if fn.endswith((".pyc", ".zip")) or fn == ".DS_Store":
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            if ignored(rel):
                continue
            if rel.startswith("data/raw"):
                continue
            with open(full, "rb") as f:
                content = f.read()
            # 保留可执行位（不限于 .py —— .sh 也需要）
            mode = "100755" if os.access(full, os.X_OK) else "100644"
            files.append({
                "path": rel,
                "mode": mode,
                "type": "blob",
                "content": base64.b64encode(content).decode(),
            })

    if not files:
        print("没有需要提交的文件")
        return

    # 2.5 清理远端已存在但本地产物已删除 / 已被 gitignore 的文件。
    #     建 tree 时传 sha=null 表示删除该路径。
    remote_paths = gh(f"repos/{args.repo}/git/trees/{base_tree}?recursive=1")
    keep = {f["path"] for f in files}
    deletes = []
    for entry in remote_paths.get("tree", []):
        if entry.get("type") != "blob":
            continue
        p = entry["path"]
        if p in keep:
            continue
        # 本地已不存在 → 删除；或属于 gitignore 产物 → 一并清理
        if not os.path.exists(os.path.join(root, p)) or ignored(p):
            deletes.append(p)
    if deletes:
        print(f"清理远端 {len(deletes)} 个文件：{', '.join(deletes[:8])}"
              f"{'...' if len(deletes) > 8 else ''}")

    # 3. 建 blob
    blobs = []
    for f in files:
        b = gh(f"repos/{args.repo}/git/blobs", input_json=json.dumps({
            "content": f["content"], "encoding": "base64"}))
        f["sha"] = b["sha"]
        blobs.append(f)

    # 4. 建 tree
    tree_entries = [
        {"path": f["path"], "mode": f["mode"], "type": "blob", "sha": f["sha"]}
        for f in blobs
    ]
    # sha=null 表示从 base_tree 中删除该路径
    tree_entries += [{"path": p, "mode": "100644", "type": "blob", "sha": None}
                     for p in deletes]
    tree = gh(f"repos/{args.repo}/git/trees", input_json=json.dumps({
        "base_tree": base_tree, "tree": tree_entries}))
    tree_sha = tree["sha"]

    # 5. 建 commit
    commit = gh(f"repos/{args.repo}/git/commits", input_json=json.dumps({
        "message": args.message, "tree": tree_sha, "parents": [base_sha]}))
    commit_sha = commit["sha"]

    # 6. 更新 ref
    gh(f"repos/{args.repo}/git/refs/heads/{args.branch}", input_json=json.dumps({
        "sha": commit_sha, "force": False}))

    print(f"已提交 {len(files)} 个文件 → commit {commit_sha[:8]}")
    print(f"https://github.com/{args.repo}/commit/{commit_sha}")


if __name__ == "__main__":
    main()
