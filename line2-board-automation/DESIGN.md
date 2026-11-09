# 2号线：看板自动化线 — 设计文档（只设计，不部署）

- **日期：** 2026-11-09
- **状态：** 设计 + 模板已完成；**未注册任何账号、未部署、未要密码或 Token**
- **目标：** 把 5 个看板的“定时抓取 → 存库 → 供数”从空间内置刷新，改成 **GitHub Actions 定时抓 + Supabase 存 + Cloudflare Workers 供数**，空间只负责展示，不重复造轮子。
- **文件：**
  - `DESIGN.md` — 本文档
  - `schema.sql` — Supabase 建表 SQL（含 RLS、去重、过期清理思路）
  - `fetch.yml` — GitHub Actions workflow 模板（部署时放 `.github/workflows/fetch.yml`）
  - `worker.ts` — Cloudflare Workers API TypeScript 骨架
- **零付费最高优先级：** 任何要绑卡的步骤都已在第 1.5 节标出；本设计默认 **不绑卡**，用完免费额度就停，而不是扣费。

---

## 0. 已验证事实 vs 待 Chris 确认的事

### 0.1 已验证事实（有来源，见第 1 节和第 2 节）

- Chris 已有 5 个看板在侧边栏，都是在空间服务上跑、带自动刷新（见第 2 节对照表）。
- Kaggle / Colab 的 GPU 额度、SDXL / Illustrious 的部署状态在个人模型实验室里，不属于本线。
- GitHub Actions / Supabase / Cloudflare Workers 的免费档数字是 2026-11-09 当天核查官方页后写的（第 1 节），不是凭记忆编的；核不到的写了“待核实”。
- 微博未纳入热点板：官方接口拒绝（Forbidden），未用假数据冒充（MEMORY.md，2026-11-07）。
- LINUX DO 抓取有被 Cloudflare 拦的风险（第 7 节）；现有羊毛墙是“单路失败保留旧数据并在页脚标注”。

### 0.2 待 Chris 确认的账号（不要假设他已有）

| 账号 / 资源 | 状态 | 需要 Chris 确认什么 |
|---|---|---|
| GitHub | **已知账号名：snwz9999-ship-it**（MEMORY.md：他曾用该账号 star n8n 等项目）。仓库名、仓库是私人仓还是公开仓，**待确认**。本设计默认私人仓，见第 1.3 节。 | 新建一个私人仓放抓取脚本和 `fetch.yml`，还是用现有仓？仓库名是什么？ |
| Supabase | **是否已有账号、是否已有项目，均待确认。不要假设已有。** | 有没有 Supabase 账号？如果有，项目名 / Project URL 是什么？如果没有，是否同意由他本人在 supabase.com 上注册一个免费项目（不绑卡）？ |
| Cloudflare | **是否已有账号、是否已有 Workers / R2，均待确认。不要假设已有。** | 有没有 Cloudflare 账号？如果有，账号邮箱是否与 Supabase / GitHub 一致？Workers 是否已开通？ |
| Supabase 密钥 | **没有，也不要在聊天里发。** | 部署时由 Chris 本人在 Supabase Dashboard 里复制 `SUPABASE_URL`、`SUPABASE_ANON_KEY`、`SUPABASE_SERVICE_ROLE_KEY`，前两个给 Worker 用（Anon），第三个只放进 GitHub Secrets 给 Actions 用。Muse 不经手真实密钥。 |
| 抓取脚本 | **本次只定了接口契约，未写实现。** | 现有 5 个空间内置的抓取逻辑（URL、解析规则、分类器）需要从现有空间提取后，逐路改写成 `scripts/fetch_source.py`。提取前不要假设 URL 还能用。 |

> **部署前置清单见第 9 节。以上任何一项没确认，都不要开始部署。**

---

## 1. 免费档核查（2026-11-09 当天查官方页）

> 核查方法：先读官方定价 / 限制页原文，再用二手汇总页交叉核对；二手页只作交叉，不作唯一来源。所有数字都标了来源和日期；官方页没写的，写“待核实”，不编。

### 1.1 GitHub Actions 免费档

