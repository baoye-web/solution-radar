#!/usr/bin/env nodejs
/**
 * scripts/gmail-code.mjs — 从 Gmail 读取 WorkBuddy 开放平台的邮箱验证码
 *
 * 背景（2026-11-01 第 35 轮实测）：
 *   · ego-browser 使用 Chrome Default profile，**Gmail 已登录**（imbaoye@gmail.com）。
 *     这意味着认证流程第 2 步「邮箱验证码」可以自动取，不必让用户手动去邮箱里翻数字。
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

// ---- 平台取码实现开始 ----

const log = (m) => console.log(m);

/**
 * 允许采信的发件人（腾讯体系 + WorkBuddy 自身）。
 *
 * ⚠️ 为什么必须用**白名单**而不是黑名单（2026-11-02 第 59 轮实测教训）：
 *   取码一旦取到**别的服务的验证码**，用户会把它当成 WorkBuddy 的验证码填进
 *   「邮箱验证码」输入框 → 必然核验失败，而且极难排查（用户会以为是平台坏了）。
 *   实测事故：白名单缺失时取到的是 **Grok Bot** 的 737248，守护进程已于
 *   15:26:38 把它当作「WorkBuddy 邮箱验证码」打印给用户。
 *   漏掉一个域名的代价远大于多过滤一封邮件 —— 漏了会出现「假码」误导用户，
 *   多过滤只会退化成「用户手动去邮箱看码」，后者可接受。
 */
const SENDER_ALLOW = /(workbuddy|tencent|qq\.com|netease|163\.com|126\.com|tencent-cloud|woa\.com)/i;

/** 明确排除的发件人：即使正文含「验证码」也不采信（白名单之外的兜底否定）。 */
const SENDER_DENY = /(grok|xai|openai|okx|google|apify|microsoft|twitter|binance|anthropic|[@.]x\.com)/i;

/** 正文/标题出现这些词 → 这封邮件确实是 WorkBuddy 开放平台发的。 */
const PLATFORM_HINT = /(开放平台|实名认证|认证主体|主体认证|workbuddy\s*开放平台)/i;

