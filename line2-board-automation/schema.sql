-- ============================================================
-- 2号线：看板自动化线 — Supabase 建表 SQL（模板，不部署）
-- 项目：line2-board-automation
-- 日期：2026-11-09
-- 用途：GitHub Actions 定时抓取 → Supabase 存 → Cloudflare Workers 供数
-- 原则：
--   1. 只在 Supabase SQL Editor 里由 Chris 本人执行，不要把 Service Key
--      写进这个文件、不要发到聊天里。
--   2. 公开读、写入只走 Service Role（GitHub Actions Secrets）。
--      Worker 只用 Anon Key + RLS 公开读，绝不放 Service Key。
--   3. 去重以链接为准；失败不删旧数据；过期先标 expired，再按保留期删。
-- ============================================================

-- 需要 pgcrypto 提供 gen_random_uuid()
create extension if not exists pgcrypto;

-- ------------------------------------------------------------
-- 1. sources：数据源登记表
--    一个来源一行。新增来源只加行，不改表结构。
--    board 取值：hot | deals | jettt | low_threshold
--    kind 取值：rss | public_json | public_html | curated
--      curated = 人工核查清单（低门槛薅羊毛），不由爬虫写，
--      只由 Chris / Muse 在 Supabase Dashboard 里维护。
-- ------------------------------------------------------------
create table if not exists public.sources (
  id text primary key,
  -- 例：baidu / toutiao / zhihu / bilibili / nodeseek / v2ex /
  --     linuxdo / xiaozhong / jettt_mint / unity_launches /
  --     unity_ranking / smart_wallets / low_threshold
  name text not null,
  board text not null check (board in ('hot', 'deals', 'jettt', 'low_threshold')),
  kind text not null check (kind in ('rss', 'public_json', 'public_html', 'curated')),
  url text,
  category text, -- 仅 deals / low_threshold 用，如 VPS / AI订阅 / Crypto
  fetch_interval_minutes int not null default 120,
  enabled boolean not null default true,
  -- 最近一次抓取状态：ok / degraded / failed / never
  last_status text not null default 'never'
    check (last_status in ('ok', 'degraded', 'failed', 'never')),
  last_fetched_at timestamptz,
  last_error text,
  note text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

-- 初始登记：只登记，不代表已验证可抓。
-- LINUX DO、微博见 DESIGN.md 第 7 节：LINUX DO 可能被 Cloudflare 拦，
-- 微博官方接口已确认拒绝（Forbidden），不强行抓。
insert into public.sources (id, name, board, kind, url, fetch_interval_minutes, note) values
  ('baidu',        '百度热搜',       'hot',   'public_html', 'https://top.baidu.com/board?tab=realtime', 120, '热点板：公开榜单'),
  ('toutiao',      '今日头条热榜',   'hot',   'public_json', null, 120, '热点板：公开榜单，URL 以抓取脚本实际使用的为准'),
  ('zhihu',        '知乎热榜',       'hot',   'public_json', null, 120, '热点板：公开榜单，URL 以抓取脚本实际使用的为准'),
  ('bilibili',     'B站热门',        'hot',   'public_json', null, 120, '热点板：公开榜单，URL 以抓取脚本实际使用的为准'),
  ('nodeseek',     'NodeSeek',       'deals', 'public_html', null, 120, '羊毛墙：需按现有分类器过滤，只收优惠帖'),
  ('v2ex',         'V2EX',           'deals', 'public_html', null, 120, '羊毛墙：需按现有分类器过滤，只收优惠帖'),
  ('linuxdo',      'LINUX DO',       'deals', 'public_html', null, 120, '羊毛墙：已知可能被 Cloudflare 拦，失败即标 degraded，不绕过验证'),
  ('xiaozhong',    '小众软件',       'deals', 'public_html', null, 120, '羊毛墙：金融帖极少，仍走同一分类器'),
  ('jettt_mint',   'Jettt mint',     'jettt', 'public_json', null, 1440, '打新板：公开 JSON，每日约 09:37 CST 刷新'),
  ('unity_launches','Unity 新盘',    'jettt', 'public_json', null, 1440, '打新板：公开 JSON'),
  ('unity_ranking','Unity 热度榜',   'jettt', 'public_json', null, 1440, '打新板：公开 JSON，含套利净利与铸造进度'),
  ('smart_wallets','24h 盈利聪明钱包','jettt','public_json', null, 1440, '打新板：公开 JSON，前 15 名'),
  ('low_threshold','低门槛清单',     'low_threshold', 'curated', null, 1440, '人工核查清单，不由爬虫写；每天约 10:37 CST 复核过期')
on conflict (id) do nothing;

-- ------------------------------------------------------------
-- 2. items：统一内容表（热点 / 羊毛 / 打新 / 低门槛共用）
-- ------------------------------------------------------------
create table if not exists public.items (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references public.sources(id) on delete restrict,
  board text not null check (board in ('hot', 'deals', 'jettt', 'low_threshold')),
  title text not null,
  url text not null,
  -- 去重键：链接全局唯一。同一链接在两个来源出现时，
  -- 后到的那次做 upsert，不产生第二条。
  -- 注意：这意味着跨源重复链接只保留第一次写入的 source_id。
  -- 如需按来源分别保留，改成 unique(source_id, url)，见 DESIGN.md。
  category text,      -- deals / low_threshold：VPS / AI订阅 / Crypto / U卡 / 海外银行 / 做任务赚钱 / 加密货币 / 美股券商 / 银行卡 / 购物 等
  rank int,           -- 热点榜排名 / 热度榜排名；无排名时为 null
  published_at timestamptz,
  fetched_at timestamptz not null default now(),
  -- 有效期：羊毛 / 低门槛用。hot / jettt 一般为 null。
  expires_at timestamptz,
  -- 原帖未标注有效期时，羊毛墙按现有规则：保留 7 天。
  -- 写入时由抓取脚本算好 expires_at（= fetched_at + 7 天），
  -- 不要只在展示层算，否则清理和查询会不一致。
  expires_note text,  -- 例：长期有效 / 原帖未标注有效期 · 看板保留至 2026-11-16
  -- 状态：active = 在架；expired = 已过期（先标，不立即删）；
  --       archived = 已下架 / 已清理出展示。
  status text not null default 'active'
    check (status in ('active', 'expired', 'archived')),
  -- 展示用扩展字段：热度、套利净利、铸造进度、点赞/收藏等。
  -- 不要把大图 / 文件正文存这里，文件走 R2（1 号线 / 另案）。
  meta jsonb not null default '{}'::jsonb,
  -- 原始抓取记录（调试用），不要把 Cookie / Token / 密钥放进来。
  raw jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (url)
);

-- 查询索引：Worker 的查询基本都是这几个组合。
create index if not exists idx_items_board_status_fetched
  on public.items (board, status, fetched_at desc);
create index if not exists idx_items_board_category
  on public.items (board, category, fetched_at desc)
  where status = 'active';
create index if not exists idx_items_source_fetched
  on public.items (source_id, fetched_at desc);
create index if not exists idx_items_expires
  on public.items (expires_at)
  where status = 'active' and expires_at is not null;
create index if not exists idx_items_published
  on public.items (published_at desc);

-- ------------------------------------------------------------
-- 3. fetch_logs：每次抓取一行，失败也要写
--    失败不删旧数据的依据就靠这张表 + items.fetched_at。
--    空间页脚“某路失败，保留旧数据”的标注，应读这张表，
--    不要只靠抓取脚本当时打印的一行日志。
-- ------------------------------------------------------------
create table if not exists public.fetch_logs (
  id uuid primary key default gen_random_uuid(),
  source_id text not null references public.sources(id) on delete restrict,
  board text not null check (board in ('hot', 'deals', 'jettt', 'low_threshold')),
  started_at timestamptz not null default now(),
  finished_at timestamptz,
  -- ok / partial / failed。partial = 抓到一部分，单条解析失败不致全败。
  status text not null check (status in ('ok', 'partial', 'failed')),
  items_fetched int not null default 0,
  items_upserted int not null default 0,
  items_skipped int not null default 0,
  error text,
  duration_ms int,
  runner text, -- 例：github-actions / manual
  created_at timestamptz not null default now()
);

create index if not exists idx_fetch_logs_source_started
  on public.fetch_logs (source_id, started_at desc);
create index if not exists idx_fetch_logs_board_started
  on public.fetch_logs (board, started_at desc);

-- ------------------------------------------------------------
-- 4. RLS：公开读、写入只走 Service Role
--    启用 RLS 后，Anon Key 只能读 active 的 items 和 sources，
--    不能写；Service Role 不受 RLS 限制（供 GitHub Actions 用）。
--    不要为 Worker 配 Service Key。
-- ------------------------------------------------------------
alter table public.sources enable row level security;
alter table public.items enable row level security;
alter table public.fetch_logs enable row level security;

-- 公开读：sources 只读已启用的；items 只读 active；logs 只读最近记录。
-- 注意：drop + create 保证重复执行不报错。
drop policy if exists "public read sources" on public.sources;
create policy "public read sources" on public.sources
  for select using (enabled = true);

drop policy if exists "public read active items" on public.items;
create policy "public read active items" on public.items
  for select using (status = 'active');

drop policy if exists "public read fetch logs" on public.fetch_logs;
create policy "public read fetch logs" on public.fetch_logs
  for select using (true);

-- 不建任何公开写策略。写操作一律用 Service Role Key，
-- 经 Supabase REST / PostgREST 的 service_role 身份完成。

-- ------------------------------------------------------------
-- 5. 过期与清理：不要用 DELETE 直接清，先标 expired
--    下面的语句由 GitHub Actions 的 cleanup job 每天跑一次，
--    不要在 Supabase 里配 pg_cron（免费档不保证可用，见 DESIGN.md）。
--
--    5a. 标过期：expires_at 已过、仍为 active 的，改为 expired。
--    5b. 清理：
--      - hot：只保留最近 24 小时快照，旧的删（热点本身就是快照）。
--      - deals：expired 超过 7 天的删；无 expires_at 的按
--        fetched_at + 7 天保留期算，超期删（与现有羊毛墙规则一致）。
--      - jettt：只保留最近 7 天快照，旧的删。
--      - low_threshold：expired 的不自动删，等人工复核；
--        每天复核 job 只标 expired，不删。
--      - fetch_logs：只保留最近 30 天。
-- ------------------------------------------------------------

-- 5a. 标过期（每天跑一次即可）
-- update public.items
--   set status = 'expired', updated_at = now()
--   where status = 'active'
--     and expires_at is not null
--     and expires_at < now();

-- 5b-1. 清理热点旧快照（保留最近 24 小时）
-- delete from public.items
--   where board = 'hot'
--     and fetched_at < now() - interval '24 hours';

-- 5b-2. 清理羊毛墙过期条目（expired 超过 7 天；无 expires_at 的按 fetched_at + 7 天）
-- delete from public.items
--   where board = 'deals'
--     and (
--       (status = 'expired' and updated_at < now() - interval '7 days')
--       or (expires_at is null and fetched_at < now() - interval '7 days')
--     );

-- 5b-3. 清理打新旧快照（保留最近 7 天）
-- delete from public.items
--   where board = 'jettt'
--     and fetched_at < now() - interval '7 days';

-- 5b-4. 清理旧抓取日志（保留最近 30 天）
-- delete from public.fetch_logs
--   where started_at < now() - interval '30 days';

-- ------------------------------------------------------------
-- 6. 自检查询（建表后在 SQL Editor 里手动跑，确认 RLS 生效）
-- ------------------------------------------------------------
-- select count(*) from public.sources;
-- select board, status, count(*) from public.items group by board, status;
-- select source_id, status, count(*) from public.fetch_logs
--   where started_at > now() - interval '24 hours' group by source_id, status;
-- -- 用 Anon Key 查应只能看到 active 的 items；用 SQL Editor（postgres 身份）
-- -- 查能看到全部。两者不一致是正常的，不要因此关掉 RLS。