| 项目 | 免费档（2026-11-09 核查） | 来源与日期 |
|---|---|---|
| 私人仓标准 Runner | 包含的计算用量 **$12 / 月**，按 Linux 2 核 $0.006/分钟折算约 **2,000 分钟/月**。官方现以美元额度表述，不再直接写“2,000 分钟”；2,000 是按官方单价换算的等值，不是官方原话。 | GitHub 官方计费页 `docs.github.com/en/billing/concepts/product-billing/github-actions`，2026-11-09 当天打开；单价表同页：Linux 2 核 $0.006/分钟、Windows $0.010、macOS $0.062 |
| 公开仓标准 Runner | **免费，不计分钟**，无分钟上限（标准 Runner）。大 Runner（larger runners）即使在公开仓也要收费。 | 同上，官方页原文：“Standard GitHub-hosted runners in public repositories”免费；“Larger runners are billed for public repositories” |
| 自托管 Runner | 免费（不计 GitHub 分钟） | 同上 |
| 制品存储 | **500 MB**（Free），与 GitHub Packages 共用额度 | 同上，官方表：GitHub Free Artifact storage 500 MB |
| 缓存存储 | **10 GB / 仓库**，与制品分开算 | 同上，官方表：Cache storage 10 GB per repository |
| 超额后 | **无有效支付方式时，用完额度后任务被拦停，不扣费。** 官方原文：“If your account does not have a valid payment method on file, usage is blocked once you consume the included compute usage.” 有卡时才按单价计费。 | 同上 |
| 定时最小间隔 | **5 分钟**。`*/5 * * * *` 是最密的合法写法；`* * * * *` 不会每分钟跑。 | 二手交叉页（cronuru.com，2026-07-24 核对官方文档；cronradar.com，2026-10-25 核对官方文档）。官方 `schedule` 事件页本次未逐字打开，**以官方页为准，部署前再复核一次** |
| 定时时区 | 默认 UTC；2026 年 3 月起支持每个 cron 条目加 `timezone:`（IANA 名），**二级来源，待官方复核**。本模板为稳妥起见一律写 UTC 换算，不依赖 `timezone:` 字段。 | 二手页 cronuru.com（2026-09-09 更新） |
| 定时延迟 | 高峰期可能延迟，官方明确说定时任务可能被延迟、排队任务在高负载时可能被丢弃；不要指望分秒不差。 | 二手交叉页（同上）；官方表述以 `schedule` 事件页为准 |
| 60 天不活跃停用 | **公开仓**：60 天无仓库活动，定时任务自动停用。私人仓不在该条原文覆盖范围内。 | 二手交叉页（同上）；本设计默认私人仓，且每 2 小时就有抓取提交活动，实际不会触发 |
| 单个 Job 最长 | **待核实**。二手页称标准 Runner 上单个 Job 上限 6 小时、整个 workflow 最长 35 天，本次未在官方限制页逐字核对，**不作为设计依据**。本模板每个 Job 设 `timeout-minutes: 10–15`，远低于该说法，即使该说法不准也不影响。 | 待核实 |
| 并发 | **待核实**。二手页称 Free 档 20 个并发 Job，本次未在官方页逐字核对。本模板用 `concurrency` 串行化，且矩阵最多 4 路并行，即使该数字不准也不影响。 | 待核实 |

### 1.2 Supabase 免费档

| 项目 | 免费档（2026-11-09 核查） | 来源与日期 |
|---|---|---|
| 价格 | **$0 / 月** | Supabase 官方定价页 `supabase.com/pricing`，2026-11-09 当天打开 |
| API 请求 | **无限**（Unlimited API requests） | 同上 |
| 数据库 | **500 MB / 项目**，共享 CPU、500 MB RAM | 同上 |
| 出站流量 | **5 GB / 月 + 5 GB 缓存出站** | 同上 |
| 文件存储 | **1 GB**，单文件上传上限 50 MB | 同上（定价页 Storage 节：1 GB included，Max file upload size 50 MB） |
| Auth 月活用户 | **50,000 MAU** | 同上 |
| 项目数 | **最多 2 个活跃项目** | 同上：“Free projects are paused after 1 week of inactivity. Limit of 2 active projects.” |
| 暂停规则 | **1 周无活动后自动暂停**。暂停后数据库不响应，需在 Dashboard 点 Resume 恢复；官方说数据和配置保留。**不要把本线唯一的库放在一个可能暂停的项目上而不做心跳。** 缓解办法见第 3.5 节：Actions 每 2 小时写库，本身就是活动；另设每周心跳检查。 | 同上；恢复细节（提前约 1 周邮件提醒、可恢复期限）为二手页说法（mykavo.app、greenlightmyapp.com，2026-10-26 核对官方文档），**恢复期限有 90 天 / 1 年两种说法，官方页锚点未统一，以 Dashboard 显示为准** |
| 备份 | 免费档**无自动备份** | 同上（定价页 Free 列无 Daily backups；Pro 才有 7 天备份） |
| 日志保留 | **1 天**（API & Database） | 同上 |
| Realtime | 并发峰值 200，每月 200 万条消息，单条最大 256 KB | 同上 |
| Edge Functions | **500,000 次调用 / 月**（包含额度） | 同上 |
| 是否要绑卡 | **待核实。** 官方定价页本次打开的文本未写 Free 档是否要卡；二手页称 Free 不用卡，**不作为依据，部署时以注册页实际要求为准**。如注册页要求绑卡，停下来问 Chris，不要擅自绑。 | 待核实 |

### 1.3 Cloudflare Workers 免费档

