# 1号线：小夏视频流水线 — 设计文档

> 日期：2026-11-09
> 状态：**只做设计，不部署、不注册、不付费**
> 目录：`~/workspace/plans/line1-xiaoxia-pipeline/`
> 配套文件：`schema.sql`（Supabase 建表）、`worker.ts`（Workers API 骨架）、`openrouter-example.py`（OpenRouter 免费模型示例）

---

## 0. 先说清楚：哪些是已验证事实，哪些是待确认

### 0.1 已验证事实（不要重编，按原记录）

- **人设与风格锁死**：吉卜力手绘治愈，9:16，15–30 秒，4–6 镜，环境音-only，无配音、无口播。禁栗宝。
  - 小夏 A 版（小红书正版）：栗色微蓬短发、侧边黄色雏菊发夹、圆脸鼻梁淡雀斑、浅黄衫 + 橄榄绿背带裙、旧帆布鞋、小藤编包。
  - 奶奶：银灰短卷发、碎花衫、紫色扣子马甲、棕裤、深色便鞋、藤篮青菜（参考图已锁，`.png`）。
  - 爷爷：Ep4 镜头 1 版为准——银灰短发、圆眼镜、白胡子、浅色衬衫、深蓝马甲、棕裤、深色鞋；不是最初绿马甲版。
  - 无名灰色小猫：灰色身体、四只白爪、毛茸茸小巧，**无心形白毛、无其他装饰性斑纹**（2026-11-02 最高优先级，覆盖此前所有心形描述）。
  - 小夏曼（邻家男孩，约 7 岁）是客人，未锁定，不要自动锁。
- **GPU（主力 + 备胎）**：
  - Kaggle：账号 `chriszhang9999`（`snwz9999@gmail.com`），30 GPU h/周，T4×2（单张 16 GB，不合并），无需绑卡。SDXL Base 1.0 已跑通（2026-11-07，`notebook0e89654478`，T4，30 步，1024×1024）。
  - Colab：同一 Google 账号，2026-11-07 22:25 验证到 Tesla T4 15360 MiB、CUDA 13.0，未开 Pro/Pro+、未绑卡。**SDXL 尚未在 Colab 实跑**，只验证了运行时。
  - Lightning AI 是备用（约 75h T4 / 31h L4，Studio 每 4h 重启），本线不依赖它。
- **模型已跑通**：
  - SDXL Base 1.0 fp16：已跑通。
  - Illustrious XL v1.1 + IP-Adapter Plus：V4 已审过（Codex 2026-11-08 PASS），Ep6 镜头 1 已生成并被 Chris 接受（`episode06_shot01_firefly.png`，720×1280，Steps 30 / CFG 6.5 / Scale 0.6 / seed 20261108，diffusers 0.37.1）。注意 Kaggle 实际数据集挂载路径是 `/kaggle/input/datasets/chriszhang9999/xiaoxia-references`，不是 V4 文件里的旧路径。
  - **硬性教训**：SDXL / Illustrious 提示词 CLIP 上限 77 token，超了会被截断。110 token 的提示词曾把猫和水洼截掉。以后每条提示词先本地实测 token 数再跑 GPU。
- **成片现状**：Ep7《奶奶的番茄汤》30 秒候选成片已做（`~/workspace/xiaoxia-ep7/output-final.mp4`，6 镜×5 秒），待 Chris 定稿。故事集空间现有 12 集规划，Ep5《雨后水洼里的蓝天》为候选未锁。
- **个人模型实验室**：空间已建（2026-11-09，slug `ge-ren-mo-xing-shi-yan-shi`），Kaggle + Colab 两张额度卡、3 条部署记录、5 条 API 研究条目，每天约 09:37 自动刷新。额度无公开接口自动同步，靠手动记录。

### 0.2 待 Chris 确认（不要假设他已有）

