#!/usr/bin/env nodejs
/**
 * scripts/extract-session.mjs — 从浏览器导出开放平台后台接口所需的 Cookie
 *
 * 背景：开放平台后台鉴权是 Cookie + X-Party-Id。Cookie 在 Chrome 里是 httpOnly，
 * 常规 document.cookie 拿不到，但可以用 CDP 的 Network.getAllCookies 取到。
 *
 * 用法：
 *   ego-browser nodejs solution-radar/scripts/extract-session.mjs
 *   # 输出 shell 可直接 eval 的 export 语句
 */
import { writeFile } from "node:fs/promises";

const task = await taskSpace("wb-session-extract");
// ⚠️ 实测（2026-11-01）：task.page("p1") 在没有可用标签页时不抛错也不返回 null，
// 而是返回 truthy 句柄，错误要到使用时才抛。所以必须逐个候选做 url() 健康检查。
let page = null;
{
  const candidates = [];
  try { const p = task.page("p1"); if (p) candidates.push(p); } catch {}
  try { for (const p of (await task.pages()) || []) candidates.push(p); } catch {}
  for (const p of candidates) {
    try { await p.url(); page = p; break; } catch {}
  }
  if (!page) {
    try { const p = await task.newPage(); await p.url(); page = p; } catch {}
  }
}
if (!page) {
  console.error("无法取得可用标签页（wb-session-extract 空间内无可用 page 且新建失败）");
  process.exit(10);
}

// 先访问目标域，让它把 cookie 落到该域
await page.goto("https://open.workbuddy.cn/");
await new Promise((r) => setTimeout(r, 4000));

const raw = await page.cdp("Network.getAllCookies");
const cookies = raw.cookies || [];

// 只要 open.workbuddy.cn 域下的（后台接口同源）
const scoped = cookies.filter((c) =>
  c.domain === "open.workbuddy.cn" ||
  c.domain === ".workbuddy.cn" ||
  c.domain === "workbuddy.cn"
);

if (!scoped.length) {
  console.error("未取到 open.workbuddy.cn 域下的 cookie");
  process.exit(2);
}

const cookieHeader = scoped
  .map((c) => `${c.name}=${c.value}`)
  .join("; ");

// 顺便取 localStorage 里的 partyId（若已实名认证则存在）
const state = await page.evaluate(() => {
  let auth = null;
  try { auth = JSON.parse(localStorage.getItem("workbuddy-auth") || "null"); } catch {}
  return { auth, loggedIn: localStorage.getItem("loggedIn") };
});

const partyId = state?.auth?.partyId || "";

const out = [
  "# 由 extract-session.mjs 生成（自动生成，勿手工编辑）",
  `export WB_COOKIE='${cookieHeader.replace(/'/g, "'\\''")}'`,
  partyId ? `export WB_PARTY_ID='${partyId}'` : "# WB_PARTY_ID 待定：账号尚未完成实名认证（workbuddy-auth.partyId = null）",
  `export WB_BASE='https://open.workbuddy.cn'`,
].join("\n");

// 不把 Cookie 打到 stdout（属于敏感凭证，会被工具层拦截）。
// 只写文件，调用方自行 source 它。
const envFile = `${process.env.HOME}/ego/auto-income-20261030/wb-session.env`;
await writeFile(envFile, out + "\n", { encoding: "utf8", mode: 0o600 });

// stdout 只给脱敏摘要
console.log(JSON.stringify({
  ok: true,
  cookie_file: envFile,
  cookie_names: scoped.map((c) => c.name),
  cookie_count: scoped.length,
  cookie_chars: cookieHeader.length,
  party_id: partyId || null,
  logged_in: state?.loggedIn === "true",
  cert_method: state?.auth?.certMethod || null,
}));