| 项目 | 免费档（2026-11-09 核查） | 来源与日期 |
|---|---|---|
| 请求量 | **100,000 请求 / 天**，UTC 午夜重置；超限返回 **Error 1027** | Cloudflare 官方限制页 `developers.cloudflare.com/workers/platform/limits/`，2026-11-09 当天打开（该页标注更新于 2026-10-29） |
| CPU 时间 | **每次调用 10 ms**（只算 CPU，不算等网络 / 等数据库的时间；等 Supabase 返回不计 CPU） | 同上 |
| 内存 | **128 MB / isolate** | 同上 |
| 子请求 | **50 个 / 每次调用**（`fetch()`、KV、R2、D1 都算） | 同上 |
| Worker 数量 | **100 个 / 账号** | 同上 |
| Cron Trigger 数量 | **5 个 / 账号**（不是每个 Worker 5 个） | 同上；二手交叉页一致 |
| 环境变量 | 64 个 / Worker，每个最大 5 KB | 同上 |
| Worker 体积 | 64 MiB（未压缩） | 同上 |
| 启动时间 | 1 秒 | 同上 |
| Cron 触发的持续时间 | 墙钟最长 **15 分钟**；HTTP 触发无固定墙钟上限（客户端保持连接即可） | 同上 |
| 付费档 | $5 / 月起，含 1,000 万请求 / 月等，**二级来源**（本次未打开官方定价页逐字核对）。本设计不依赖付费档。 | 二手页 budgetforge.dev（2026-08-22 复核官方定价页） |
| 是否要绑卡 | **待核实。** 官方限制页未写 Free 是否要卡；二手页称无卡可开，**不作为依据，部署时以注册页实际要求为准**。如要求绑卡，停下来问 Chris。 | 待核实 |

**结论：三家免费档都够用，但各有一个“先到先得”的短板——GitHub 是分钟数，Supabase 是暂停规则，Workers 是每天 10 万请求 + 每次 10 ms CPU。设计时分别留了护栏（第 1.5 节、第 3.5 节、第 5.4 节）。**

### 1.4 分钟数 / 请求量预算（估算，非官方承诺）

按本模板的触发频率估算（每次抓取按 1–2 分钟计，含启动 Python 和写库）：

| 板 | 频率 | 每天运行次数 | 估算分钟/天 | 估算分钟/月 |
|---|---|---|---|---|
| 热点（4 路并行） | 每 2 小时 | 12 轮 × 4 路 | 约 24–48 | 约 720–1,440 |
| 羊毛墙（4 路并行） | 每 2 小时 | 12 轮 × 4 路 | 约 24–48 | 约 720–1,440 |
| 打新（4 路并行） | 每天 1 次 | 1 轮 × 4 路 | 约 2–8 | 约 60–240 |
| 低门槛复核 | 每天 1 次 | 1 | 约 1–2 | 约 30–60 |
| 清理 | 每天 1 次 | 1 | 约 1–2 | 约 30–60 |
| **合计（按每次 1 分钟的乐观口径）** | — | — | **约 50** | **约 1,500** |
| **合计（按每次 2 分钟的保守口径）** | — | — | **约 100** | **约 3,000（超标）** |

**解读：**

- 私人仓按每次 1 分钟计，约 1,500 分钟/月，**在 2,000 分钟内，但余量只有约 500 分钟**，不能再加高频任务。
- 按每次 2 分钟计会超标。**所以必须把单次抓取控制在 1 分钟左右**：只抓榜单页 / JSON，不做全站爬取；Python 启动后直接跑，不要在 Job 里 `pip install` 大依赖（把依赖打薄，或用缓存）。
- 如果实测单次超过 1.5 分钟，优先降频（热点改 3 小时一更）而不是加钱。
- **公开仓可绕开分钟数限制**（标准 Runner 不计分钟），但仓库内容公开可见，抓取逻辑和来源清单都会公开；除非 Chris 明确同意公开仓，否则默认私人仓。不要为了省分钟数擅自改公开。

Workers 请求量：空间每天的打开量远低于 10 万/天（个人看板），即使每个空间每次打开调 3–5 次接口，也够用。真正的风险不是总量，是**空间每次打开重复调接口**——迁移时给接口加 `Cache-Control: public, max-age=60`（`worker.ts` 已写），空间端也做 15 分钟内的本地缓存，不要每次打开都全量拉。

Supabase 存储：热点每期约 119 条，只保留最近 24 小时快照；羊毛墙按现有量级（正式库提交时 55 条起，7 分类）加每天新增，保留 7 天；打新每天 4 路快照保留 7 天。按每条 1–2 KB 估算，30 天总量在 **10–20 MB** 量级，远低于 500 MB。**不要存图片正文、不要存 `raw` 大字段**，`raw` 只留调试用的最小字段。

### 1.5 绑卡 / 扣费护栏（必读）

| 护栏 | 做法 |
|---|---|
| GitHub 不绑卡 | 不要在 GitHub 账号上加支付方式。无卡时用完 $12 额度后任务被拦停，不扣费（官方说明，第 1.1 节）。**不要为“不断更”去绑卡。** |
| Supabase 不绑卡 | Free 项目不要升级 Pro。如注册页要求绑卡，停下来问 Chris。Pro 是 $25/月起，不在本设计内。 |
| Workers 不绑卡 | Free 档不要升级 Paid（$5/月起）。超 10 万请求/天时返回 Error 1027，空间端要能降级到内置快照（第 5.4 节），不要为不断更去付费。 |
| 密钥不进仓库 | `SUPABASE_SERVICE_ROLE_KEY` 只放 GitHub Secrets，`SUPABASE_ANON_KEY` 只放 Worker Secrets（`wrangler secret put`），**两个 key 都不要写进代码、不要提交、不要发聊天**。 |
| 超额先降频 | 分钟数 / 请求量接近上限时，先降频、减路数，而不是升级。 |

---

## 2. 现有 5 个看板盘点对照表

> 来源：MEMORY.md（2026-11-07 建板记录）+ 侧边栏空间清单（2026-11-09 核对 slug）。**抓取 URL、解析规则、分类器细节不在 MEMORY.md 里，需要从现有空间提取，见“复用”列。**

