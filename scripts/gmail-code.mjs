#!/usr/bin/env nodejs
/**
 * scripts/gmail-code.mjs — 从 Gmail 读取 WorkBuddy 开放平台的邮箱验证码
 *
 * 背景（2026-11-01 第 35 轮实测）：
 *   · ego-browser 使用 Chrome Default profile，**Gmail 已登录**（imbaoye@gmail.com，
 *     收件箱 134 封未读）。这意味着认证流程第 2 步「邮箱验证码」可以自动取，
 *     不必让用户手动去邮箱里翻数字。
 *   · 认证流程顺序（前端包逆向，见 memory）：先 POST /parties 建 draft 主体 →
 *     邮箱验证码 verify → 最后才做人脸核身。
 *   · 因此只要用户已提交表单（parties 出现 draft 主体），验证码邮件就会到达 Gmail，
 *     本脚本可自动定位并提取。
 *
 * 用法：
 *   ego-browser nodejs < scripts/gmail-code.mjs
 * 输出：
 *   CODE:123456            —— 找到验证码
 *   CODE:none              —— 未找到（可能是还没发验证码，或已过期）
 *
 * 只读：不修改 Gmail 状态，不删除任何邮件。
 */

// 可切换的任务空间：用 agent 自有的空间，绝不用被用户接管的空间
const MAIL_SPACE = "wb-mail-check";

const log = (m) => console.log(m);

// 在文本中提取验证码。
// 平台邮件的验证码是 4~8 位纯数字。策略：先找「验证码/code/verification」附近的数字串，
// 取上下文里最靠近关键词的 6 位数字（腾讯体系常见 6 位）；找不到则退化为取最长数字串。
function extractCode(text) {
  if (!text) return null;
  const cleaned = text.replace(/[-‍]/g, "");
  const kw = /(验证码|校验码|动态码|verification\s*code|security\s*code|code)\s*[:：]?\s*([0-9]{4,8})/gi;
  let m, best = null;
  while ((m = kw.exec(cleaned)) !== null) {
    best = m[2];
  }
  if (best) return best;
  // 退化：找形如「123 456」或独立 6 位数字
  const spaced = cleaned.match(/\b(\d{3})\s*(\d{3})\b/);
  if (spaced) return spaced[1] + spaced[2];
  const standalone = cleaned.match(/\b(\d{6})\b/);
  if (standalone) return standalone[1];
  return null;
}

/**
 * 取一个可用的 page。
 *
 * ⚠️ 实测（2026-11-01 第 36 轮）：`task.page("p1")` 在没有可用标签页时
 * 不抛错也不返回 null，而是返回一个 truthy 句柄；错误要到第一次使用才抛
 * （"page label not found: p1" / "page p1 was closed"）。
 * 所以「if (!page) 才兜底」的写法是坏的 —— 兜底分支永远进不去。
 * 正确做法：逐个候选 await p.url() 做健康检查，坏的会在此抛错，正好用来跳过。
 */
async function acquirePage(task) {
  const candidates = [];
  try {
    const p = task.page("p1");
    if (p) candidates.push(p);
  } catch { /* 具名标签页不存在 */ }
  try {
    for (const p of (await task.pages()) || []) candidates.push(p);
  } catch { /* 忽略 */ }
  for (const p of candidates) {
    try {
      await p.url();
      return p;
    } catch { /* 已关闭或不存在，换下一个 */ }
  }
  try {
    const p = await task.newPage();
    await p.url();
    return p;
  } catch {
    return null;
  }
}

async function main() {
  const task = await taskSpace(MAIL_SPACE);
  const page = await acquirePage(task);
  if (!page) {
    console.log("CODE:none");
    console.log("NO_PAGE: 空间内无可用标签页，且新建失败");
    return;
  }

  const queries = [
    "subject:(验证码 OR verification code OR 校验码) newer_than:7d",
    "from:(workbuddy OR netease OR tencent) newer_than:7d",
    "开放平台 newer_than:7d",
  ];

  for (const q of queries) {
    let rows = null;
    try {
      await page.goto(
        "https://mail.google.com/mail/u/0/#search/" + encodeURIComponent(q),
        { timeout: 25000 }
      );
      await new Promise((r) => setTimeout(r, 4500));
      rows = await page.evaluate(() => {
        const out = [];
        for (const tr of Array.from(document.querySelectorAll("tr"))) {
          const t = (tr.innerText || "").replace(/\s+/g, " ").trim();
          if (t) out.push(t.slice(0, 200));
        }
        return out.slice(0, 15);
      });
    } catch (e) {
      log(`  查询「${q.slice(0, 40)}」失败：${String(e).slice(0, 100)}`);
      continue;
    }
    if (!rows || rows.length === 0) continue;

    for (const r of rows) {
      const code = extractCode(r);
      if (code) {
        log(`CODE:${code}`);
        log(`  来源行：${r.slice(0, 150)}`);
        return;
      }
    }
    log(`  查询「${q.slice(0, 40)}」命中 ${rows.length} 行，但未提取到验证码`);
  }

  log("CODE:none");
}

main().catch((e) => {
  log("CODE:none");
  log(`异常：${String(e).slice(0, 200)}`);
});
