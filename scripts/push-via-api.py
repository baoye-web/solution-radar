#!/usr/bin/env python3
"""push-via-api.py — 用 GitHub REST API 提交文件，绕过 git over HTTPS 的代理问题。

用法:
    python3 scripts/push-via-api.py --repo baoye-web/solution-radar --dir . --message "..."
"""
import argparse
import base64
import json
import os
import subprocess
import sys


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
            if rel.startswith("data/raw"):
                continue
            with open(full, "rb") as f:
                content = f.read()
            files.append({
                "path": rel,
                "mode": "100755" if os.access(full, os.X_OK) and full.endswith(".py") else "100644",
                "type": "blob",
                "content": base64.b64encode(content).decode(),
            })

    if not files:
        print("没有需要提交的文件")
        return

    # 3. 建 blob
    blobs = []
    for f in files:
        b = gh(f"repos/{args.repo}/git/blobs", input_json=json.dumps({
            "content": f["content"], "encoding": "base64"}))
        f["sha"] = b["sha"]
        blobs.append(f)

    # 4. 建 tree
    tree = gh(f"repos/{args.repo}/git/trees", input_json=json.dumps({
        "base_tree": base_tree, "tree": [
            {"path": f["path"], "mode": f["mode"], "type": "blob", "sha": f["sha"]}
            for f in blobs]}))
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