| # | 看板 | Slug | 数据源 | 刷新频率（现有） | 去重 / 过期规则（现有） | 复用判断 |
|---|---|---|---|---|---|---|
| 1 | 全网实时热点 | `quan-wang-shi-shi-re-dian` | 百度热搜、今日头条热榜、知乎热榜、B站热门（4 路公开榜单），每期约 119 条 | 每 2 小时自动刷新；打开过期自动抓；右上角可手动刷新 | 未在 MEMORY.md 里单独写去重规则；热点是快照型，**每轮全量替换**即可，不长期保留。微博未纳入（官方接口 Forbidden） | **可直接复用**：四路公开榜单的抓取逻辑、榜单结构。**要新写**：把“空间内置抓取”改成“Actions 抓 → 写 `items`（board=hot）→ Worker 供数”；每轮先写新快照再删 24 小时前的旧快照（不要先删后写，避免空窗） |
| 2 | 全网薅羊毛（VPS·AI订阅·Crypto·U卡·海外银行羊毛） | `quan-wang-hao-yang-mao` | NodeSeek、V2EX、LINUX DO、小众软件（4 路），7 分类：VPS、AI订阅、Crypto、U卡、海外银行、免费白嫖、会员优惠 | 每 2 小时自动刷新；打开快照超 2 小时自动抓 | **有效期标注 + 过期删除**：剩不到 3 天标红；长期有效的如实标长期有效；没写的标“原帖未标注有效期 · 看板保留至 …”，保留 7 天。单路失败保留旧数据并在页脚标注。只收优惠帖，问答/吐槽/经历帖不混入 | **可直接复用**：7 分类、有效期标注规则、保留 7 天规则、单路失败保留旧数据的规则。**要新写**：把分类器和有效期解析搬进 `scripts/fetch_source.py`；把“保留 7 天”从展示层改成写入时算好 `expires_at`（见 `schema.sql`）；LINUX DO 的 Cloudflare 拦截处理见第 7 节 |
| 3 | Jettt Unity 打新看板 | `jettt-unity-da-xin-kan-ban` | 四路公开 JSON：Jettt mint、Unity 新盘 launches、Unity 热度榜 ranking（含套利净利与铸造进度）、24h 盈利聪明钱包前 15 | 每日约 09:37 自动刷新；打开快照超 15 分钟自动抓 | 未在 MEMORY.md 里单独写过期规则；打新是快照型，**每天一轮全量替换**即可。只做展示与盯盘，不铸造、不买卖、不给投资建议；风险分是数据源自带，非 Muse 的审计结论 | **可直接复用**：四路公开 JSON 的 URL 与字段映射。**要新写**：Actions 每天 01:37 UTC 抓一次写库；Worker 按 `source_id` 分组返回（`worker.ts` 的 `/jettt` 已按分组写）。**不要改频率**，每天一次足够 |
| 4 | 低门槛薅羊毛 | `di-men-jian-hao-yang-mao` | **人工核查清单，不是爬虫产物**。36 条（MEMORY.md 记载；分类加总：做任务赚钱 9 + 加密货币 8 + 美股券商 6 + 银行卡 9 + 购物 5 = 37，**有一条出入，迁移时以 Supabase 实际条数为准**），另有 8 条避坑红线 | 每天约 10:37 定时复核；过期条目在打开页面和每日复核时自动删除 | 过期自动删除；剩不到 3 天标红；未标注的如实标；顶部结论先行（大陆居民目前没有零成本可提现的加密/美股开户奖励，最现实的是问卷任务平台） | **不可复用抓取逻辑——本来就没有抓取逻辑。** 迁移方式不同：把 36 条清单**一次性导入 Supabase `items`（board=low_threshold，source_id=low_threshold）**，之后只在 Dashboard 里人工增删改；Actions 每天只跑“标 expired”，不删、不抓。不要给这块配爬虫 |
| 5 | AWS 节点额度看板 | `aws-jie-dian-e-du-kan-ban` | AWS Billing / CloudWatch（API 刷新脚本 `~/workspace/aws-refresh/refresh_aws.py`，每天 10:00 / 10:37 跑；MFA 拦截时记 `mfa_required` 并保留上次数值）。余额 $175.41 是 2026-11-09 控制台核查的结转数，无公开 API 可自动同步余额 | 每天刷新（API 脚本 + 控制台复核） | 不适用：不是公开榜单，是账号内数据；余额无 API，靠人工复核 | **不纳入本线。** 原因：需要 AWS 凭证 + boto3 + 可能的 MFA，不是公开 JSON/榜单，不能放进同一个 GitHub Actions 抓取模板；且余额无公开 API，强行“自动同步”是假数据。AWS 板继续走现有 API 脚本 + 控制台复核，不要改 |

**小结：**

- **能直接复用的：** 热点 4 路、羊毛墙 4 路（含分类与有效期规则）、打新 4 路 JSON。共 12 路，复用的是“抓什么、怎么分类、怎么标有效期”，不是代码本身——代码要从空间里提取后改写。
- **要新写的：** Supabase 表与 RLS、Actions 的分 job 与失败处理、Worker 的 5 个接口、清理 job、页脚“某路失败”的状态读取。
- **不纳入的：** AWS 板（账号内数据 + MFA）、低门槛清单的抓取（本来就没有抓取，只有人工清单）。