| # | 待确认 | 为什么要问 | 如果答案是“没有” |
|---|---|---|---|
| 1 | 是否已有 **Supabase** 账号？有无免费项目？ | 建表、存分镜状态要用到 | 先注册免费账号（见 §1 注意：免费档无需绑卡的说法来自二手核查，注册时以官网为准，**不要绑卡**） |
| 2 | 是否已有 **Cloudflare** 账号？有无 R2 桶、Workers？ | R2 存图、Workers 做 API | 先注册免费账号；R2 桶名本设计暂定 `xiaoxia-assets`，以实际创建为准 |
| 3 | 是否已有 **OpenRouter** 账号？有无 API key？ | 文案生成要用到 | 先注册（无需绑卡可跑 `:free`），key 只放本地 `.env`，绝不发出来 |
| 4 | 是否已有 **Sakupa** 账号 / 已托管的站？ | 对外落地页要用到 | 先确认账号与域名，再决定放什么 |
| 5 | GitHub 账号是否已有（2 号线共用，1 号线暂不依赖） | 本线不强制，写 Workers / 存模板可选 | 没有也不影响 1 号线先跑 |
| 6 | 每集发布平台是否仍为小红书为主，抖音/视频号是否同步？ | 决定 `copy` 表要写几个平台版本 | 默认先只做小红书 1 版，其他平台以后加 |

**红线**：以上任何一步如果出现“绑卡 / 充值 / 升级 Pro”提示，**停下，先问 Chris，不要默认做**。零付费最高优先级。
特别是 OpenRouter 的“一次性买 $10 credits 升到每天 1,000 次”是付费行为，本设计默认不做，先用每天 50 次跑（见 §5）。

---

## 1. 免费档核查（2026-11-09 核查，来源与日期写明）

> 核查方法：公开网页搜索 + 二手来源交叉。**不是官网后台实测**，部署当天必须再对一次官网。
> 凡是写“待核实”的，就是这次没核到的，不要当成已确认。

### 1.1 Supabase 免费档（Free，$0）

| 项目 | 免费档 | 来源与日期 |
|---|---|---|
| 数据库 | 500 MB Postgres | Supabase 定价页二手核查：greenlightmyapp 2026-10-26 06:52 ET 抓取 `supabase.com/pricing`；makerkit 2026-09 核查；growthcab 2026-10-29 读官网。三家一致。 |
| 文件存储 | 1 GB | 同上 |
| 出站流量（egress） | 5 GB/月 + 5 GB 缓存出站 | 同上 |
| 月活用户（MAU） | 50,000 | 同上 |
| 项目数 | 最多 2 个活跃项目 | 同上 |
| Edge Function 调用 | 500,000 次/月 | nayankyada 2026-09 整理；greenlightmyapp 2026-10-26 同口径 |
| API 请求 | 不限次数 | 同上多家一致 |
| 计算 | 共享 Nano，约 0.5 GB 内存 | growthcab 2026-10-29；digijaws 2026-09-29 |
| **最大坑** | **闲置 1 周自动暂停**，需手动恢复；无自动备份 | 多家一致：makerkit / jetadmin 2026-09-26 / greenlightmyapp 2026-10-26 |
| 绑卡 | 二手来源称“免费档无需绑卡” | makerkit、digijaws 2026-09-29 称 no card。**未在官网后台实测，注册时以官网为准** |

**对 1 号线的结论**：500 MB 只存文本和状态足够，图片/视频绝不进库。1 周暂停是已知风险：每周至少打开一次，或用定时任务轻触一次（2 号线做），不要等要用了才发现被暂停。

### 1.2 Cloudflare R2 免费档（Standard，每月重置）

| 项目 | 免费档 | 来源与日期 |
|---|---|---|
| 存储 | 10 GB/月（Standard） | budgetforge 2026-09-29 再核对 `developers.cloudflare.com/r2/pricing`；upstash 2026-10-01 更新；dev.to 2026-10-19。三家一致。 |
| Class A 操作（写/列出/删除） | 1,000,000 次/月 | 同上 |
| Class B 操作（读） | 10,000,000 次/月 | 同上 |
| 出站流量 | $0，免费 | 同上一致 |
| 超出后价格 | 存储 $0.015/GB/月；A 类 $4.50/百万次；B 类 $0.36/百万次 | 同上 |
| **注意** | 免费档**只适用于 Standard**，不适用于 Infrequent Access（后者从第 1 GB 就计费） | budgetforge 2026-09-08 更正说明；upstash 2026-10-01 同口径 |

**对 1 号线的结论**：只用 Standard，不要选 Infrequent Access。10 GB 够存参考图 + 首帧 + 成片一阵子；一集 720×1280 成片约几十 MB，首帧约 1–2 MB，10 GB 不是瓶颈，先不用算超额。

### 1.3 Cloudflare Workers 免费档（Workers Free，$0）

