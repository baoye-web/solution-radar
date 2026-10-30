# 现成方案雷达 · Solution Radar

> 选型决策助手。扫描 GitHub 上的现成方案，输出补齐 **代价 / 收益 / 来源 / 红线** 四个维度的决策清单。
> 不是罗列项目名——是让人能直接做决定。

[![每日更新](https://github.com/baoye-web/solution-radar/actions/workflows/radar-daily.yml/badge.svg)](https://github.com/baoye-web/solution-radar/actions/workflows/radar-daily.yml)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

## 为什么做这个

「有没有现成的开源方案能做 XX？」——这是每个做技术选型的人都问过的第一个问题。

但普通的回答方式（给一串项目名 + star 数）有个致命缺陷：**它回答不了「我能不能用」。**

星标数告诉你的是"有多少人关注"，不是：
- 我要付出什么代价（授权？自建服务器？自己维护？）
- 它到底能给我什么（"强大的 Agent 框架"不是答案）
- 凭什么信（可核验的证据链在哪）
- 什么情况下绝对不能用（AGPL 的网络开源义务、无许可证、已弃用）

**这个项目补的就是这四维。** 每个方案都带完整的代价/收益/来源/红线，证据全部可点开核验。

## 快速开始

不需要安装任何依赖，Python 3.9+ 即可：

```bash
# 1. 采集（GitHub 公开 API）
python3 radar/fetch.py --topics "ai-agent,mcp-server" --min-stars 800 --out data/raw.json

# 2. 评分 + 补齐四维
python3 radar/score.py --in data/raw.json --out data/entries.json --limit 40

# 3. 渲染清单
python3 radar/publish.py --in data/entries.json --outdir site --reports reports --limit 20
```

产物：
- `site/index.html` —— 静态站，可直接托管
- `site/feed.json` —— 结构化订阅源
- `reports/YYYY-MM-DD.md` —— 当日决策清单

## 四维决策清单

每个条目固定回答四个问题：

| 维度 | 内容 | 证据 |
|---|---|---|
| **代价** | 许可证成本、基础设施成本、维护成本 | LICENSE 文件类型 + 部署形态 + release 状态 |
| **收益** | 具体能力，不是形容词 | description + topics 关键词匹配 |
| **来源** | 凭什么信 | star / fork / 最后提交 / 语言，全可点开 |
| **红线** | 什么情况下绝对不能用 | 许可证义务 + 项目存续状态 |

### 许可证分档是核心

选型里最不可逆的决策就是许可证。本项目把它做成硬分类：

| 档 | 许可证 | 商用边界 |
|---|---|---|
| 宽松 | MIT / Apache-2.0 / BSD | 可闭源商用 |
| 弱 Copyleft | LGPL / MPL-2.0 | 有限制的可商用，改动边界要盯 |
| **强 Copyleft** | **AGPL / GPL** | **对外提供服务即触发开源义务** |
| **Source Available** | **BSL / Sustainable Use / Elastic** | **自用可以，转售与多租户 SaaS 受限** |
| 无许可证 | 无 LICENSE 文件 | 默认保留所有权利，不可商用 |

完整速查表见 [`skill/references/license-tiers.md`](skill/references/license-tiers.md)。

**特别提示**：GitHub 返回 `spdx_id = "noassertion"` 表示「自定义许可证」——n8n（Sustainable Use）、dify（Dify Open Source License）都在这一档。它们**看起来开源友好，但商用边界跟 Apache 完全不同**，最容易被误判。

## 评分逻辑

公开、可复算，没有黑箱：

| 维度 | 分值 | 说明 |
|---|---|---|
| 星标（对数压缩） | 0–40 | `min(log10(stars+1)*8, 40)` |
| 活跃度 | +12 / -14 | 30 天内提交 +12；超 365 天 -14 |
| 发布纪律 | +6 | 有正式 release |
| 许可证友好度 | +12 / -16 | 宽松 +12；强 copyleft -10；无许可证 -16 |
| 决策价值 | 0–24 | 命中能力类型数 × 6 |
| 风险 | -10 / 项 | 早期标记、已归档 |
| 社区健康 | +6 | fork/star > 0.15 |

**「社区健康」是刻意加的**：fork/star 比例比 star 绝对值更能说明项目是否真的被用在生产里。

**分数只用来排序，不用来替代判断。** 星标高 ≠ 适合你的场景。

## 自动化

`.github/workflows/radar-daily.yml` 每天 UTC 01:47 自动跑完整链路：采集 → 评分 → 渲染 → 生成分发文案 → 提交 → 部署 GitHub Pages。

失败时会把 `data/` 与 `reports/` 作为 artifact 留存 7 天，方便事后排查而不是"什么都没发生"。

### 分发层

`radar/distribute.py` 把当日清单渲染成四个平台可直接发布的文案：

| 平台 | 形态 | 切入角度 |
|---|---|---|
| X | 3 条独立短帖，每条聚焦一个方案 | 单方案深挖，带可点开链接 |
| 知乎 | 结构化长文 | 授权边界对比 + 完整清单 |
| 小红书 | 避坑向短文 | 「这几个别用，这几个放心用」 |
| 微博 | 单条 | 当日最值得看的一个 |

```bash
python3 radar/distribute.py --in data/entries.json --out out --pick 6
```

**刻意不做的事**：不生成通用清单体（已被证实是红海），每条内容必须带可核验的具体数字。

### 收入通道

| 通道 | 需要认证 | 说明 |
|---|---|---|
| **GitHub Sponsors** | 绑 Stripe/PayPal | `.github/FUNDING.yml` 已配置。GitHub 直接结算，**不经过 WorkBuddy 平台** |
| **WorkBuddy 付费 Skill** | 实名 + 微信签约 | 见 [上架操作手册](docs/上架操作手册.md) |
| **付费数据 API** | 实名 | 未来把 `site/feed.json` 包成订阅接口 |

## 作为 Skill 使用

本仓库同时是一个 WorkBuddy Skill（`solution-radar/`），可上架到开放平台。

```bash
# 本地预检 + 打包（不上传）
ego-browser nodejs < solution-radar/scripts/publish.mjs

# 真实上架（含 --wait 会在实名认证完成后自动继续）
ego-browser nodejs < solution-radar/scripts/publish.mjs -- --submit --publish --price 19 --monthly
```

或者用守护进程（认证完成后自动接手，无需你再操作）：

```bash
bash scripts/auto-publish.sh --daemon --timeout=7200 --interval=25
tail -f ~/ego/auto-income-20261030/daemon.log
```

脚本会做本地预检（8 个必填 frontmatter 字段、目录结构、体积、脚本执行位），**预检不过就不浪费一轮审核**。

> **技术约束**：上架链路必须在浏览器上下文中执行（`page.fetch`）。实测从 Chrome 提取的 httpOnly `session` cookie 在浏览器外用 urllib 直连会被重定向到 Keycloak 登录页，同一份 cookie 在页面上下文里完全正常。

## 项目结构

```
solution-radar/
├── radar/                   # 管线本体（可独立运行）
│   ├── fetch.py             # GitHub API 采集（纯标准库）
│   ├── score.py             # 四维评分与结构化
│   └── publish.py           # 渲染 HTML / feed.json / markdown
├── solution-radar/          # 可上架的 Skill 包
│   ├── SKILL.md             # 含 8 个平台必填 frontmatter 字段
│   ├── references/
│   │   └── license-tiers.md # 许可证分档速查
│   └── scripts/             # 管线脚本 + 自动上架脚本
├── site/                    # 静态站产物
├── reports/                 # 每日清单归档
└── .github/workflows/       # 每日自动更新
```

## 设计约束

这些是刻意的取舍，不是遗漏：

- **不抓 README 正文、不做语义分析、不看 issue 讨论。** 所有结论基于结构化字段——这是为了让每个数字都能被第三方独立复核。
- **不替用户下结论。** 给判断依据和红线，结论由用户结合自身约束决定。
- **许可证必须显式分类，不许含糊。** 宁可标"需人工核对"，也不能给一个可能错误的"可商用"。
- **星标是参考，不是推荐理由。**

## 已知局限

- star 反映关注度而非适配度；topics 是作者自填，可能缺失或过时
- 不覆盖非 GitHub 生态（PyPI/npm 独立包、部分闭源商业方案）
- 不做漏洞扫描与安全评估——**分数高不代表安全**，接入前请自行审计
- 评分权重是经验值，可自行调整（`radar/score.py` 顶部常量全部可改）

## License

MIT