---

## 3. Supabase 表设计

完整 SQL 见 `schema.sql`。这里只写设计决策，不重复 SQL。

### 3.1 三张表

| 表 | 用途 | 关键字段 |
|---|---|---|
| `sources` | 数据源登记：一路一行，新增来源只加行 | `id`（主键，如 `baidu`）、`name`、`board`、`kind`（rss / public_json / public_html / curated）、`url`、`fetch_interval_minutes`、`enabled`、`last_status`、`last_fetched_at`、`last_error` |
| `items` | 统一内容表：热点 / 羊毛 / 打新 / 低门槛共用 | `id`（uuid）、`source_id`（外键）、`board`、`title`、`url`（全局唯一）、`category`、`rank`、`published_at`、`fetched_at`、`expires_at`、`expires_note`、`status`（active / expired / archived）、`meta`（jsonb）、`raw`（jsonb，最小调试字段） |
| `fetch_logs` | 每次抓取一行，失败也要写 | `id`、`source_id`、`board`、`started_at`、`finished_at`、`status`（ok / partial / failed）、`items_fetched`、`items_upserted`、`items_skipped`、`error`、`duration_ms`、`runner` |

### 3.2 去重：链接全局唯一

- `items.url` 上建 `UNIQUE` 约束。同一链接在两个来源出现时，后到的做 `upsert`（`on conflict (url) do update`），不产生第二条。
- **取舍已写在 `schema.sql` 注释里：** 跨源重复链接只保留第一次写入的 `source_id`。如果 Chris 希望“同一链接在不同来源各保留一条”，改成 `UNIQUE(source_id, url)`，Worker 查询时再按 `url` 去重展示。**默认用全局唯一，因为羊毛墙和热点板最怕同一帖子刷屏。**
- 抓取脚本写入时必须用 `upsert`，不要用 `insert` 后靠报错跳过；`upsert` 时只更新 `fetched_at`、`rank`、`meta`、`updated_at`，不要覆盖人工改过的 `category` / `expires_at`（低门槛清单尤其重要）。

### 3.3 有效期与过期清理思路

- **写入时算好 `expires_at`，不要只在展示层算。** 否则清理 job 和 Worker 查询会各算各的。
  - 原帖标了有效期的：`expires_at` = 原帖有效期。
  - 原帖未标的（羊毛墙）：`expires_at` = `fetched_at + 7 天`，`expires_note` = “原帖未标注有效期 · 看板保留至 YYYY-MM-DD”。
  - 长期有效的：`expires_at = null`，`expires_note = "长期有效"`。
  - 热点 / 打新：`expires_at = null`，靠快照保留期清理，不靠 `expires_at`。
- **清理分两步，不要一步 DELETE：**
  1. 每天复核 job 先把 `expires_at < now()` 且仍为 `active` 的标为 `expired`。
  2. 清理 job 再按保留期删：热点只留 24 小时、羊毛墙 `expired` 超 7 天删、打新只留 7 天、低门槛 `expired` 的**不自动删**（等人工确认）。
- **失败不删旧数据：** 抓取 job 失败时只写 `fetch_logs`，不跑清理、不标 `expired`。清理是单独的 cron（每天 03:07 UTC），与抓取 job 分开，避免某路失败就误删。

### 3.4 抓取脚本的输入 / 输出契约（实现时遵守）

> 本次只定契约，不写脚本实现。`scripts/fetch_source.py` 在部署阶段按此契约写。

- 输入：`--source <source_id> --board <board>`，环境变量 `SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`。
- 输出（每条）：`{title, url, category, rank, published_at, expires_at, expires_note, meta, raw}`；`url` 必填，无 `url` 的条目直接跳过并计入 `items_skipped`。
- 退出码：全部成功或部分成功都返回 0（部分成功时在 `fetch_logs` 记 `partial`）；只有“完全没抓到且没写日志”才算失败。
- 每次运行必须写一行 `fetch_logs`，含 `started_at`、`finished_at`、`status`、`items_fetched`、`items_upserted`、`duration_ms`、`error`（失败时写原始错误的前 500 字，不要写密钥）。
- 写库用 Supabase REST `POST /rest/v1/items?on_conflict=url` + `Prefer: resolution=merge-duplicates`，或用 `supabase-py` 的 `upsert`。不要拼 SQL 字符串。

### 3.5 Supabase 暂停风险的缓解

- **心跳就是抓取本身：** 热点 + 羊毛墙每 2 小时写库，本身就是数据库活动，正常情况下不会触发“1 周无活动”。
- **兜底：** 在 `fetch.yml` 里已有一天多次的写入；另在清理 job 里每天读一次 `sources` 表（读也算活动，但不要只依赖读）。如果连续 7 天没有任何 Actions 运行（例如仓库被停用），Supabase 仍可能暂停——那时在 Dashboard 点 Resume 恢复，数据保留。
- **备份：** 免费档无自动备份。每月手动在 Supabase Dashboard 做一次 `pg_dump` 导出，存到本地 / R2（R2 是 1 号线 / 另案，不要在本设计里默认已开通）。**不要把备份文件提交到 GitHub 仓库。**

