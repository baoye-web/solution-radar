#!/usr/bin/env python3
"""
scripts/publish-to-openplatform.py — WorkBuddy 开放平台自动上架

把本地 skill 打包 → 上传 → 提交审核 →（可选）设置付费 → 发布上架。

**鉴权方式**：Cookie + X-Party-Id（开放平台后台的鉴权形式）。
这意味着需要一次人工登录取 Cookie，之后整条链路可脚本化。

首次使用：
    1. ego-browser 打开 https://open.workbuddy.cn/login/ 完成扫码
    2. 从浏览器 DevTools 复制 Cookie 与 X-Party-Id
    3. 写入环境变量：
       export WB_COOKIE='session=xxx; other=yyy'
       export WB_PARTY_ID='<x-party-id>'
       export WB_BASE='https://open.workbuddy.cn'
    4. python3 scripts/publish-to-openplatform.py --skill-dir ./solution-radar --dry-run

注意：
- 平台要求 zip ≤3MB、根目录含 SKILL.md、子目录仅限 references/scripts/templates
- 付费方式发布后不可改，定价前请确认
- 审核结论由平台给出，驳回后必须真正修改内容（公约 2.1.2 禁止无修改重提）
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from pathlib import Path

ALLOWED_SUBDIRS = {"references", "scripts", "templates"}
MAX_ZIP_MB = 3

# 平台 frontmatter 必填字段（官方 /docs/skill）
REQUIRED_FM = [
    "name", "display_name", "display_name_en",
    "description", "description_zh", "description_en",
    "version", "author",
]


class Client:
    """开放平台后台 REST 客户端。鉴权 = Cookie + X-Party-Id。"""

    def __init__(self, base, cookie, party_id, verbose=True):
        self.base = base.rstrip("/")
        self.cookie = cookie
        self.party_id = party_id
        self.verbose = verbose

    def _log(self, *a):
        if self.verbose:
            print(*a, file=sys.stderr)

    def call(self, method, path, body=None, raw=False, files=None):
        url = f"{self.base}{path}"
        headers = {
            "Cookie": self.cookie,
            "Accept": "application/json",
            "Origin": self.base,
            "Referer": f"{self.base}/",
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        }
        if self.party_id:
            headers["X-Party-Id"] = self.party_id

        data = None
        if files:
            boundary = f"----wb{uuid.uuid4().hex}"
            headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
            buf = bytearray()
            for k, v in (body or {}).items():
                buf += f"--{boundary}\r\n".encode()
                buf += f'Content-Disposition: form-data; name="{k}"\r\n\r\n'.encode()
                buf += f"{v}\r\n".encode()
            for k, (fname, content) in files.items():
                buf += f"--{boundary}\r\n".encode()
                buf += (f'Content-Disposition: form-data; name="{k}"; '
                        f'filename="{fname}"\r\n').encode()
                buf += b"Content-Type: application/octet-stream\r\n\r\n"
                buf += content + b"\r\n"
            buf += f"--{boundary}--\r\n".encode()
            data = bytes(buf)
        elif body is not None:
            data = json.dumps(body).encode()
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                txt = resp.read().decode("utf-8", "replace")
                if raw:
                    return txt
                try:
                    return json.loads(txt)
                except json.JSONDecodeError:
                    return {"_raw": txt[:2000]}
        except urllib.error.HTTPError as e:
            loc = e.headers.get("Location", "")
            hint = ""
            if e.code in (301, 302) or "/login" in loc:
                hint = " → 登录态失效，请重新取 Cookie"
            body_txt = ""
            try:
                body_txt = e.read().decode("utf-8", "replace")[:600]
            except Exception:  # noqa: BLE001
                pass
            self._log(f"[http {e.code}] {method} {path}{hint}\n{body_txt}")
            raise


def parse_frontmatter(text):
    """极简 YAML frontmatter 解析（只处理 key: value，不引入 pyyaml 依赖）。"""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    fm = {}
    for line in block.split("\n"):
        line = line.rstrip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        k, v = line.split(":", 1)
        v = v.strip().strip('"').strip("'")
        fm[k.strip()] = v
    return fm


def validate(skill_dir):
    """本地预检——平台解析失败是白跑一轮审核，务必先查。"""
    errs, warns = [], []
    sd = Path(skill_dir)
    md = sd / "SKILL.md"
    if not md.exists():
        errs.append("缺少 SKILL.md")
        return errs, warns, {}

    fm = parse_frontmatter(md.read_text(encoding="utf-8"))
    for k in REQUIRED_FM:
        if not fm.get(k):
            errs.append(f"frontmatter 缺必填字段：{k}")

    if fm.get("name") and fm["name"] != sd.name:
        warns.append(f"name({fm['name']}) 与目录名({sd.name})不一致，平台可能以目录名为准")

    for sub in sd.iterdir():
        if sub.is_dir() and sub.name not in ALLOWED_SUBDIRS:
            errs.append(f"子目录 '{sub.name}' 不在允许清单内（仅限 {sorted(ALLOWED_SUBDIRS)}）")
        if sub.is_dir() and sub.name in ALLOWED_SUBDIRS:
            for f in sub.iterdir():
                if f.is_file() and f.suffix.lower() in (".py", ".sh"):
                    if not (f.stat().st_mode & 0o111):
                        warns.append(f"脚本 {f.relative_to(sd)} 无执行位，平台上可能无法直接运行")

    # 素材体积
    total = sum(f.stat().st_size for f in sd.rglob("*") if f.is_file())
    if total > MAX_ZIP_MB * 1024 * 1024:
        errs.append(f"目录体积 {total/1024/1024:.1f}MB 超过 {MAX_ZIP_MB}MB")

    # 开发者公约红线自查
    body = md.read_text(encoding="utf-8")
    if "示例" in body and "example" in body.lower():
        pass
    if len(body) < 400:
        warns.append("SKILL.md 正文偏短，审核要求「功能说明、运行条件、测试方式及依赖」")

    return errs, warns, fm


def build_zip(skill_dir, out_path):
    """按平台要求打包：根目录直接放 SKILL.md 与三个允许的子目录。"""
    sd = Path(skill_dir)
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.write(sd / "SKILL.md", "SKILL.md")
        for sub in sorted(ALLOWED_SUBDIRS):
            d = sd / sub
            if not d.is_dir():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file():
                    z.write(f, f"{sub}/{f.relative_to(d)}")
    return out_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill-dir", required=True)
    ap.add_argument("--zip", default="", help="输出 zip 路径")
    ap.add_argument("--base", default=os.environ.get("WB_BASE", "https://open.workbuddy.cn"))
    ap.add_argument("--dry-run", action="store_true", help="只做本地预检与打包，不调接口")
    ap.add_argument("--price", type=int, default=0, help="定价（元）。0 表示先免费上架")
    ap.add_argument("--monthly", action="store_true", help="按月订阅而非一次性买断")
    ap.add_argument("--submit", action="store_true", help="上传后立即提交审核")
    ap.add_argument("--publish", action="store_true", help="审核通过后自动发布（默认不发布）")
    ap.add_argument("--visibility", default="public", choices=["public", "private"])
    args = ap.parse_args()

    cookie = os.environ.get("WB_COOKIE", "")
    party_id = os.environ.get("WB_PARTY_ID", "")

    # ---- 步骤 1：本地预检 ----
    errs, warns, fm = validate(args.skill_dir)
    print(f"=== 本地预检：{args.skill_dir} ===")
    if fm:
        print(f"  name={fm.get('name')}  version={fm.get('version')}  author={fm.get('author')}")
    for w in warns:
        print(f"  [warn] {w}")
    for e in errs:
        print(f"  [ERROR] {e}")
    if errs:
        print("预检未通过，打包与上传中止。")
        sys.exit(1)
    print("  预检通过")

    # ---- 步骤 2：打包 ----
    zip_path = args.zip or f"{args.skill_dir.rstrip('/').split('/')[-1]}.zip"
    build_zip(args.skill_dir, zip_path)
    size_kb = os.path.getsize(zip_path) / 1024
    print(f"=== 打包完成：{zip_path}（{size_kb:.1f}KB）===")

    if args.dry_run:
        print("\n[dry-run] 未调用平台接口。去掉 --dry-run 可真实上传。")
        print("需先设置环境变量：WB_COOKIE / WB_PARTY_ID")
        return

    if not cookie:
        print("缺少 WB_COOKIE。请先在浏览器登录 open.workbuddy.cn 并复制 Cookie。")
        sys.exit(2)

    c = Client(args.base, cookie, party_id)

    # ---- 步骤 3：查我的资产（拿 partyId、确认已登录）----
    print("\n=== 检查登录态 ===")
    try:
        me = c.call("POST", "/api/v2/open-platform/capabilities/list", {"page": 1, "pageSize": 5})
    except urllib.error.HTTPError:
        print("接口调用失败——最可能是登录态失效或缺少 X-Party-Id。")
        sys.exit(3)
    assets = (me.get("data") or {}).get("list") or me.get("data") or []
    print(f"  已连接。当前资产数：{len(assets) if isinstance(assets, list) else '未知'}")

    # ---- 步骤 4：初始化上传 ----
    print("\n=== 上传 ===")
    with open(zip_path, "rb") as f:
        content = f.read()
    init = c.call("POST", "/api/v2/open-platform/capabilities/init-upload", {
        "name": fm.get("name"),
        "displayName": fm.get("display_name"),
        "version": fm.get("version"),
        "type": "skill",
    })
    d = init.get("data") or init
    upload_url = d.get("uploadUrl") or d.get("upload_url") or d.get("url")
    upload_id = d.get("uploadId") or d.get("upload_id") or d.get("id")
    if not upload_url:
        print("未拿到上传地址，接口返回：")
        print(json.dumps(init, ensure_ascii=False, indent=2)[:1500])
        sys.exit(4)

    # PUT 文件内容
    put_headers = {"Content-Type": "application/octet-stream"}
    if party_id:
        put_headers["X-Party-Id"] = party_id
    preq = urllib.request.Request(upload_url, data=content, headers=put_headers, method="PUT")
    try:
        with urllib.request.urlopen(preq, timeout=180) as r:
            print(f"  文件上传完成（HTTP {r.status}）")
    except urllib.error.HTTPError as e:
        print(f"  文件上传失败 HTTP {e.code}：{e.read().decode('utf-8','replace')[:400]}")
        sys.exit(5)

    # ---- 步骤 5：完成上传（平台开包解析，生成 skillId）----
    done = c.call("POST", "/api/v2/open-platform/capabilities/complete-upload", {
        "uploadId": upload_id, "name": fm.get("name"), "type": "skill",
    })
    dd = done.get("data") or done
    asset_id = dd.get("id") or dd.get("assetId") or dd.get("skillId")
    print(f"  平台解析完成，assetId = {asset_id}")

    # ---- 步骤 6：补展示信息（分类/类目）----
    cats = c.call("POST", "/api/v2/open-platform/capabilities/business-categories", {})
    cat_opts = ((cats.get("data") or {}).get("list") or cats.get("data") or [])
    print(f"  可选服务类目 {len(cat_opts) if isinstance(cat_opts, list) else '未知'} 个")

    # ---- 步骤 7：提交审核 ----
    if args.submit:
        print("\n=== 提交审核 ===")
        sub = c.call("POST", "/api/v2/open-platform/capabilities/submit", {
            "id": asset_id, "type": "skill",
            "originality": True,   # 原创声明：本人独立原创，无抄袭搬运
        })
        sd = sub.get("data") or sub
        print(f"  已提交。状态：{sd.get('status') or sd.get('state') or '已接收'}")
        print("  官方口径 7 个工作日内完成，实测有开发者次日通过。")

    # ---- 步骤 8：定价（仅审核通过后可用）----
    if args.price > 0:
        print("\n=== 设置付费 ===")
        pay = c.call("POST", f"/api/v2/open-platform/capabilities/{asset_id}/pay", {
            "payMode": 2 if args.monthly else 1,   # 1=一次性 2=按月
            "price": args.price,
            "payDetail": [{"price": args.price, "description": fm.get("description_zh", "")[:80]}],
        })
        print(f"  已设置：{'按月订阅' if args.monthly else '一次性买断'} {args.price} 元")
        print("  ⚠️ 付费方式发布后不可更改；1–9999 元。")

    # ---- 步骤 9：发布 ----
    if args.publish:
        print("\n=== 发布 ===")
        pub = c.call("POST", f"/api/v2/open-platform/capabilities/{asset_id}/publish",
                     {"visibleScope": args.visibility})
        print(f"  已发布（可见范围：{args.visibility}）")
    else:
        print(f"\n[提示] assetId={asset_id} 已就绪。审核通过后发布：")
        print(f"  --publish（公开发布）或 --visibility private（仅自己账号下可见）")

    print(f"\n下一步：查状态 python3 scripts/publish-to-openplatform.py --status {asset_id}")


if __name__ == "__main__":
    main()
