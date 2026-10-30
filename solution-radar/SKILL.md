---
name: solution-radar
display_name: 现成方案雷达
display_name_en: Solution Radar
description: 选型决策助手。当用户问「有没有现成的开源方案」「用什么工具做X」「这个项目能商用吗」「有哪些替代品」时，扫描 GitHub 上的候选项目，输出补齐代价、收益、来源、红线四个维度的选型清单，而不是罗列名字。适合技术选型、开源项目评估、自研还是用现成的判断。
description_zh: 扫描 GitHub 开源项目，输出带「代价/收益/来源/红线」四维的选型决策清单，专治「不知道有什么现成方案」和「不知道能不能商用」。适用于技术选型、开源项目评估、自研 vs 采购判断。
description_en: Scans GitHub for existing open-source solutions and returns a decision checklist with cost, benefit, evidence, and red-line dimensions. Solves "is there an existing tool" and "can I use it commercially".
version: 1.0.0
author: solution-radar
allowed-tools: Bash, Read, Write, WebFetch
---

# 现成方案雷达

## 这个技能解决什么

**用户的真实困境不是"不知道怎么做"，是"不知道有没有现成的、能不能用、值不值得用"。**

典型提问：
- 「有没有现成的开源方案能做 XX？」
- 「A 和 B 哪个更适合我？」
- 「这个 GitHub 项目能商用吗？」
- 「做这个功能要花多久，要不要自研？」

**普通回答的失败模式**：给一串项目名 + star 数。用户拿到之后还是不知道该怎么选——因为缺少「代价」和「红线」这两个决定性维度。

**本技能的做法**：每个方案都补齐四个可核验的决策维度：

| 维度 | 回答什么 | 证据来源 |
|---|---|---|
| **代价** | 我要付出什么 | 许可证类型 + 部署形态 + 维护状态 |
| **能拿到什么** | 它具体提供什么能力 | description + topics 关键词匹配 |
| **来源** | 凭什么信 | star/fork/最后提交/语言/release，全可点开核 |
| **红线** | 什么情况下绝对不能用 | 许可证义务 + 项目状态 |

## 怎么用

### 标准用法

用户问选型问题时，先跑采集与评分：

```bash
# 1. 采集（GitHub 公开 API，标准库即可）
python3 scripts/fetch.py --topics "ai-agent,mcp-server" --min-stars 800 --out data/raw.json

# 2. 评分 + 补齐四维
python3 scripts/score.py --in data/raw.json --out data/entries.json --limit 40

# 3. 渲染成清单
python3 scripts/publish.py --in data/entries.json --outdir site --reports reports --limit 20
```

然后读 `data/entries.json`，用中文表格 + 每条的 decision 字段回答用户。

### 关键规则

**必须做的三件事**：

1. **先问清垂类，再扫描。** 用户说「有没有 AI 工具能做 XX」时，先把 XX 翻译成 2–4 个 GitHub topic（例如「会议纪要」→ `meeting-transcription,speech-to-text,whisper`）。扫描范围太宽会淹掉结果。

2. **许可证必须显式说明，不许含糊。** 这是最容易被忽略、后果最严重的一维：
   - `permissive`（MIT/Apache/BSD）→ 可闭源商用
   - `copyleft-strong`（AGPL/GPL）→ **对外提供服务即触发开源义务**，必须先解决授权
   - `source-available`（BSL/Sustainable Use/Elastic）→ 自用可以，**对外转售或做多租户 SaaS 受限**
   - `custom` / `noassertion` → GitHub 标记为非标准许可证，**必须提示用户读原文**
   - 无许可证 → 默认保留所有权利，**不可商用**

3. **红线优先于分数。** 分数只用来排序，不能替代判断。AGPL 项目哪怕 100 分也要先说清授权义务。

**不该做的三件事**：

- 不要罗列项目名就交差——没补齐四维的清单没有决策价值
- 不要把 star 数当推荐理由——星标高只说明有人关注，不说明适合用户的场景
- 不要替用户下「用这个」的结论——给判断依据，结论由用户结合自己的约束下

## 评分逻辑

`scripts/score.py` 的权重是公开的、可复算的：

| 维度 | 满分 | 说明 |
|---|---|---|
| 星标（对数） | 40 | `min(log10(stars+1)*8, 40)`，避免头部项目单项碾压 |
| 活跃度 | +12 | 30 天内提交 +12，90 天 +6，365 天 -4，更久 -14 |
| 发布纪律 | +6 | 有正式 release |
| 许可证友好度 | +12 / -16 | 宽松 +12，强 copyleft -10，无许可证 -16 |
| 决策价值 | +24 | 命中的能力类型数 × 6，封顶 24 |
| 风险 | -10 / 项 | 早期/实验性标记、已归档 |
| 社区健康 | +6 | fork/star > 0.15 说明有真实用户在改 |

**「社区健康」这一项是刻意加的**：fork/star 比例比 star 绝对值更能说明项目是否真的被用于生产。

## 目录

```
solution-radar/
├── SKILL.md
├── scripts/
│   ├── fetch.py       # GitHub API 采集（标准库，无依赖）
│   ├── score.py       # 四维评分与结构化
│   └── publish.py     # 渲染 HTML / feed.json / markdown 清单
└── references/
    └── license-tiers.md   # 许可证分档与商用边界速查
```

## 数据来源与限制

- **来源**：GitHub 公开 Search + Repository API，全部公开可核。
- **限流**：带 token 5000 次/小时；无 token 60 次/小时（脚本已内置退避重试）。
- **不做的事**：不抓 README 正文、不做语义分析、不看 issue 讨论。所有结论都基于结构化字段——这是为了让每个数字都能被独立复核。
- **已知局限**：star 反映关注度而非适配度；topics 是作者自填；不覆盖非 GitHub 生态（PyPI/npm 独立包、部分闭源商业方案）。