### 3.6 RLS 与密钥分工

| 身份 | 能做什么 | 不能做什么 |
|---|---|---|
| Anon Key（Worker 用） | 读 `sources`（已启用的）、读 `items`（`status=active` 的）、读 `fetch_logs` | 写任何表 |
| Service Role Key（Actions 用） | 读写全部（不受 RLS 限制） | **绝不放进 Worker、绝不写进代码、绝不提交到仓库** |
| 空间（浏览器） | 经 Worker 调接口读数据 | 直连 Supabase 写库 |

---

## 4. GitHub Actions 设计

完整模板见 `fetch.yml`。这里只写调度和失败处理。

### 4.1 Cron 时间表（UTC / CST 对照）

| 任务 | Cron（UTC） | 对应 CST | 频率 |
|---|---|---|---|
| 热点 + 羊毛墙 | `7 */2 * * *` | 08:07、10:07、12:07 …（每 2 小时） | 每 2 小时 |
| 打新 | `37 1 * * *` | 每天 09:37 | 每天 1 次 |
| 低门槛复核（只标过期） | `37 2 * * *` | 每天 10:37 | 每天 1 次 |
| 清理 | `7 3 * * *` | 每天 11:07 | 每天 1 次 |

- 一律写 UTC，不用 `timezone:` 字段（该字段为二级来源，待官方复核）。
- 用 `7` 分而不是整点，避开整点高峰，减少延迟。
- 定时任务可能延迟 10–30 分钟，空间端不要按“09:37 整”硬编码展示时间，展示 `fetched_at` 实际时间。

### 4.2 Job 划分

- `fetch-hot`：矩阵 4 路（baidu / toutiao / zhihu / bilibili），`fail-fast: false`，每路 `continue-on-error: true`。
- `fetch-deals`：矩阵 4 路（nodeseek / v2ex / linuxdo / xiaozhong），同上。
- `fetch-jettt`：矩阵 4 路（jettt_mint / unity_launches / unity_ranking / smart_wallets），同上。
- `review-low-threshold`：单 job，只标 `expired`，不删。
- `cleanup`：单 job，`needs: []`（不依赖抓取 job，避免某路失败就跳过清理）；上线第一周先用 `--dry-run-check` 只打印将要删的数量，确认无误后再去掉该参数真正执行。

### 4.3 失败处理（核心规则）

1. **按来源分 job，单路失败不拖垮其他路。** 矩阵 + `fail-fast: false` + `continue-on-error: true`。
2. **失败不删旧数据。** 任何 job 失败时只写 `fetch_logs`（`status=failed`），不跑清理、不标 `expired`、不清空 `items`。
3. **失败要留痕。** `sources.last_status` 改为 `failed` / `degraded`，`last_error` 写原始错误前 500 字；`fetch_logs` 写一行。空间页脚读 `GET /sources?board=…` 展示“某路失败，保留旧数据”，不要只靠 Actions 日志。
4. **部分成功记 `partial`，不要全标失败。** 例如 V2EX 抓到 10 条、其中 2 条解析失败，记 `partial`，`items_fetched=10`，`items_skipped=2`。
5. **重试：** 单次请求失败重试 1 次（指数退避 5 秒、15 秒），再失败即记 `failed`，不要无限重试。不要为绕过反爬而频繁重试，那会被封得更快。

---

## 5. Workers API 设计

完整骨架见 `worker.ts`。这里只写接口与迁移步骤。

### 5.1 最小接口（5 个 GET，全部只读）

| 接口 | 参数 | 返回 | 缓存 |
|---|---|---|---|
| `GET /health` | — | `{ok, has_supabase_url, has_anon_key, time}`，不查库 | 不缓存 |
| `GET /hot?limit=120` | `limit`（默认 120，上限 200） | `{ok, board:"hot", count, items[], sources[]}`，`sources` 是最近各路抓取状态 | 60 秒 |
| `GET /deals?category=&limit=100` | `category` 可选（VPS / AI订阅 / Crypto / U卡 / 海外银行 / 免费白嫖 / 会员优惠），`limit` 默认 100 | `{ok, board:"deals", count, items[], sources[]}` | 60 秒 |
| `GET /jettt` | — | `{ok, board:"jettt", count, bySource:{jettt_mint:[], unity_launches:[], unity_ranking:[], smart_wallets:[]}, sources[]}`，按 `source_id` 分组 | 300 秒 |
| `GET /low-threshold?category=&limit=100` | `category` 可选，`limit` 默认 100 | `{ok, board:"low_threshold", count, items[]}` | 300 秒 |
| `GET /sources?board=hot` | `board` | `{ok, board, sources[]}`，供空间页脚标注失败路 | 60 秒 |

- 全部只查 `status=active` 且 `expires_at` 为空或未过期的条目；已过期但还没被标 `expired` 的，在查询层再拦一道。
- 每次请求只做 1 次 Supabase 查询（`/hot`、`/deals`、`/jettt` 再加 1 次 `fetch_logs` 查询，共 2 次子请求，远低于 50 次上限）。
- 超限 / 上游不可用时返回 `502 {ok:false, error:"Upstream unavailable"}`，**空间端看到 502 就继续展示上一次的内置快照，不要白屏**（第 5.4 节）。
- CORS：`Access-Control-Allow-Origin: *`（公开展示页）；如需收紧，改成空间域名。

