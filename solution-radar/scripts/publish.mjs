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
 *   ego-browser nodejs < solution-radar/scripts/publish.mjs
 *   ⚠️ 不要在命令后追加参数：ego-browser nodejs 会拒绝尾随参数。
 *   参数改用配置文件传入（默认 /tmp/wb_publish_config.json）：
 *     {"submit":true,"publish":true,"price":19,"monthly":true}
 *   也可用环境变量 WB_PUBLISH_CONFIG 指定其他配置文件路径。
 *   参数含义：
 *     submit    上传后立即提交审核
 *     publish   审核通过则定价并发布（需配合 price）
 *     price     定价（元）
 *     monthly   按月订阅而非一次性买断
 *     wait      轮询等待 partyId（实名认证完成后自动继续）
 */

const fs = require("node:fs");
const path = require("node:path");

const args = process.argv.slice(2);
const has = (f) => args.includes(f);
const val = (f, d) => {
  const i = args.indexOf(f);
  return i >= 0 && args[i + 1] ? args[i + 1] : d;
};

// 参数来源：命令行参数 或 配置文件。
// 实测（2026-11-01）：`ego-browser nodejs` 拒绝任何尾随参数
// （报 "accepts at most one source argument"），且不透传环境变量，
// 因此 `ego-browser nodejs < publish.mjs -- --submit --publish ...` 这种写法
// 必然失败，且退出码恰好是 2 —— 与「缺少 partyId」的退出码完全相同，
// 会把真实错误掩盖成「还没实名认证」。
// 解决办法：把参数写进配置文件，由脚本自己读取。
// 用法：
//   ego-browser nodejs < solution-radar/scripts/publish.mjs
//   （可选）先写 /tmp/wb_publish_config.json：
//     {"submit":true,"publish":true,"price":19,"monthly":true,"wait":false}
const CFG_PATH = process.env.WB_PUBLISH_CONFIG || "/tmp/wb_publish_config.json";
let cfg = {};
try {
  cfg = JSON.parse(fs.readFileSync(CFG_PATH, "utf8"));
} catch { /* 无配置文件则全部用默认值 */ }

const flag = (name, key, dflt) => {
  if (cfg[key] !== undefined) return cfg[key];
  return dflt;
};

const SUBMIT = cfg.submit !== undefined ? !!cfg.submit : has("--submit");
const PUBLISH = cfg.publish !== undefined ? !!cfg.publish : has("--publish");
const PRICE = parseInt(cfg.price !== undefined ? cfg.price : val("--price", "0"), 10) || 0;
const MONTHLY = cfg.monthly !== undefined ? !!cfg.monthly : has("--monthly");
const WAIT = cfg.wait !== undefined ? !!cfg.wait : has("--wait");
const WAIT_TIMEOUT = parseInt(cfg.waitTimeout !== undefined ? cfg.waitTimeout : val("--wait-timeout", "3600"), 10) * 1000;
const INTERVAL = parseInt(cfg.interval !== undefined ? cfg.interval : val("--interval", "30"), 10) * 1000;

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