| 项目 | 免费档 | 来源与日期 |
|---|---|---|
| 请求 | 100,000 次/天，UTC 午夜重置，超了报 Error 1027 | Cloudflare 官方 Limits 页（`developers.cloudflare.com/workers/platform/limits/`，搜索结果 2026-10-29 更新）；budgetforge 2026-08-22 再核对官网 |
| CPU 时间 | 每次调用 10 ms 上限（是 CPU 时间，不是墙钟时间；等 fetch 不计） | 同上 |
| 内存 | 128 MB / isolate | 多家 2026-10 整理一致 |
| 子请求 | 每次调用最多 50 个 | toolfreebie 2026-10-02 实测整理；howtoshipit 2026-10 整理 |
| Worker 数量 | 100 个/账号 | anygen 2026-09-09 读官网 |
| 付费档 | $5/月起，含 1,000 万次/月 + 3,000 万 CPU-ms | 同上，**本设计不启用** |

**对 1 号线的结论**：Worker 只做薄转发（见 `worker.ts`），不要在 Worker 里做重计算、图片处理、循环调多次 Supabase。10 ms 很容易超，复杂查询交给 Supabase 视图。

### 1.4 OpenRouter 免费模型（`:free`）

| 项目 | 免费档 | 来源与日期 |
|---|---|---|
| 价格 | `:free` 模型按 token $0 | OpenRouter Limits 文档二手核查：byoklist 2026-10-29 对照官方文档；pinggy 2026-10-27 对照 live API；aitoolspro 2026-10 整理。三家一致。 |
| 速率 | 20 次/分钟（固定，买 credits 也不升） | 同上 |
| 每日上限 | 从未买过 credits：50 次/天；一生累计买过 ≥$10 credits：1,000 次/天；UTC 午夜重置 | 同上 |
| 模型名单 | 会轮换。2026-10-27/28 快照里有 `nvidia/nemotron-3-ultra-550b-a55b:free`、`google/gemma-4-31b-it:free` 等，但 7 月免费的 `gpt-oss-120b`、`qwen3-coder` 到 10 月已不免费 | pinggy 2026-10-27；projedefteri 2026-10-28 |
| 路由 | `openrouter/free` 会自动选当前可用的免费模型，返回里写实际用的模型 | byoklist 2026-11-08（每日拉 API 更新）；anygen 2026-09 |
| 查剩余额度 | `GET https://openrouter.ai/api/v1/key`，看 `free_model_daily_requests` | 同上多家一致 |
| 绑卡 | 注册免费，无需绑卡；$10 充值是**付费**，只升上限，不消耗也算买过 | projedefteri / aitoolspro 2026-10 |
| **注意** | 部分免费模型的上游可能记录/训练提示词（按上游政策），不要把密码、隐私信息放进提示词 | aitoolspro 2026-10 提醒 |

**对 1 号线的结论**：不要写死某一个 `:free` 模型。默认用 `openrouter/free` 路由，并在 `copy` 表里记录实际返回的 `model_used`。一集文案控制在 **1 次调用**出标题+正文+标签（见 `openrouter-example.py`），50 次/天够做约 6–10 集文案，够用。

### 1.5 待核实

- **Sakupa 免费档**：本次未核到官方免费额度、流量上限、绑定域名规则。**待核实**，不要编数字。定位见 §7，部署前再核。
- **Cloudflare 账号注册是否强制绑卡**：R2 / Workers 的免费使用是否要求先绑支付方式，本次未核到一致结论。**待核实**，注册时如遇绑卡提示先停下问 Chris。
- **Supabase 免费项目恢复流程**：暂停后恢复是否丢数据、恢复要多久，本次未核到。**待核实**，不要假设“点一下就回来”。
- **OpenRouter 具体可用模型**：名单每天变，本文列的只是 2026-10-27/28 快照。**以调用当天 `openrouter.ai/models` 为准**。

---

## 2. 流水线总览