### 5.2 Worker 不做的事

- 不抓取、不解析 HTML、不跑分类器（那是 Actions 的活，Worker 的 10 ms CPU 不够）。
- 不写库（写库只走 Actions 的 Service Key）。
- 不配 Cron Trigger 做抓取（免费档全账号只有 5 个 Cron Trigger，抓取已由 Actions 负责，不要浪费）。

### 5.3 空间如何从内置刷新切到调这个接口（分步，不一次性全切）

> **原则：先并行跑 7 天，确认无数据缺口后再逐板切，不要一次性全切。**

| 步骤 | 做什么 | 验收标准 |
|---|---|---|
| Step 0：影子模式 | Supabase + Actions 先跑起来写库，空间**不改**，仍用现有内置刷新。每天对一次数：Worker 返回的条数与空间内置刷新的条数是否一致（允许 ±10% 的波动，来源本身每天有波动）。 | 连续 3 天，热点、羊毛墙、打新三板的条数波动在 ±10% 内，且无整路缺失。 |
| Step 1：试点打新 | 只切 `jettt-unity-da-xin-kan-ban` 一个板，改成调 `GET /jettt`。打新每天只更一次，风险最低，且四路 JSON 最稳定。 | 切后 7 天，每天 09:37 后数据正常，四路分组齐全，无 502 白屏。 |
| Step 2：切热点 | 切 `quan-wang-shi-shi-re-dian`，改成调 `GET /hot`。保留“打开过期自动抓”的按钮，但按钮改为调 Worker（不是直接抓源站）。 | 切后 7 天，每期条数在 100–130 条区间，无整路缺失。 |
| Step 3：切羊毛墙 | 切 `quan-wang-hao-yang-mao`，改成调 `GET /deals?category=…`。有效期标注、剩 3 天标红、页脚失败标注，全部改成读 Worker 返回的 `expires_at` / `expires_note` / `sources`，不要自己再算。 | 切后 7 天，分类 Tab 正常，有效期标红正常，单路失败时页脚能正确标注。 |
| Step 4：切低门槛 | 切 `di-men-jian-hao-yang-mao`，改成调 `GET /low-threshold`。这块的增删改只在 Supabase Dashboard 里做，空间只读。 | 切后 7 天，36 条清单完整，过期标红正常，人工改一条后 5 分钟内能在空间看到。 |
| Step 5：下线内置刷新 | 每个板切后稳定 7 天，再逐个关掉该板的内置刷新逻辑，不要一次性全关。AWS 板**不切**，继续走现有 API 脚本。 | 全部板切完且稳定 7 天后，Actions + Supabase + Workers 成为唯一数据源。 |

### 5.4 降级策略

| 故障 | 空间端做法 |
|---|---|
| Worker 返回 502 / 超时 | 继续展示上一次的内置快照（Step 0–2 期间保留），页脚标“数据为上次快照，Worker 暂不可用”。不要白屏。 |
| Worker 返回 1027（超 10 万请求/天） | 同上；同时检查是否有空间在循环调接口，修掉循环调用后再观察。 |
| Supabase 暂停 | Worker 会返回 502；空间端同上降级。Chris 在 Dashboard 点 Resume 恢复后，Actions 下一轮写入即恢复。 |
| 某路抓取失败 | Worker 返回的 `sources` 里该路为 `failed`，空间页脚标“XX 路失败，保留旧数据”，其他路正常展示。不要整板标失败。 |

---

## 6. 部署顺序（只列顺序，不执行）

1. Chris 确认 GitHub 仓库（私人仓）+ Supabase 项目 + Cloudflare 账号（第 0.2 节、第 9 节）。
2. 在 Supabase SQL Editor 执行 `schema.sql`（Chris 本人执行），确认 RLS 生效（Anon 只能读 `active`）。
3. 把低门槛 36 条清单一次性导入 `items`（board=low_threshold），核对条数。
4. 从现有空间提取 12 路抓取逻辑，改写成 `scripts/fetch_source.py`（按第 3.4 节契约），先在本地跑通一路再提交。
5. 把 `fetch.yml` 放进仓库 `.github/workflows/`，配好 Secrets（`SUPABASE_URL`、`SUPABASE_SERVICE_ROLE_KEY`），先用 `workflow_dispatch` 手动跑一次，确认 `fetch_logs` 有记录。
6. 部署 `worker.ts` 到 Cloudflare（`wrangler secret put SUPABASE_URL / SUPABASE_ANON_KEY`），调 `GET /health`、`GET /hot` 确认通。
7. 按第 5.3 节分步切空间，先打新，再热点，再羊毛墙，再低门槛。

---

## 7. 已知坑与处理

### 7.1 LINUX DO 被 Cloudflare 拦