// ---- 读实名认证状态 ----
// 优先 localStorage（前端 zustand store 的 partyId）；若为空，回落到服务端
// GET /api/v2/open-platform/parties —— 该端点是平台官方的「主体列表」，
// 已实名时其中会有 partyType=personal 且 verifyStatus=verified 的记录，
// 其 partyId 才是真正被服务端签发的主体 ID。
// 实测（2026-11-01）：用户提交邮箱验证码后，平台会先创建一条 draft 主体，
// 人脸核身通过后才变 verified；此时若 SPA 尚未刷新 localStorage，
// 单看 localStorage 会误判「未认证」，而 parties 列表是权威口径。
async function readPartyId(page) {
  const local = await page.evaluate(() => {
    let auth = null;
    try { auth = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch {}
    return {
      partyId: auth?.partyId || null,
      certMethod: auth?.certMethod || null,
      loggedIn: localStorage.getItem("loggedIn") === "true",
    };
  });

  let server = null;
  try {
    server = await page.evaluate(async () => {
      const r = await fetch("/api/v2/open-platform/parties", { credentials: "include" });
      const j = await r.json();
      const list = j?.data?.parties || [];
      const verified = list.find((p) => p.partyType === "personal" && p.verifyStatus === "verified");
      const draft = list.find((p) => p.partyType === "personal");
      return {
        code: j?.code,
        count: list.length,
        verifiedPartyId: verified?.partyId || null,
        draftPartyId: draft?.partyId || null,
        draftStatus: draft?.verifyStatus || null,
        draftNextAction: draft?.nextAction || null,
        emailVerified: draft?.contactEmailVerified ?? null,
        operatorVerified: draft?.operatorVerified ?? null,
      };
    });
  } catch { /* 服务端不可达时以 localStorage 为准 */ }

  const partyId = local.partyId || server?.verifiedPartyId || null;
  return { ...local, partyId, server };
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

/**
 * 取一个可用的 page。
 *
 * ⚠️ 实测（2026-11-01，第 36 轮）踩坑：`task.page("p1")` 在空间里没有可用标签页时
 * **不会抛错，也不会返回 null**，而是返回一个「truthy 的句柄对象」；
 * 真正的错误要等到第一次使用它时才抛出：
 *     Error: page label not found: p1. Managed Pages in task space 145: none.
 *     Error: page p1 was closed
 * 因此下面这种「看起来很稳妥」的写法其实是坏的 ——
 *     try { page = task.page("p1"); } catch {}
 *     if (!page) { page = (await task.pages())[0]; }   // ← 永远进不来
 * 句柄恒为真值 → 兜底分支永不执行 → 错误延后到 page.goto() 才炸 →
 * 顶层 main().catch 兜住 → 以退出码 9 结束，与「无 task space」的退出码撞车，
 * 真实原因（标签页没了）被彻底掩盖。这已是连续第三次退出码撞车。
 *
 * 正确做法：逐个候选「用一次」来验证可用性（await p.url()），
 * 坏的句柄在使用时会抛错，正好用来做健康检查；全都不行再 newPage()。
 */
async function acquirePage(task, log) {
  const candidates = [];
  // 1) 具名标签页 p1 —— 用 url() 验证它是否真的活着
  try {
    const p = task.page("p1");
    if (p) candidates.push({ p, src: "page(p1)" });
  } catch (e) {
    if (log) log(`  task.page("p1") 直接抛错：${String(e).slice(0, 90)}`);
  }
  // 2) 空间里的其他标签页
  try {
    const ps = await task.pages();
    for (const p of ps || []) candidates.push({ p, src: "pages()" });
  } catch (e) {
    if (log) log(`  task.pages() 抛错：${String(e).slice(0, 90)}`);
  }

  for (const c of candidates) {
    try {
      await c.p.url(); // 健康检查：已关闭/不存在的句柄在此抛错
      if (log) log(`  页面来源：${c.src}`);
      return c.p;
    } catch (e) {
      if (log) log(`  候选 ${c.src} 不可用：${String(e).slice(0, 80)}`);
    }
  }

  // 3) 全部候选都不可用 → 新建标签页
  try {
    const p = await task.newPage();
    await p.url();
    if (log) log("  页面来源：newPage()");
    return p;
  } catch (e) {
    if (log) log(`  newPage() 失败：${String(e).slice(0, 90)}`);
    return null;
  }
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

  // 选取一个 agent 自有的 task space。
  // 实测（2026-11-01）：硬编码 "wb-publish" 在该空间已被用户接管后会直接抛错
  // 「A task space named "wb-publish" already exists and belongs to the user」，
  // ego 进程以退出码 9 结束 —— 这会掩盖真实的认证状态。
  // 策略：依次尝试候选空间名；某个空间若归用户所有则跳过（绝不擅自接管），
  // 全部候选都不可用时回落到 probe 空间（agent 自有）。
  const SPACE_CANDIDATES = ["wb-publish-auto", "wb-publish-run", "wb-daemon-probe"];
  let task = null;
  let usedSpace = null;
  for (const name of SPACE_CANDIDATES) {
    try {
      task = await taskSpace(name);
      usedSpace = name;
      break;
    } catch (e) {
      log(`  空间 ${name} 不可用：${String(e).slice(0, 90)}`);
    }
  }
  if (!task) {
    log("✗ 没有可用的 task space（候选均被用户接管），中止。");
    process.exit(11);
  }
  log(`  使用 task space：${usedSpace}`);

  // page 标签页可能不存在或已关闭，取一个真正可用的（见 acquirePage 注释）
  const page = await acquirePage(task, log);
  if (!page) {
    log("✗ 无法取得可用页面（无标签页且新建失败），中止。");
    process.exit(10);
  }
  // 导航到真实存在的开发者设置页（/developer-profile 是 404 路由，不要用）
  try {
    await page.goto(`${BASE}/settings/developer`);
    await new Promise((r) => setTimeout(r, 5000));
  } catch (e) {
    log(`✗ 打开开发者设置页失败：${String(e).slice(0, 160)}`);
    process.exit(10);
  }

  // 3) 拿 partyId（必要时轮询等待实名认证完成）
  let party = await readPartyId(page);
  if (!party.partyId && WAIT) {
    const srv = party.server || {};
    log(`  未实名认证（certMethod=${party.certMethod}），轮询等待 partyId…`);
    log(`  服务端主体列表：count=${srv.count ?? "?"}${srv.draftPartyId ? "（有 draft 主体，待人脸核身）" : "（尚未创建主体，尚未提交认证）"}`);
    log(`  请打开 ${BASE}/settings/developer 完成实名认证（需二代身份证 + 已实名手机号 + 人脸核身）`);
    const t0 = Date.now();
    let n = 0;
    while (Date.now() - t0 < WAIT_TIMEOUT) {
      await new Promise((r) => setTimeout(r, INTERVAL));
      party = await readPartyId(page);
      n++;
      if (party.partyId) { log(`  ✓ 拿到 partyId（${n} 次探测）`); break; }
      if (n % 4 === 0) {
        await page.goto(`${BASE}/settings/developer`);
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

// 退出码约定（务必互不重叠，否则会掩盖真实原因）：
//   0  = 全部成功
//   1  = 本地预检未通过
//   2  = 缺少 partyId（账号未完成实名认证）—— 守护脚本依赖此码
//   3  = 未拿到上传地址
//   4  = 未拿到 assetId
//   10 = 无可用页面（标签页全部关闭/新建失败）
//   11 = 无可用 task space（候选均被用户接管）
//   12 = 未归类异常（见下方 catch）
main().catch((e) => {
  log("异常：" + String(e).slice(0, 300));
  process.exit(12);
});
