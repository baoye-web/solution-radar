#!/usr/bin/env nodejs
/**
 * solution-radar/scripts/publish.mjs — 在浏览器上下文中完成 Skill 上架
 *
 * 为什么必须用 page.fetch 而不是 Python urllib：
 *   实测（2026-10-30）开放平台后台的 session cookie 带 host 绑定与 UA 校验，
 *   在浏览器外用 urllib 直连会被重定向到 Keycloak 登录页（返回 login-pf HTML）。
 *   而 page.fetch 在页面上下文里执行，浏览器自动带上正确凭证，已实测：
 *     GET  /api/v2/open-platform/messages                → 200 {"code":0}
 *     POST /api/v2/open-platform/capabilities/list       → 403 {"code":100003,"msg":"当前用户无权访问该主体"}
 *   403 是「缺 party_id」，不是「未登录」——登录态有效，缺的是主体。
 *
 * 用法：
 *   ego-browser nodejs < solution-radar/scripts/publish.mjs -- --submit
 *   参数：
 *     --submit    上传后立即提交审核
 *     --publish   审核通过则定价并发布（需配合 --price）
 *     --price N   定价（元）
 *     --monthly   按月订阅而非一次性买断
 *     --wait      轮询等待 partyId（实名认证完成后自动继续）
 */

const fs = require("node:fs");
const path = require("node:path");

const args = process.argv.slice(2);
const has = (f) => args.includes(f);
const val = (f, d) => {
  const i = args.indexOf(f);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};

const SUBMIT = has("--submit");
const PUBLISH = has("--publish");
const PRICE = parseInt(val("--price", "0"), 10);
const MONTHLY = has("--monthly");
const WAIT = has("--wait");
const WAIT_TIMEOUT = parseInt(val("--wait-timeout", "3600"), 10) * 1000;
const INTERVAL = parseInt(val("--interval", "30"), 10) * 1000;

const BASE = "https://open.workbuddy.cn";
const V2 = `${BASE}/api/v2/open-platform`;
const V1 = `${BASE}/api/v1/open-platform/charge-developer/merchant`;

// ego-browser nodejs 走 stdin 时：process.cwd() === "/"，__dirname === "."
// 因此必须用绝对路径定位 skill 包。优先读 WB_SKILL_DIR，其次按已知布局推断。
function resolveSkillDir() {
  const home = process.env.HOME || "/Users/happysir";
  const candidates = [
    process.env.WB_SKILL_DIR,
    path.join(home, "ego/auto-income-20261030/agent-radar/solution-radar"),
    path.join(process.cwd(), "Users/happysir/ego/auto-income-20261030/agent-radar/solution-radar"),
    "/Users/happysir/ego/auto-income-20261030/agent-radar/solution-radar",
  ].filter(Boolean);
  for (const c of candidates) {
    if (fs.existsSync(path.join(c, "SKILL.md"))) return c;
  }
  return candidates[0];
}

const SKILL_DIR = resolveSkillDir();
const ZIP_PATH = path.resolve(SKILL_DIR, "..", "solution-radar.zip");

function log(...a) {
  console.log(`[${new Date().toISOString().slice(11, 19)}]`, ...a);
}

// ---- 在页面上下文里调接口 ----
async function api(page, method, path, body) {
  const opts = {
    method,
    headers: { "content-type": "application/json" },
    timeout: 30000,
  };
  if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
  const r = await page.fetch(path.startsWith("http") ? path : path, opts);
  let parsed = r.body;
  if (typeof parsed === "string") {
    try { parsed = JSON.parse(parsed); } catch { /* 保持字符串 */ }
  }
  return { status: r.status, body: parsed };
}

