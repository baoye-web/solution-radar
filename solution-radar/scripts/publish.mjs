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
 * ⚠️ 请求体契约（2026-11-01 从开放平台前端 chunk 逆向确认，详见
 *    .workbuddy/memory/openplatform-api-contracts.md）：
 *   init-upload   → {capabilityType, fileName, fileSize, mimeType}
 *   complete-upload→ {uploadId, assetId?}
 *   submit        → {capabilityType, action, assetId, uploadToken, clientToken,
 *                    fields:{common:{categories, marketCategories, payMode, payDetail, needEncrypt}}}
 *   publish       → {visibleScope:["ALL"]}
 *   /pay          → {capabilityType, payMode, payDetailJson:[{price, duration_days}]}
 *   share/channels/create → {assetId, capabilityType, channelTag, shareWays:[...]}
 *   金额单位是「分」：19 元/月 → payMode:2, payDetail:[{price:1900, duration_days:30}]
 *
 * 用法：
 *   ego-browser nodejs < solution-radar/scripts/publish.mjs
 *   ⚠️ 不要在命令后追加参数：ego-browser nodejs 会拒绝尾随参数，
 *      且不透传环境变量（实测 process.env 只有 PATH/HOME）。
 *      参数走配置文件，见 CONFIG_CANDIDATES。
 */

const fs = require("node:fs");
const path = require("node:path");

// ---- 配置来源 ------------------------------------------------------------
// ego-browser nodejs 不透传自定义环境变量，因此只能用文件传参。
// 第一个存在的文件生效；守护脚本写入第一个路径。
const CONFIG_CANDIDATES = [
  "/Users/happysir/ego/auto-income-20261030/publish-config.json",
  "/tmp/wb_publish_config.json",
];
let cfg = {};
for (const p of CONFIG_CANDIDATES) {
  try {
    cfg = JSON.parse(fs.readFileSync(p, "utf8"));
    break;
  } catch { /* 换下一个 */ }
}
const flag = (key, dflt) => (cfg[key] !== undefined ? cfg[key] : dflt);

const SUBMIT = !!flag("submit", true);
const PUBLISH = !!flag("publish", true);
const PRICE_YUAN = Number(flag("price", 19)); // 元（对外），内部按分传给平台
const MONTHLY = !!flag("monthly", true);
const WAIT = !!flag("wait", false);
const WAIT_TIMEOUT = Number(flag("waitTimeout", 3600)) * 1000;
const INTERVAL = Number(flag("interval", 30)) * 1000;
// 幂等：按技能名查已有资产并续跑（审核通过后无需重新上传）
const RESUME = !!flag("resume", true);

const BASE = "https://open.workbuddy.cn";
const V2 = `${BASE}/api/v2/open-platform`;
const V1 = `${BASE}/api/v1/open-platform/charge-developer/merchant`;

const MAX_ZIP_BYTES = 3145728; // 平台对 Skill 包的硬限制：3 MiB