```
                    手动（Chris / Muse 在 Kaggle / Colab 上）
   故事 / 分镜
   (故事集空间) ──> 提示词 token 预检 ──> Kaggle T4 跑首帧 ─┐
                                                    Colab T4 备胎 ─┤
                                                                     │
                                                                     ▼
                                                          R2：参考图 / 首帧 / 成片 / 字幕
                                                                     │
                                                                     ▼
                                              Supabase：episodes / shots / assets / copy
                                                                     │  (只存元数据 + 文案，不存二进制)
                                                                     ▼
                                              Workers API：GET /episodes 等（薄转发）
                                                                     │
                                        ┌────────────────────────────┼────────────────────────────┐
                                        ▼                            ▼                            ▼
                              个人模型实验室              故事集空间（内部看板）        Sakupa 对外落地页
                              （额度 + 部署状态）          （集 / 镜进度）               （给别人看的成片页）
                                        │
                                        ▼
                              OpenRouter :free（只做文字）
                              标题 / 正文 / 标签 / 分镜文案
                              → 人工审改 → 写入 copy 表
```

**分工**：

| 环节 | 谁做 | 在哪做 | 备注 |
|---|---|---|---|
| 分镜、提示词 | Muse 起草，Chris 定 | 故事集空间 / 本设计 | 提示词先 token 预检，≤77 才跑 |
| 跑首帧 / 视频 | Chris 在自己手机/电脑上开 Notebook，Muse 给步骤 | Kaggle 主力，Colab 备胎 | 不自动跑、不代操作账号 |
| 上传 R2 | Chris / Muse 按 §4 目录上传 | R2 桶 `xiaoxia-assets`（暂定） | 只传当前采用版 + 保留 1 个上一版 |
| 记状态 | Muse / Chris 改 Supabase 或调 Workers | Supabase / Workers | 状态见 §3 |
| 写文案 | OpenRouter `:free` 起草，人工改 | `openrouter-example.py` | 1 集 1 次调用，见 §5 |
| 发布 | Chris 本人手机发帖 | 小红书 / 抖音 / 视频号 | Muse 不代发帖、不代实名 |

---

## 3. Supabase 表设计

完整 SQL 见 `schema.sql`。这里只讲设计意图。

### 3.1 四张表

| 表 | 一行是什么 | 关键字段 |
|---|---|---|
| `episodes` | 一集 | `episode_no`（唯一，如 `Ep7`）、`title`、`synopsis`、`duration_sec`、`status`（planning / producing / candidate / locked / published） |
| `shots` | 一镜 | `episode_id`、`shot_no`、`description`、`characters[]`、`positive_prompt` / `negative_prompt`、`positive_tokens` / `negative_tokens`、`token_check_pass`、`model_name`、`steps`、`cfg`、`ip_adapter_scale`、`seed`、`status`、`status_note`、`qc_note` |
| `assets` | 一个文件 | `episode_id`、`shot_id`（可空，成片挂集上）、`kind`（reference / first_frame / video / subtitle / audio / other）、`r2_key`、`r2_bucket`、`sha256`、`is_current`、`source`（kaggle / colab / musebox） |
| `copy` | 一条文案 | `episode_id`、`platform`（xiaohongshu / douyin / shipinhao）、`copy_type`（title / body / tags / storyboard_text）、`content`、`model`、`model_used`、`prompt_tokens`、`completion_tokens`、`edited_by_human`、`is_current` |

### 3.2 分镜状态机

```
todo（待做） → first_frame（首帧） → video（视频） → candidate（候选） → locked（已锁） → published（已发）
```

- 允许回退：`locked → candidate`、`candidate → video`。回退时在 `status_note` 写原因，如“第 2 镜多一只手，退回重做”。
- `locked` 的定义：Chris 亲口说“锁了 / 就这样 / 不用调了”才算锁。候选不是锁。
- `published` 的定义：已在平台发出，有笔记/视频 ID 的在 `episodes` 备注或 `copy` 里记，不要只凭感觉标已发。

### 3.3 为什么这样分

- **提示词 token 单独记**：`positive_tokens` / `negative_tokens` 是实测数，不是估算。`token_check_pass` 只有两边都 ≤77 才为 true。跑 GPU 前先查这行。
- **资产只存 R2 路径**：`assets` 不存二进制，只存 `r2_key`（如 `xiaoxia/ep07/shot01/first-frame.png`）。同一镜多版本时，只有一版 `is_current=true`，锁定后填 `sha256` 防误覆盖。
- **文案记模型与用量**：`copy.model` 写请求时用的模型（建议 `openrouter/free`），`model_used` 写实际返回的模型，`prompt_tokens` / `completion_tokens` 写返回的用量。方便按 50 次/天算账，也方便以后发现某模型写得差时回溯。
- **视图 `v_episode_progress`**：每集总镜数 / 已锁 / 已发 / 待做，供 Workers 和内部看板直接读，不用在 Worker 里算。

