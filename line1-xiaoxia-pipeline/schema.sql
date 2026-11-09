-- ============================================================
-- 小夏视频流水线 1号线 — Supabase 建表 SQL
-- 文件：schema.sql
-- 日期：2026-11-09（设计版，未部署、未执行）
-- 用途：在 Supabase SQL Editor 中粘贴执行，建 4 张表。
-- 前提（待 Chris 确认）：已有一个 Supabase 免费项目，
--   且已确认免费档 500 MB 数据库够用（见 DESIGN.md §1）。
-- 注意：
--   - 只存元数据和文本，不存图片/视频二进制。
--     图片/视频只存 R2 路径（见 assets.r2_key）。
--   - 不存任何 API key、密码。OpenRouter key 只放
--     Cloudflare Workers Secret / 本地 .env，绝不进库。
--   - RLS 本文件默认关闭（先跑通内部流程用）。
--     对外开放前必须再加 RLS + 只读 anon key 策略，
--     本设计不默认对外公开。
-- ============================================================

-- ---------- 枚举：分镜状态 ----------
-- 待做 -> 首帧 -> 视频 -> 候选 -> 已锁 -> 已发
-- 允许回退：已锁可回退到候选，候选可回退到视频。
create type shot_status as enum (
  'todo',        -- 待做
  'first_frame', -- 首帧
  'video',       -- 视频
  'candidate',   -- 候选
  'locked',      -- 已锁
  'published'    -- 已发
);

-- ---------- 枚举：资产类型 ----------
create type asset_kind as enum (
  'reference',   -- 参考图（人物/场景）
  'first_frame', -- 首帧图
  'video',       -- 分镜视频 / 成片
  'subtitle',    -- 字幕文件（.srt / .vtt）
  'audio',       -- 环境音 / 配音（本线当前为环境音-only）
  'other'
);

-- ---------- 1. episodes：集 ----------
create table if not exists episodes (
  id            uuid primary key default gen_random_uuid(),
  -- 集号：Ep1、Ep2 … 与故事集空间的集号保持一致。
  -- 建议用 text 而不是 int，方便以后出现 Ep6.5 / Ep7-番外。
  episode_no    text not null unique,
  title         text not null,
  -- 如《奶奶的番茄汤》《雨后水洼里的蓝天》
  synopsis      text,
  -- 每集固定 5 镜 x 5 秒 = 25 秒；Ep7 为 6 镜 x 5 秒 = 30 秒。
  -- 长度只作记录，不做强制校验。
  duration_sec  int,
  -- 风格锁死：吉卜力手绘治愈；禁栗宝。
  style_note    text default '吉卜力手绘治愈，9:16，环境音-only，无配音、无栗宝',
  -- 发布平台：小红书 / 抖音 / 视频号，可多选存数组。
  platforms     text[] default '{}',
  -- 集状态与分镜状态分开：集整体进度。
  status        text not null default 'planning'
                check (status in ('planning','producing','candidate','locked','published')),
  publish_date  date,
  created_at    timestamptz not null default now(),
  updated_at    timestamptz not null default now()
);

comment on table episodes is '小夏系列：集。1 集 = 多镜，状态为集整体进度。';
comment on column episodes.episode_no is '集号，如 Ep7，与故事集空间保持一致，唯一。';

-- ---------- 2. shots：分镜 ----------
create table if not exists shots (
  id              uuid primary key default gen_random_uuid(),
  episode_id      uuid not null references episodes(id) on delete cascade,
  -- 镜号：1..5 或 1..6，从 1 开始连续。
  shot_no         int not null,
  -- 分镜一句话，如：奶奶在菜园摘番茄，小夏提篮跟在后面。
  description     text not null,
  -- 人物：只存代号，如 xia / grandma / grandpa / cat / xiaoman。
  -- 人物设计以各自锁定参考图为准，禁止在提示词里混设计。
  characters      text[] default '{}',

  -- --- 提示词与 token 预检（SDXL / Illustrious 的硬性教训）---
  -- SDXL CLIP 上限 77 token，超了会被截断。
  -- positive_prompt_tokens / negative_prompt_tokens 由
  -- sdxl_prompt_token_preflight 本地实测后填入，禁止凭感觉填。
  positive_prompt text,
  negative_prompt text,
  positive_tokens int check (positive_tokens is null or positive_tokens >= 0),
  negative_tokens int check (negative_tokens is null or negative_tokens >= 0),
  -- token 检查是否通过（两边都 <=77 才算通过）。
  token_check_pass boolean default false,

  -- --- 生成参数（以实际跑通的参数为准）---
  -- 如 SDXL：steps 30 / cfg 6.5；Illustrious：steps 30 / cfg 6.5 / ip_adapter_scale 0.6
  model_name      text,  -- 如 sdxl-base-1.0 / illustrious-xl-v1.1
  steps           int,
  cfg             numeric(4,1),
  ip_adapter_scale numeric(3,2),
  seed            bigint,

  -- --- 状态机 ---
  status          shot_status not null default 'todo',
  -- 状态变更备注：如“第 2 镜多一只手，退回视频重做”。
  status_note     text,
  -- 质量备注：如“shot5 小夏曼明显高于小夏，Chris 说以后再说”。
  qc_note         text,

  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),

  unique (episode_id, shot_no)
);