// ---- 从 localStorage 读 partyId ----
async function readPartyId(page) {
  return await page.evaluate(() => {
    let auth = null;
    try { auth = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch {}
    return {
      partyId: auth?.partyId || null,
      certMethod: auth?.certMethod || null,
      loggedIn: localStorage.getItem("loggedIn") === "true",
    };
  });
}

// ---- 本地预检（与 Python 侧规则一致，双保险）----
function validate(dir) {
  const errs = [];
  const mdPath = path.join(dir, "SKILL.md");
  if (!fs.existsSync(mdPath)) {
    errs.push("缺少 SKILL.md");
    return { errs, fm: {} };
  }
  const text = fs.readFileSync(mdPath, "utf8");
  const end = text.indexOf("\n---", 3);
  const fm = {};
  if (text.startsWith("---") && end > 0) {
    for (const line of text.slice(3, end).split("\n")) {
      const m = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
      if (m) fm[m[1]] = m[2].trim().replace(/^["']|["']$/g, "");
    }
  }
  const required = ["name", "display_name", "display_name_en", "description",
    "description_zh", "description_en", "version", "author"];
  for (const k of required) if (!fm[k]) errs.push(`frontmatter 缺必填字段：${k}`);

  const allowed = new Set(["references", "scripts", "templates"]);
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory() && !allowed.has(e.name)) errs.push(`子目录 '${e.name}' 不被平台接受`);
  }
  return { errs, fm };
}

// ---- 打包（平台要求：根目录 SKILL.md，子目录仅 references/scripts/templates）----
function buildZip(dir, out) {
  const { execFileSync } = require("node:child_process");
  const parts = [];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory() && ["references", "scripts", "templates"].includes(e.name)) {
      parts.push("-r", path.join(dir, e.name), e.name);
    }
  }
  parts.push(path.join(dir, "SKILL.md"), "SKILL.md");
  execFileSync("zip", ["-qr", out, ...parts], { cwd: dir });
  return out;
}