### 3.4 安全（先跑通内部流程）

- 本文件默认 **不开启 RLS**，先在内部用。**对外开放前必须再加 RLS + 只读 anon key 策略**，不要直接把 Supabase URL + anon key 发给别人。
- 任何 key、密码都不进库、不进 Git。Supabase 的 `service_role` key 只放 Workers Secret。

---

## 4. R2 目录结构

桶名暂定：`xiaoxia-assets`（待 Chris 确认实际桶名）。
只用 Standard 存储（见 §1.2，Infrequent Access 没有免费档）。

```
xiaoxia-assets/
├── references/                          # 参考图（人物/场景，全系列共用）
│   ├── xia/
│   │   └── xiaoxia-character-reference.jpg      # 小夏 A 版锁定参考图
│   ├── grandma/
│   │   └── grandma-reference.png                # 奶奶锁定参考图
│   ├── grandpa/
│   │   └── grandpa-reference.png                # 爷爷锁定参考图（Ep4 镜头 1 版）
│   ├── grandpa-ep4-shot1/
│   │   └── grandpa-ep4-shot1-reference.png      # Ep4 镜头 1 爷爷特写（如有）
│   ├── cat/
│   │   └── cat-reference.png                    # 猫参考图（待补：目前无独立猫参考图）
│   └── xiaoman/
│       └── xiaoman-reference.png                # 小夏曼参考图（未锁定，仅作参考）
│
├── ep01/
│   ├── shot01/
│   │   ├── first-frame.png
│   │   ├── first-frame-v2.png                 # 多版本时加 -v2，is_current 只留一版
│   │   ├── video.mp4
│   │   └── video-v2.mp4
│   ├── shot02/
│   │   └── ...
│   ├── final/
│   │   ├── output-final.mp4                   # 成片（锁定后填 sha256 进 assets 表）
│   │   └── output-final-v2.mp4
│   └── subtitles/
│       ├── dialogue.srt                       # 对白字幕（对话原话，按说话时间）
│       └── dialogue.vtt
│
├── ep02/
│   └── ...（同 ep01 结构）
│
├── ep07/                                      # 例：《奶奶的番茄汤》
│   ├── shot01/
│   │   ├── first-frame.png
│   │   └── video.mp4
│   ├── shot02/
│   ├── shot03/
│   ├── shot04/
│   ├── shot05/
│   ├── shot06/
│   ├── final/
│   │   └── output-final.mp4
│   └── subtitles/
│       └── dialogue.srt
│
└── _incoming/                                 # 临时上传区，确认后再移到 epXX/
    └── README.txt                             # 写明：_incoming 不作为正式路径，7 天清理
```

**规则**：

1. **按集 / 镜分，参考图、首帧、成片、字幕分开**。不要把所有图堆在一个文件夹。
2. **文件名固定**：首帧一律 `first-frame.png`，分镜视频一律 `video.mp4`，成片一律 `output-final.mp4`，对白字幕一律 `dialogue.srt`。多版本加 `-v2`、`-v3`，不要改主名。
3. **字幕是对话原话**，不是场景描述。按 Chris 2026-10-29 的规则：字幕 = 角色说的话，按说话时间对齐，放在下面。
4. **不要把 Kaggle 的 `/kaggle/working/` 路径当成 R2 路径**。Kaggle 是临时工作区，R2 是长期存放处。上传后在 `assets` 表里记 `r2_key` + `source=kaggle/colab`。
5. **成片锁定后算 SHA-256**，填进 `assets.sha256`，以后任何覆盖都能发现。

---

## 5. OpenRouter 调用设计（只走免费模型）

完整可跑示例见 `openrouter-example.py`。这里只讲设计。

### 5.1 只做什么

- 标题、正文、标签、分镜文案。**不做图片生成，不做视频生成，不调付费模型。**
- 系统提示词里锁死人设（小夏 A 版、奶奶、爷爷、猫四白爪无心形、禁栗宝），防止模型改人设。

### 5.2 模型选择

- **默认用 `openrouter/free` 路由**，不写死某一个 `:free` 模型。名单会轮换，写死了下个月可能就不免费了。
- 返回里一定要记 `model_used`（实际用的模型）和 token 数，写入 `copy` 表。
- 调用前先在 `openrouter.ai/models` 上搜 `free` 确认当天仍有免费模型；不要凭本文 2026-10 月的快照直接用。