// ego-browser nodejs 走 stdin 时：process.cwd() === "/"，__dirname === "."
// 因此必须用绝对路径定位 skill 包。优先读 WB_SKILL_DIR，其次按已知布局推断。
function resolveSkillDir() {
  const home = process.env.HOME || "/Users/happysir";
  const candidates = [
    process.env.WB_SKILL_DIR,
    path.join(home, "ego/auto-income-20261030/agent-radar/solution-radar"),
    path.join(home, ".workbuddy/skills/solution-radar"),
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

// ---- 在页面上下文里调接口 -------------------------------------------------
// 平台前端 http client 会把 {code,data,msg} 拆包，只把 data 给业务层。
// page.fetch 拿到的是原始包裹，所以这里统一拆包并把 code 单独返回。
async function api(page, method, url, body) {
  const opts = {
    method,
    headers: { "content-type": "application/json" },
    timeout: 30000,
  };
  if (body !== undefined && body !== null) opts.body = JSON.stringify(body);
  const r = await page.fetch(url, opts);
  let parsed = r.body;
  if (typeof parsed === "string") {
    try { parsed = JSON.parse(parsed); } catch { /* 保持字符串 */ }
  }
  const code = parsed && typeof parsed === "object" ? parsed.code : undefined;
  const data = parsed && typeof parsed === "object" && parsed.data !== undefined
    ? parsed.data
    : parsed;
  return { status: r.status, code, data, raw: parsed };
}

// ---- 读实名认证状态 ------------------------------------------------------
// 优先 localStorage（前端 zustand store 的 partyId）；若为空，回落到服务端
// GET /api/v2/open-platform/parties —— 该端点是平台官方的「主体列表」，
// 已实名时其中会有 partyType=personal 且 verifyStatus=verified 的记录。
async function readPartyId(page) {
  const local = await page.evaluate(() => {
    let auth = null;
    try { auth = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch {}
    return {
      partyId: auth?.partyId || auth?.state?.partyId || null,
      certMethod: auth?.certMethod || auth?.state?.certMethod || null,
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
        emailVerified: draft?.contactEmailVerified ?? null,
      };
    });
  } catch { /* 服务端不可达时以 localStorage 为准 */ }

  const partyId = local.partyId || server?.verifiedPartyId || null;
  return { ...local, partyId, server };
}

// ---- 本地预检（与平台前端校验规则一致，双保险）--------------------------
function parseFrontmatter(dir) {
  const mdPath = path.join(dir, "SKILL.md");
  if (!fs.existsSync(mdPath)) return { errs: ["缺少 SKILL.md"], fm: {} };
  const text = fs.readFileSync(mdPath, "utf8");
  const end = text.indexOf("\n---", 3);
  const fm = {};
  if (text.startsWith("---") && end > 0) {
    for (const line of text.slice(3, end).split("\n")) {
      const m = line.match(/^([A-Za-z_][\w-]*)\s*:\s*(.*)$/);
      if (m) fm[m[1]] = m[2].trim().replace(/^["']|["']$/g, "");
    }
  }
  return { errs: [], fm };
}

function validate(dir) {
  const { errs: fmErrs, fm } = parseFrontmatter(dir);
  const errs = [...fmErrs];
  // 平台《技能基础结构》文档列出的必填 frontmatter 字段
  const required = ["name", "display_name", "display_name_en", "description",
    "description_zh", "description_en", "version", "author"];
  for (const k of required) if (!fm[k]) errs.push(`frontmatter 缺必填字段：${k}`);
  // 平台只接受 references / scripts / templates 三个子目录
  const allowed = new Set(["references", "scripts", "templates"]);
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory() && !allowed.has(e.name)) errs.push(`子目录 '${e.name}' 不被平台接受`);
  }
  return { errs, fm };
}

// ---- 打包 ----------------------------------------------------------------
// 平台按 zip 内的 entry path 定位 SKILL.md，因此包内只能是干净的相对路径。
// 之前用 `zip -qr out <绝对路径> <相对名>` 会同时写入绝对路径与相对路径两套
// entry（Users/happysir/... 与 references/...），entry 前缀不一致会让平台
// 的 fileExists 校验失败。这里统一以 skill 目录为 cwd、只用相对名打包。
function buildZip(dir, out) {
  const { execFileSync } = require("node:child_process");
  // 注意 zip 的两个陷阱：
  //  1) 参数顺序：`-x` 之后的所有非选项参数都会被当成排除模式，
  //     所以文件清单必须全部放在 `-x` 之前，否则 SKILL.md 会被误排除。
  //  2) zip 对已存在的归档是「增量合并」而不是覆盖，
  //     旧的绝对路径 entry 会残留并让包体积膨胀 → 每次先删旧包。
  try { fs.unlinkSync(out); } catch { /* 不存在则忽略 */ }
  const files = ["SKILL.md"];
  for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
    if (e.isDirectory() && ["references", "scripts", "templates"].includes(e.name)) {
      files.push(e.name);
    }
  }
  execFileSync("zip", ["-qr", out, ...files, "-x", "*__pycache__*", "-x", "*.pyc", "-x", ".DS_Store"], { cwd: dir });
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
  try {
    const p = task.page("p1");
    if (p) candidates.push({ p, src: "page(p1)" });
  } catch (e) {
    if (log) log(`  task.page("p1") 直接抛错：${String(e).slice(0, 90)}`);
  }
  try {
    const ps = await task.pages();
    for (const p of ps || []) candidates.push({ p, src: "pages()" });
  } catch (e) {
    if (log) log(`  task.pages() 抛错：${String(e).slice(0, 90)}`);
  }

  for (const c of candidates) {
    try {
      await c.p.url();
      if (log) log(`  页面来源：${c.src}`);
      return c.p;
    } catch (e) {
      if (log) log(`  候选 ${c.src} 不可用：${String(e).slice(0, 80)}`);
    }
  }

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

// ---- 分类解析 ------------------------------------------------------------
// fields.common.categories ← 业务分类树（business-categories）的叶子节点
// fields.common.marketCategories ← 市场分类（capabilities/categories?capabilityType=skill）
async function resolveCategories(page, log) {
  const biz = await api(page, "GET", `${V2}/capabilities/business-categories`);
  const tree = biz.data?.categories || [];
  const leaves = [];
  const walk = (nodes) => {
    for (const n of nodes || []) {
      const kids = n.children || [];
      if (kids.length) walk(kids);
      else if (!n.qualificationRequired) leaves.push(n); // 需要资质的分类要额外交资质文件，避开
    }
  };
  walk(tree);
  const chosen = leaves[0];
  if (!chosen) {
    log("  ✗ 业务分类树里找不到无需资质的叶子分类");
    return null;
  }

  const mkt = await api(page, "GET", `${V2}/capabilities/categories?capabilityType=skill`);
  const all = mkt.data?.categories || [];
  // configScope: 0=All 可选，1=OpsOnly 不可选
  const selectable = all.filter((c) => c.id && (c.configScope === undefined || c.configScope === null || c.configScope === 0));
  const marketIds = selectable.slice(0, 3).map((c) => c.id);
  if (!marketIds.length) {
    log("  ✗ 市场分类列表为空或全部为 OpsOnly");
    return null;
  }
  log(`  业务分类：${chosen.id}（${chosen.name?.zh || chosen.id}）`);
  log(`  市场分类：${marketIds.join(", ")}`);
  return { categories: [{ categoryId: chosen.id }], marketCategories: marketIds };
}

// ---- 查已有资产（续跑用）-------------------------------------------------
async function findExisting(page, name) {
  const r = await api(page, "POST", `${V2}/capabilities/list`, {
    page: 1,
    pageSize: 50,
    filters: [{ name: "capability_type", values: ["skill"] }],
  });
  const items = r.data?.items || [];
  return items.find((it) => it && (it.name === name || it.displayName?.zh === name)) || null;
}

// ---- 商户认证状态（定价前置条件）----------------------------------------
async function merchantCertStatus(page, partyId, assetId) {
  const url = `${V1}/cert-status?partyId=${encodeURIComponent(partyId)}` +
    (assetId ? `&resourceId=${encodeURIComponent(assetId)}` : "");
  const r = await api(page, "GET", url);
  const d = r.data || {};
  return {
    ok: r.code === 0,
    certified: d.certified === true,
    chargeEnabled: d.chargeEnabled === true,
    isBlocked: d.isBlocked === true,
    registerStatus: d.registerStatus ?? null,
    blockReason: d.blockReason || "",
  };
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
  const zipSize = fs.statSync(ZIP_PATH).size;
  if (zipSize > MAX_ZIP_BYTES) {
    log(`  ✗ zip 体积 ${(zipSize / 1048576).toFixed(2)} MiB 超过平台上限 3 MiB`);
    process.exit(1);
  }
  log(`  打包完成：${ZIP_PATH}（${(zipSize / 1024).toFixed(1)} KB）`);

  // 3) 选取一个 agent 自有的 task space
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

  const page = await acquirePage(task, log);
  if (!page) {
    log("✗ 无法取得可用页面（无标签页且新建失败），中止。");
    process.exit(10);
  }
  try {
    await page.goto(`${BASE}/settings/developer`);
    await new Promise((r) => setTimeout(r, 5000));
  } catch (e) {
    log(`✗ 打开开发者设置页失败：${String(e).slice(0, 160)}`);
    process.exit(10);
  }

  // 4) 拿 partyId（必要时轮询等待实名认证完成）
  let party = await readPartyId(page);
  if (!party.partyId && WAIT) {
    const srv = party.server || {};
    log(`  未实名认证（certMethod=${party.certMethod}），轮询等待 partyId…`);
    log(`  服务端主体列表：count=${srv.count ?? "?"}`);
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

  // 5) 续跑：按技能名找已有资产
  let asset = null;
  if (RESUME) {
    try {
      asset = await findExisting(page, fm.name);
    } catch (e) {
      log(`  查询已有资产失败（忽略）：${String(e).slice(0, 120)}`);
    }
  }
  if (asset) {
    log(`  发现已有资产：assetId=${asset.assetId} status=${asset.status}`);
    if (asset.status === "rejected") {
      try {
        const reason = await api(page, "GET", `${V2}/capabilities/${asset.assetId}/reasons`);
        log(`  驳回原因：${JSON.stringify(reason.data).slice(0, 400)}`);
      } catch { /* 忽略 */ }
      log("  平台公约禁止无修改重提：请真正修改 SKILL.md 后重跑。");
      process.exit(6);
    }
    if (asset.status === "under_review" || asset.status === "draft") {
      log(`  当前状态 ${asset.status}，等待平台审核（最迟 24 小时，微信通知）。`);
      process.exit(6);
    }
  }

  let assetId = asset?.assetId || null;

  // 6) 上传 + 提交审核
  if (!assetId) {
    log("  [1/4] 初始化上传");
    let r = await api(page, "POST", `${V2}/capabilities/init-upload`, {
      capabilityType: "skill",
      fileName: path.basename(ZIP_PATH),
      fileSize: zipSize,
      mimeType: "application/zip",
    });
    log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 240)}`);
    if (r.code !== 0) {
      log("    ✗ init-upload 被平台拒绝");
      process.exit(5);
    }
    const uploadUrl = r.data?.uploadUrl;
    const uploadId = r.data?.uploadId;
    if (!uploadUrl || !uploadId) {
      log("    ✗ 未拿到 uploadUrl / uploadId");
      process.exit(3);
    }

    log("  [2/4] 直传 COS");
    const zipBuf = fs.readFileSync(ZIP_PATH);
    const up = await fetch(uploadUrl, {
      method: "PUT",
      body: zipBuf,
      headers: { "Content-Type": "application/zip" },
    });
    log(`    → HTTP ${up.status}`);
    if (up.status !== 200 && up.status !== 204) {
      log("    ✗ COS 直传失败");
      process.exit(5);
    }

    log("  [3/4] 平台开包解析");
    r = await api(page, "POST", `${V2}/capabilities/complete-upload`, { uploadId });
    log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 260)}`);
    if (r.code !== 0) {
      log("    ✗ complete-upload 被平台拒绝（通常是包结构或 frontmatter 不合法）");
      process.exit(5);
    }
    assetId = r.data?.assetId;
    const uploadToken = r.data?.uploadToken;
    if (!assetId) {
      log("    ✗ 未拿到 assetId");
      process.exit(4);
    }
    log(`    assetId = ${assetId}`);

    // 服务端解析结果可用于校验 frontmatter 字段名是否被平台认到
    const parsed = r.data?.parsed;
    const dn = parsed?.common?.displayName;
    if (parsed) {
      log(`    服务端解析：displayName=${JSON.stringify(dn)} version=${parsed.version ?? "?"}` +
        ` usageExamples=${(parsed.skill?.usageExamples || []).length}`);
      if (!dn || (!dn.zh && !dn.en)) {
        log("    ⚠️ 服务端没解析出 displayName —— 检查 SKILL.md 的 display_name/display_name_en");
      }
    }

    log("  [4/4] 提交审核");
    const cat = await resolveCategories(page, log);
    if (!cat) {
      log("    ✗ 分类解析失败");
      process.exit(5);
    }
    const cert = await merchantCertStatus(page, party.partyId, assetId);
    const priced = PRICE_YUAN > 0 && cert.certified;
    if (PRICE_YUAN > 0 && !cert.certified) {
      log(`    ⚠️ 商户未认证（certified=${cert.certified} registerStatus=${cert.registerStatus}）` +
        ` → 先按免费提交，定价需先在「变现」页完成商户认证`);
    }
    const payFields = priced
      ? {
          payMode: MONTHLY ? 2 : 1,
          payDetail: [{
            price: Math.round(PRICE_YUAN * 100), // 平台单位：分
            duration_days: MONTHLY ? 30 : 0,
          }],
          needEncrypt: false,
        }
      : { payMode: 0, needEncrypt: false };

    r = await api(page, "POST", `${V2}/capabilities/submit`, {
      capabilityType: "skill",
      action: "create",
      assetId,
      uploadToken,
      clientToken: `pub_${crypto.randomUUID()}`,
      fields: {
        common: {
          categories: cat.categories,
          marketCategories: cat.marketCategories,
          releaseNote: `v${fm.version}`,
          ...payFields,
        },
      },
    });
    log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 300)}`);
    if (r.code !== 0) {
      log("    ✗ submit 被平台拒绝");
      process.exit(5);
    }
    log(`    提交成功，status=${r.data?.status ?? "?"}`);
    log("    审核最迟 24 小时（微信通知）。审核通过后重跑本脚本即可完成定价+发布+推广渠道。");
    process.exit(6);
  }

  // 7) 已存在且已 approved → 发布
  if (PUBLISH && asset?.status === "approved") {
    log(`  发布（visibleScope=["ALL"]）`);
    let r = await api(page, "POST", `${V2}/capabilities/${assetId}/publish`, {
      visibleScope: ["ALL"],
    });
    log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 240)}`);
    if (r.code !== 0) {
      log("    ✗ publish 失败");
      process.exit(5);
    }
  } else if (PUBLISH && asset?.status === "published") {
    log("  已是 published 状态，跳过发布");
  }

  // 8) 定价（已发布技能走 /pay；与平台「修改价格」同一端点）
  if (PUBLISH && PRICE_YUAN > 0 && asset?.status === "published") {
    const cert = await merchantCertStatus(page, party.partyId, assetId);
    if (!cert.certified) {
      log(`  ⚠️ 商户未认证（certified=${cert.certified}）→ 无法设置付费：${cert.blockReason || "需先完成商户认证"}`);
    } else {
      log(`  设置付费：${MONTHLY ? "按月" : "一次性"} ${PRICE_YUAN} 元`);
      const r = await api(page, "POST", `${V2}/capabilities/${assetId}/pay`, {
        capabilityType: "skill",
        payMode: MONTHLY ? 2 : 1,
        payDetailJson: [{
          price: Math.round(PRICE_YUAN * 100), // 分
          duration_days: MONTHLY ? 30 : 0,
        }],
      });
      log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 240)}`);
      if (r.code !== 0) {
        log("    ⚠️ 设置付费失败（可能触发年度改价配额）");
      }
    }
  }

  // 9) 建推广渠道（分享链接 + 小程序码；一次请求带两种方式）
  if (PUBLISH && asset?.status === "published") {
    log("  创建推广渠道");
    const ways = ["link", "wxacode"];
    const r = await api(page, "POST", `${V2}/share/channels/create`, {
      assetId,
      capabilityType: "skill",
      channelTag: "GitHub",
      shareWays: ways,
    });
    log(`    → ${r.status} code=${r.code} ${JSON.stringify(r.data).slice(0, 260)}`);
    if (r.code === 0 && r.data?.wxacode?.imageUrl) {
      log(`    小程序码：${r.data.wxacode.imageUrl}`);
    }
  }

  // 10) 查收入接口（此时应为 0，但验证接口连通）
  const inc = await api(page, "GET", `${V1}/income-stats?partyId=${encodeURIComponent(party.partyId)}`);
  log(`  收入接口 → ${inc.status} code=${inc.code} ${JSON.stringify(inc.data).slice(0, 240)}`);

  log("");
  log("=== 完成 ===");
  log(`assetId = ${assetId}`);
  log("审核状态查询：重新运行本脚本（自动续跑），或查开放平台通知中心");
}

main().catch((e) => {
  log("异常：" + String(e).slice(0, 300));
  process.exit(12);
});