comment on table shots is '分镜：1 集多镜。状态：todo / first_frame / video / candidate / locked / published。';
comment on column shots.positive_tokens is '正向提示词实测 token 数，CLIP 上限 77，超了必须缩短，禁止凭感觉填。';
comment on column shots.token_check_pass is '正负提示词都 <=77 才为 true。';

-- 状态变更时间戳：每次更新 updated_at（用触发器）
create or replace function set_updated_at()
returns trigger as $$
begin
  new.updated_at = now();
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_episodes_updated_at on episodes;
create trigger trg_episodes_updated_at
  before update on episodes
  for each row execute function set_updated_at();

drop trigger if exists trg_shots_updated_at on shots;
create trigger trg_shots_updated_at
  before update on shots
  for each row execute function set_updated_at();

-- ---------- 3. assets：资产（只存 R2 路径，不存二进制） ----------
create table if not exists assets (
  id            uuid primary key default gen_random_uuid(),
  episode_id    uuid not null references episodes(id) on delete cascade,
  -- shot_id 可空：成片 / 集级字幕挂在集上，不挂某一镜。
  shot_id       uuid references shots(id) on delete cascade,
  kind          asset_kind not null,
  -- R2 对象 key，如 xiaoxia/ep07/shot01/first-frame.png
  -- 不存完整 URL、不存签名 URL，URL 由 Workers 按需拼。
  r2_key        text not null,
  -- 桶名：建议一个桶 xiaoxia-assets（待 Chris 确认桶名）。
  r2_bucket     text not null default 'xiaoxia-assets',
  file_name     text,
  mime_type     text,
  size_bytes    bigint check (size_bytes is null or size_bytes >= 0),
  width         int,
  height        int,
  duration_sec  numeric(6,2),
  -- 校验：成片 SHA-256，锁定后填入，防以后误覆盖。
  sha256        text,
  -- 是否为当前锁定版本：同一镜可有多版首帧，只有一版 is_current=true。
  is_current    boolean not null default false,
  -- 生成来源：kaggle / colab / musebox / 其他，方便追溯。
  source        text,
  created_at    timestamptz not null default now()
);

comment on table assets is '资产：首帧/成片/字幕/参考图，只存 R2 key，不存二进制。';
comment on column assets.r2_key is 'R2 对象 key，如 xiaoxia/ep07/shot01/first-frame.png。';
comment on column assets.is_current is '同一镜多版本时，只有一版为 true，即当前采用版。';

create index if not exists idx_assets_episode on assets (episode_id);
create index if not exists idx_assets_shot on assets (shot_id);
create index if not exists idx_assets_kind on assets (kind);

-- ---------- 4. copy：文案（标题/正文/标签，OpenRouter 生成记录） ----------
create table if not exists copy (
  id              uuid primary key default gen_random_uuid(),
  episode_id      uuid not null references episodes(id) on delete cascade,
  -- 平台：xiaohongshu / douyin / shipinhao，同一集可多平台多版本。
  platform        text not null,
  -- 类型：title / body / tags / storyboard_text
  copy_type       text not null
                  check (copy_type in ('title','body','tags','storyboard_text')),
  content         text not null,
  -- OpenRouter 生成记录：模型、用量、是否人工改过。
  -- 只存模型名和 token 数，不存 key、不存完整原始返回以外的敏感信息。
  model           text,  -- 如 openrouter/free（实际模型见 model_used）
  model_used      text,  -- 实际返回的模型，如 nvidia/nemotron-3-...:free
  prompt_tokens   int,
  completion_tokens int,
  -- 人工改过：true 表示 Chris / Muse 改过原文案，不再是纯模型输出。
  edited_by_human boolean not null default false,
  -- 是否为当前采用版：同一集+平台+类型，只有一版 is_current=true。
  is_current      boolean not null default false,
  created_at      timestamptz not null default now()
);

comment on table copy is '文案：标题/正文/标签/分镜文案。记录 OpenRouter 模型与用量，方便按 50 次/天预算。';
comment on column copy.model is '请求时用的模型名，建议用 openrouter/free 路由，避免写死会轮换下架的模型。';
comment on column copy.model_used is 'OpenRouter 实际返回的模型名，以响应为准。';

create index if not exists idx_copy_episode_platform on copy (episode_id, platform, copy_type);

-- ---------- 视图：每集进度一览（给 Workers / 内部看板用） ----------
create or replace view v_episode_progress as
select
  e.id as episode_id,
  e.episode_no,
  e.title,
  e.status as episode_status,
  count(s.id) as shot_count,
  count(s.id) filter (where s.status = 'locked') as locked_count,
  count(s.id) filter (where s.status = 'published') as published_count,
  count(s.id) filter (where s.status = 'todo') as todo_count
from episodes e
left join shots s on s.episode_id = e.id
group by e.id, e.episode_no, e.title, e.status;

comment on view v_episode_progress is '每集进度：总镜数 / 已锁 / 已发 / 待做。';

-- ---------- 种子数据（示例，不含真实图片路径） ----------
-- 执行后请按真实集数修改。Ep7 为已做候选成片的例子。
-- 注意：种子数据里的 r2_key 只是占位格式，真实 key 以 R2 实际上传为准。
-- insert into episodes (episode_no, title, synopsis, duration_sec, status)
-- values ('Ep7', '奶奶的番茄汤', '小夏和奶奶做番茄汤，6 镜 x 5 秒', 30, 'candidate');