### 5.3 次数预算（50 次/天）

| 用途 | 每次调用数 | 备注 |
|---|---|---|
| 一集文案（标题+正文+标签） | 1 次 | 一次出完，不要拆成 3 次 |
| 分镜文案润色（一集） | 1 次 | 可选，不是每集都做 |
| 重试（模型没按 JSON 返回） | 1 次 | 最多重试 1 次，之后人工整理 |
| **一集合计** | **1–3 次** | 50 次/天 ≈ 每天可做 10 集以上文案，够用 |

**不要**用 OpenRouter 做高频调用（如每镜 1 次、每标题 1 次）。次数花完会报 429，等 UTC 午夜重置。

### 5.4 调用示例（摘要，完整版见 `openrouter-example.py`）

```bash
export OPENROUTER_API_KEY="YOUR_API_KEY"  # 占位，换成自己的 key，只放环境变量
python3 openrouter-example.py --episode Ep7 --platform xiaohongshu
python3 openrouter-example.py --check-only   # 只查剩余次数，不耗次数
```

Python 示例用标准库 `urllib`，不依赖 `requests` / `openai` 包，`POST https://openrouter.ai/api/v1/chat/completions`，头带 `Authorization: Bearer $OPENROUTER_API_KEY`。

**流程**：先 `GET /key` 查剩余 → 再决定要不要花这 1 次 → 生成后人工审改 → 采用版手动写入 `copy` 表（`is_current=true`，`edited_by_human=true` 如改过）。**不自动写入**。

### 5.5 失败处理

- `429`：免费次数用完或 20 次/分钟限流。等一等，不要循环重试。
- `402`：余额为负，免费模型也会被停用。先处理余额，**不要直接改调付费模型**。
- 模型没按 JSON 返回：人工整理，不要重试超过 1 次。

---

## 6. Workers API 设计（最小接口）

完整骨架见 `worker.ts`。这里只讲接口表。

| 方法 | 路径 | 用途 | 鉴权 | 说明 |
|---|---|---|---|---|
| GET | `/health` | 健康检查 | 无 | 不碰数据库 |
| GET | `/episodes` | 集列表 + 每集进度 | 无（内部用） | 可加 `?status=candidate` 过滤；读视图 `v_episode_progress` |
| GET | `/episodes/:id` | 单集详情 | 无（内部用） | `:id` 可为 uuid 或 `Ep7` |
| GET | `/episodes/:id/shots` | 某集全部分镜（按 `shot_no` 升序） | 无（内部用） | 含提示词 token、状态、参数 |
| GET | `/episodes/:id/assets` | 某集资产（只返回 `r2_key` 等元数据） | 无（内部用） | 不返回文件本身 |
| POST | `/shots/:id/status` | 更新单个分镜状态 | `Authorization: Bearer <WRITE_TOKEN>` | 体：`{"status":"locked","status_note":"..."}`；状态必须在白名单内 |

**设计约束**：

- **薄转发**：Worker 里只做参数校验 + 转发 Supabase REST，不做重计算。复杂聚合交给 Supabase 视图。
- **写接口要口令**：`WRITE_TOKEN` 用 `wrangler secret put WRITE_TOKEN` 单独放，不要写进代码。这只是防随手改，不是正式鉴权；对外开放前换成 Supabase Auth / RLS。
- **不要在 Worker 里拼 R2 签名 URL**：本骨架只返回 `r2_key`，需要公开访问时再加 R2 绑定 + 域名，不要默认公开整个桶。
- **不要在 Worker 里调 OpenRouter**：文案生成在本地跑 `openrouter-example.py`，不在 Worker 里烧 OpenRouter 的 50 次/天。

**以后可加（本设计不做）**：`POST /episodes` 新建集、`POST /shots` 新建分镜、`GET /copy?episode=Ep7&platform=xiaohongshu` 查文案。先跑通读 + 改状态，再加写接口。

---

## 7. Sakupa 静态托管：只写定位，不展开部署