// 在文本中提取验证码。
//
// 关键约束（2026-11-02 第 59 轮修正）：**文本里必须先出现「验证码」类关键词**
// 才允许提取数字。否则会从「验证码将在 10 分钟内过期」这类说明句、
// 或邮件页脚里的 UID / 订单号 / 时间戳里抓到一个 6 位数字当成验证码。
// 上一版把「任意独立 6 位数字」当退化兜底 → 正是它取到了 Grok Bot 的码。
function extractCode(text) {
  if (!text) return null;
  const cleaned = text.replace(/[-‍]/g, "");
  // 没有验证码关键词 → 一律不提取。
  if (!/(验证码|校验码|动态码|verification\s*code|security\s*code|one[-\s]?time\s*(?:password|code))/i.test(cleaned)) {
    return null;
  }
  // 关键词与数字之间允许最多 6 个非数字字符，覆盖：
  //   「验证码是 737248」「验证码：737248」「验证码 737248」「code is 737248」
  // 关键词用非捕获组 → 只有第 1 个捕获组是验证码数字（避免改分支时漏改下标）。
  const kw = /(?:验证码|校验码|动态码|verification\s*code|security\s*code|one[-\s]?time\s*(?:password|code)|\bcode\b)[^0-9]{0,6}([0-9]{4,8})/gi;
  const m = kw.exec(cleaned);
  if (m) return m[1];
  // 退化：验证码被拆成「123 456」
  const spaced = cleaned.match(/\b(\d{3})\s+(\d{3})\b/);
  if (spaced) return spaced[1] + spaced[2];
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

/**
 * 执行一次 Gmail 搜索并返回列表行。
 *
 * ⚠️ 关键坑（2026-11-02 第 59 轮实测）：**只改 URL hash 的 goto 不会触发搜索**。
 * Gmail 是 SPA，`#search/xxx` 只有在**整页重新加载**时才会真正执行查询。
 * 实测对照：
 *   · goto('#search/开放平台')            → 返回 52 行 = **整个收件箱**（搜索没生效）
 *   · 同一 URL 紧接 reload()              → 返回 1 行「找不到与您的搜索匹配的邮件」（搜索生效）
 *   · 之后仅改 hash 到 #search/OKX        → 生效（SPA 此时已 hydrate）
 * 上一版没有 reload，于是「查 3 个查询」实际是「把整个收件箱扫 3 遍」，
 * 再叠加「任意 6 位数字」兜底 → 取到了 Grok Bot 的验证码。
 * 因此这里固定 goto → reload → 等待 的顺序。
 */
async function searchRows(page, q) {
  const url = "https://mail.google.com/mail/u/0/#search/" + encodeURIComponent(q);
  await page.goto(url, { timeout: 25000 });
  await new Promise((r) => setTimeout(r, 2500));
  // reload 是「让搜索真正生效」的关键（Gmail 是 SPA，只改 hash 不触发查询）。
  // 但 Gmail 是重资源页面，reload 偶尔会超时 → 捕获后继续等一次，
  // 因为即便 reload 超时，页面通常也已经渲染出了搜索结果。
  try {
    await page.reload({ timeout: 25000 });
  } catch (e) {
    log(`  （reload 超时，继续读取当前页面：${String(e).slice(0, 60)}）`);
  }
  await new Promise((r) => setTimeout(r, 4500));
  return page.evaluate(() => {
    const out = [];
    for (const tr of document.querySelectorAll("tr")) {
      const t = (tr.innerText || "").replace(/\s+/g, " ").trim();
      if (!t) continue;
      // 发件人邮箱在 span.zF / span.yP 的 email 属性上（列表行有未读态时是 zF，读过是 yP）
      let sender = "";
      const sp = tr.querySelector("span[email]");
      if (sp) sender = sp.getAttribute("email") || "";
      out.push({ text: t.slice(0, 400), sender });
    }
    return out.slice(0, 20);
  });
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
    "from:(workbuddy OR netease OR tencent) newer_than:7d",
    "subject:(验证码 OR 校验码 OR verification code) from:(workbuddy OR netease OR tencent OR qq) newer_than:7d",
    "开放平台 newer_than:7d",
  ];

  // Gmail 搜索无结果时，列表区会出现一行**占位提示**（不是邮件）：
  //   「找不到与您的搜索匹配的邮件。请尝试使用发件人、日期和大小等搜索选项。」
  // 之前把它当成「命中 1 行」，导致日志误报「命中但提取不到验证码」。
  const isPlaceholder = (t) =>
    /找不到与您的搜索匹配|没有与您的搜索匹配|No emails matched|did not match any of your search/i.test(t);

  for (const q of queries) {
    let rows = null;
    try {
      rows = await searchRows(page, q);
    } catch (e) {
      log(`  查询「${q.slice(0, 40)}」失败：${String(e).slice(0, 100)}`);
      continue;
    }
    if (!rows || rows.length === 0) continue;
    rows = rows.filter((r) => !isPlaceholder(r.text));
    if (rows.length === 0) {
      log(`  查询「${q.slice(0, 40)}」无匹配邮件`);
      continue;
    }

    // **发件人白名单**过滤：只保留腾讯体系 / WorkBuddy 自己的邮件。
    // 白名单外的邮件，即使正文含「验证码」也一律不采信。
    const matched = rows.filter((r) => {
      const s = r.sender || "";
      if (SENDER_DENY.test(s)) return false;
      if (SENDER_ALLOW.test(s)) return true;
      // 次选：发件人不在白名单，但整行明确提到开放平台/实名认证
      return PLATFORM_HINT.test(r.text);
    });
    log(`  查询「${q.slice(0, 40)}」命中 ${rows.length} 行，通过发件人白名单 ${matched.length} 行`);
    if (matched.length === 0) continue;

    // 先从列表行（发件人+主题+摘要）里试
    for (const r of matched) {
      const code = extractCode(r.text);
      if (code) {
        log(`CODE:${code}`);
        log(`  来源行：${r.text.slice(0, 150)}`);
        log(`  发件人：${r.sender || "(未知)"}`);
        return;
      }
    }

    // 列表行通常只有主题，看不到正文里的验证码 → 打开**第一封通过白名单的**邮件读正文
    log(`  命中 ${matched.length} 封平台邮件，打开正文读取验证码…`);
    let bodyText = "";
    try {
      const prefix = matched[0].text.slice(0, 60);
      await page.evaluate((pfx) => {
        for (const tr of document.querySelectorAll("tr")) {
          const t = (tr.innerText || "").replace(/\s+/g, " ").trim();
          if (t && t.slice(0, 60) === pfx) { tr.click(); return true; }
        }
        return false;
      }, prefix);
      await new Promise((r) => setTimeout(r, 4000));
      bodyText = await page.evaluate(() => {
        const el = document.querySelector("div.aT5-aG") || document.querySelector("div[role='dialog']");
        return ((el && el.innerText) || document.body.innerText || "").replace(/\s+/g, " ");
      });
    } catch (e) {
      log(`  打开邮件正文失败：${String(e).slice(0, 100)}`);
    }
    const bodyCode = extractCode(bodyText);
    if (bodyCode) {
      log(`CODE:${bodyCode}`);
      log(`  来源：邮件正文`);
      log(`  发件人：${matched[0].sender || "(未知)"}`);
      return;
    }
    log(`  命中 ${matched.length} 封平台邮件，但未提取到验证码`);
  }

  log("CODE:none");
}

main().catch((e) => {
  log("CODE:none");
  log(`异常：${String(e).slice(0, 200)}`);
});
// ---- 平台取码实现结束 ----