- **现象：** 直接抓 LINUX DO 可能返回 403 或 Cloudflare Challenge 页（不是正常帖子列表）。
- **处理：**
  - 脚本遇到 403 / Challenge 页时，**直接记 `failed` + `last_status=degraded`，不重试绕过、不换 UA 伪装、不上无头浏览器硬闯**。绕过验证可能违反对方服务条款，也可能导致 IP 被封。
  - 该路失败不影响其他三路；空间页脚标“LINUX DO 路失败，保留旧数据”。
  - 长期方案（待确认）：查 LINUX DO 是否有公开 RSS / API / 搜索接口可用；如有，改走官方接口；如没有，接受该路不稳定，靠 NodeSeek + V2EX 供帖（MEMORY.md 已记：三类主要由 V2EX + LINUX DO 供帖，LINUX DO 不稳时 V2EX 顶上）。
  - **不要为这一路单独加代理、换 IP、买验证码服务。**

### 7.2 微博官方接口拒绝

- **现象：** 微博官方接口返回 Forbidden，已确认（MEMORY.md，2026-11-07）。
- **处理：** **继续不纳入微博**，不要用第三方非官方接口硬抓、不要用假数据冒充。热点板保持百度 / 头条 / 知乎 / B站四路。

### 7.3 其他源站的反爬与变动

| 风险 | 处理 |
|---|---|
| 百度 / 头条 / 知乎 / B站改版、改字段 | 每路独立 job，改版只影响该路；脚本解析失败记 `partial`，不致全败；发现改版后在 `fetch_logs.error` 里留痕，再修解析器。 |
| NodeSeek / V2EX / 小众软件限流 | 请求间隔 2 秒以上，单次只抓列表页，不翻多页；429 时退避重试 1 次，再失败记 `failed`。 |
| Jettt / Unity JSON 地址或字段变更 | 四路 JSON 各自独立；字段缺失时用 `meta` 存原始值，不要因一个字段缺失丢整条。 |
| GitHub Actions 定时延迟 | 接受延迟；空间展示 `fetched_at` 实际时间，不要写死“09:37 整”。 |
| Supabase 暂停 | 见第 3.5 节：Actions 每 2 小时写库即心跳；暂停后 Dashboard 点 Resume。 |
| Workers 超限（1027 / 1102） | 见第 5.4 节降级；不要在 Worker 里加抓取逻辑加重 CPU。 |
| 分类器误判（把问答帖当优惠帖） | 沿用现有分类器规则（只收优惠帖）；误判的条目在 Supabase Dashboard 里把 `status` 改为 `archived`，不要改代码去“修”单条。 |

---

## 8. 不在本次设计范围内的事

- 不注册 GitHub / Supabase / Cloudflare 账号；不部署、不上线。
- 不写 `scripts/fetch_source.py` 的具体实现（只定了契约，第 3.4 节）。
- 不做 1 号线（小夏视频线）的设计。
- 不做 AWS 板的迁移（见第 2 节第 5 行）。
- 不做 R2 / Sakupa 的设计（R2 是 1 号线存图用的，另案）。
- 不处理用户登录、付费、订阅（本线全部是公开展示，无登录）。

---

## 9. 部署前需要 Chris 确认的清单

> 以下任何一项没确认，都不要开始部署。确认时只需要在聊天里回一句话，不用发密码或 Token。

- [ ] GitHub 仓库：用哪个仓库？私人仓还是公开仓？（默认私人仓；已知账号 snwz9999-ship-it，仓库名待确认）
- [ ] Supabase：有没有账号？项目建好了吗？Project URL 是什么？（不要在聊天里发 Service Key）
- [ ] Cloudflare：有没有账号？Workers 开通了吗？（不要在聊天里发 API Token）
- [ ] 低门槛清单的 36 条：以 Supabase 导入后的实际条数为准，是否接受分类加总 37 与总数 36 有一条出入，迁移时核对？
- [ ] `items.url` 全局唯一的取舍：是否接受跨源重复链接只保留第一次写入的 `source_id`？如不接受，改成 `UNIQUE(source_id, url)`。
- [ ] 清理 job 上线第一周是否先用 `--dry-run-check` 只打印不真删？（默认是，先 dry-run 一周）
- [ ] 是否同意 Step 0 先影子跑 7 天再切空间？（默认同意，分步切，不一次性全切）

---

## 10. 来源清单（核查日期均为 2026-11-09）

- GitHub Actions 计费：`docs.github.com/en/billing/concepts/product-billing/github-actions`（官方，2026-11-09 打开）
- GitHub Actions 定时限制：二手交叉页 `cronuru.com/guides/github-actions-scheduled-workflows`（2026-07-24 核对官方文档）、`cronradar.com/blog/github-actions-scheduled-workflows`（2026-10-25 核对官方文档）
- Supabase 定价：`supabase.com/pricing`（官方，2026-11-09 打开）
- Supabase 暂停细节：二手页 `mykavo.app/blog/supabase-free-tier-limits`、`greenlightmyapp.com/blog/supabase-pricing`（2026-10-26 核对官方文档）；恢复期限 90 天 / 1 年两种说法，以 Dashboard 为准
- Cloudflare Workers 限制：`developers.cloudflare.com/workers/platform/limits/`（官方，2026-11-09 打开，页内标注更新于 2026-10-29）
- 现有看板事实：MEMORY.md（2026-11-07 建板记录）+ 侧边栏空间清单（2026-11-09 核对 slug：`quan-wang-shi-shi-re-dian`、`quan-wang-hao-yang-mao`、`jettt-unity-da-xin-kan-ban`、`di-men-jian-hao-yang-mao`、`aws-jie-dian-e-du-kan-ban`）