- **定位**：对外落地页。给别人看的成片页、系列介绍页、提示词包介绍页，放 Sakupa。
- **不定位**：内部看板、额度管理、状态管理。那些是个人模型实验室 / 故事集空间的事，不搬到 Sakupa。
- **与空间的关系**：Muse 空间适合内部看、自己管理；Sakupa 适合对外分享链接。不要两边各做一套同样的页。
- **本次不展开**：Sakupa 免费档、绑定域名、部署步骤均**待核实**，等 Chris 确认账号后再写部署文档。本文不编 Sakupa 的额度数字。

---

## 8. 每集工作流（按状态走）

```
planning（规划）
  └─ Muse 起草分镜 + 提示词 → token 预检（≤77）→ 写入 shots（status=todo）
todo（待做）
  └─ Chris 在 Kaggle（主力）/ Colab（备胎）跑首帧 → 上传 R2 → assets 记 first_frame
     → shots 状态 → first_frame
first_frame（首帧）
  └─ Chris 确认首帧 → 跑分镜视频 → 上传 R2 → shots 状态 → video
video（视频）
  └─ Muse + Chris 看 5–10 秒、16–23 秒等关键段 → 无大问题 → 拼成片 → shots 状态 → candidate
candidate（候选）
  └─ Chris 全片看一遍 → 说“锁了 / 就这样” → 成片 sha256 记入 assets → shots 状态 → locked
locked（已锁）
  └─ OpenRouter 写文案（1 次调用）→ 人工审改 → 写入 copy → Chris 手机发帖
     → shots 状态 → published，episodes 状态 → published
published（已发）
  └─ 在 episodes / copy 里记平台笔记 ID、播放数据（以后再补，不在本设计里展开）
```

**每集检查点**：

- 首帧前：token 检查通过了吗？人物有没有混设计？猫有没有心形？
- 视频后：有没有多手、多人、重复包、人物变矮/变高？
- 候选后：Chris 有没有亲口说锁？没说就不标 `locked`。

---

## 9. 成本与红线

| 项目 | 免费档是否够 | 红线 |
|---|---|---|
| Kaggle T4 | 30h/周，够做首帧 + 批量 | 不要绑卡；额度用完等下周，不要买 |
| Colab T4 | 备胎，分配不保证 | 不要开 Pro/Pro+，不要绑卡 |
| Supabase | 500 MB 文本够用 | 闲置 1 周会暂停；不要超 2 个项目；不要升 Pro（$25/月） |
| R2 | 10 GB Standard 够用 | 只用 Standard，不要用 Infrequent Access；不要超 10 GB |
| Workers | 10 万次/天够用 | 不要在 Worker 里做重计算；不要升 Paid（$5/月） |
| OpenRouter `:free` | 50 次/天够做文案 | **不要一次性买 $10 升 1,000 次**（那是付费，默认不做）；不要调付费模型 |
| Sakupa | 待核实 | 部署前先核免费档，不要绑卡 |

**任何一步出现“绑卡 / 充值 / 升级”提示，先停下问 Chris。**

---

## 10. 待 Chris 确认清单（动手前逐项打勾）

- [ ] Supabase：有无账号？有无免费项目？项目 URL 是？
- [ ] Cloudflare：有无账号？R2 桶名定了吗（暂定 `xiaoxia-assets`）？Workers 有无？
- [ ] OpenRouter：有无账号？有无 API key？key 存在哪（只放本地 `.env`）？
- [ ] Sakupa：有无账号？对外落地页放什么内容？
- [ ] 每集发布平台：小红书为主？抖音/视频号是否同步？
- [ ] 先从哪集开始试跑这条线：Ep7（已有候选成片）还是下一集新集？
- [ ] R2 里的参考图：是否按 §4 目录先传小夏/奶奶/爷爷三张锁定图？猫参考图目前缺，是否先空着？

**以上未确认前，不要注册、不要建表、不要部署。** 本文档 + 三个模板文件就是本次的全部产出。

---

## 附：文件清单

| 文件 | 用途 |
|---|---|
| `DESIGN.md`（本文） | 设计 + 核查 + 工作流 + 待确认清单 |
| `schema.sql` | Supabase 建表 SQL（4 张表 + 视图 + 触发器），在 Supabase SQL Editor 执行 |
| `worker.ts` | Cloudflare Workers API 骨架（5 个接口 + 健康检查），TypeScript |
| `openrouter-example.py` | OpenRouter 免费模型示例（1 集文案 1 次调用），Python 标准库，无第三方依赖 |

---

*核查日期：2026-11-09。免费档数字以官网为准，部署当天再对一次。*