async function main() {
  log("=== 现成方案雷达 · 上架流程（浏览器上下文）===");

  // 1) 本地预检
  const { errs, fm } = validate(SKILL_DIR);
  if (errs.length) {
    for (const e of errs) log(`  [ERROR] ${e}`);
    log("预检未通过，中止。");
    process.exit(1);
  }
  log(`  预检通过：${fm.name} v${fm.version} by ${fm.author}`);

  // 2) 打包
  buildZip(SKILL_DIR, ZIP_PATH);
  const sizeKB = (fs.statSync(ZIP_PATH).size / 1024).toFixed(1);
  log(`  打包完成：${ZIP_PATH}（${sizeKB} KB）`);

  const task = await taskSpace("wb-publish");
  const page = task.page("p1");
  await page.goto(`${BASE}/developer-profile`);
  await new Promise((r) => setTimeout(r, 5000));

  // 3) 拿 partyId（必要时轮询等待实名认证完成）
  let party = await readPartyId(page);
  if (!party.partyId && WAIT) {
    log(`  未实名认证（certMethod=${party.certMethod}），轮询等待 partyId…`);
    log(`  请打开 ${BASE}/settings/developer 完成实名认证（需二代身份证 + 已实名手机号 + 人脸核身）`);
    const t0 = Date.now();
    let n = 0;
    while (Date.now() - t0 < WAIT_TIMEOUT) {
      await new Promise((r) => setTimeout(r, INTERVAL));
      party = await readPartyId(page);
      n++;
      if (party.partyId) { log(`  ✓ 拿到 partyId（${n} 次探测）`); break; }
      if (n % 4 === 0) {
        await page.goto(`${BASE}/developer-profile`);
        await new Promise((r) => setTimeout(r, 3000));
        log(`  …仍在等待（第 ${n} 次）`);
      }
    }
  }

  if (!party.partyId) {
    log("✗ 缺少 partyId —— 账号尚未完成实名认证。");
    log("  这是腾讯的人脸核验，无法用脚本绕过。请完成一次认证后重跑本命令。");
    process.exit(2);
  }
  log(`  partyId: ${party.partyId}`);

  // 4) 初始化上传
  log("  [1/4] 初始化上传");
  let r = await api(page, "POST", `${V2}/capabilities/init-upload`, {
    name: fm.name,
    displayName: fm.display_name,
    version: fm.version,
    type: "skill",
    partyId: party.partyId,
  });
  log(`    → ${r.status} ${JSON.stringify(r.body).slice(0, 260)}`);
  const d = r.body?.data || r.body || {};
  const uploadUrl = d.uploadUrl || d.upload_url || d.url || d.ossUrl;
  const uploadId = d.uploadId || d.upload_id || d.id;
  if (!uploadUrl) {
    log("    ✗ 未拿到上传地址");
    process.exit(3);
  }

  // 5) 上传文件（用 Node fetch 直传 OSS，不带平台 cookie）
  log("  [2/4] 上传 zip");
  const zipBuf = fs.readFileSync(ZIP_PATH);
  const up = await fetch(uploadUrl, {
    method: "PUT",
    body: zipBuf,
    headers: { "Content-Type": "application/zip", "Content-Length": String(zipBuf.length) },
  });
  log(`    → HTTP ${up.status}`);

  // 6) 完成上传（平台开包解析 → 生成 assetId）
  log("  [3/4] 平台解析");
  r = await api(page, "POST", `${V2}/capabilities/complete-upload`, {
    uploadId, name: fm.name, type: "skill", partyId: party.partyId,
  });
  log(`    → ${r.status} ${JSON.stringify(r.body).slice(0, 300)}`);
  const dd = r.body?.data || r.body || {};
  const assetId = dd.id || dd.assetId || dd.skillId || dd.capabilityId;
  if (!assetId) {
    log("    ✗ 未拿到 assetId");
    process.exit(4);
  }
  log(`    assetId = ${assetId}`);

  // 7) 提交审核
  if (SUBMIT) {
    log("  [4/4] 提交审核");
    r = await api(page, "POST", `${V2}/capabilities/submit`, {
      id: assetId, type: "skill", partyId: party.partyId, originality: true,
    });
    log(`    → ${r.status} ${JSON.stringify(r.body).slice(0, 300)}`);
  }

  // 8) 定价 + 发布
  if (PUBLISH) {
    if (PRICE <= 0) {
      log("  未指定价格，跳过定价");
    } else {
      log(`  设置付费：${MONTHLY ? "按月" : "一次性"} ${PRICE} 元`);
      r = await api(page, "POST", `${V2}/capabilities/${assetId}/pay`, {
        payMode: MONTHLY ? 2 : 1,
        price: PRICE,
        payDetail: [{ price: PRICE, description: (fm.description_zh || "").slice(0, 80) }],
        partyId: party.partyId,
      });
      log(`    → ${r.status} ${JSON.stringify(r.body).slice(0, 260)}`);
      log("    ⚠️ 付费方式发布后不可更改");

      log("  发布");
      r = await api(page, "POST", `${V2}/capabilities/${assetId}/publish`, {
        visibleScope: "public", partyId: party.partyId,
      });
      log(`    → ${r.status} ${JSON.stringify(r.body).slice(0, 260)}`);
    }
  }

  // 9) 建推广渠道
  log("  创建推广渠道");
  for (const way of ["link", "wxacode"]) {
    r = await api(page, "POST", `${V2}/share/channels/create`, {
      assetId, assetType: "skill", channelTag: `GitHub-${way}`,
      shareWay: way, channelName: fm.display_name, partyId: party.partyId,
    });
    log(`    [${way}] → ${r.status} ${JSON.stringify(r.body).slice(0, 220)}`);
  }

  // 10) 查收入接口（此时应为 0，但验证接口连通）
  r = await api(page, "GET", `${V1}/income-stats?partyId=${party.partyId}`);
  log(`  收入接口 → ${r.status} ${JSON.stringify(r.body).slice(0, 260)}`);

  log("");
  log("=== 完成 ===");
  log(`assetId = ${assetId}`);
  log("审核状态查询：重新运行本脚本带 --status，或查 notifications");
}

main().catch((e) => {
  log("异常：" + String(e).slice(0, 300));
  process.exit(9);
});
