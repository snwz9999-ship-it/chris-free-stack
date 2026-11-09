/**
 * ============================================================
 * 2号线：看板自动化线 — Cloudflare Workers API 骨架（模板，不部署）
 * 文件：worker.ts（部署时配合 wrangler.toml / wrangler.jsonc 使用）
 * 日期：2026-11-09
 *
 * 原则：
 *  1. Worker 只读 Supabase，不写库。写库只走 GitHub Actions
 *     （Service Role Key）。Worker 只用 Anon Key + RLS 公开读。
 *  2. 绝不把 SUPABASE_SERVICE_ROLE_KEY 放进 Worker / wrangler 配置。
 *  3. 免费档限制（2026-11-09 核查，见 DESIGN.md）：
 *     100,000 请求/天（UTC 午夜重置，超限报 Error 1027）、
 *     每次调用 CPU 10 ms（只算 CPU，不算等网络的时间）、
 *     内存 128 MB、每次调用最多 50 个子请求。
 *     本 Worker 每次请求只做 1 次 Supabase 查询 + 少量 JSON 处理，
 *     远未触线；不要在 Worker 里做抓取、解析 HTML、跑分类器。
 *  4. wrangler 配置（部署时另建，不要写在这个文件里）：
 *     - 绑定环境变量：SUPABASE_URL、SUPABASE_ANON_KEY
 *       （用 `wrangler secret put` 放，不要写进代码和仓库）
 *     - 不要给这个 Worker 配 Cron Trigger 做抓取；
 *       免费档全账号只有 5 个 Cron Trigger，抓取已由 Actions 负责。
 *
 * 本地调试（部署前）：
 *   npx wrangler dev --env dev
 *   curl 'http://localhost:8787/health'
 *   curl 'http://localhost:8787/hot?limit=20'
 * ============================================================
 */

export interface Env {
  SUPABASE_URL: string;
  SUPABASE_ANON_KEY: string;
}

// Supabase PostgREST 返回的行（与 schema.sql 的 public.items 对应）
export interface BoardItem {
  id: string;
  source_id: string;
  board: string;
  title: string;
  url: string;
  category: string | null;
  rank: number | null;
  published_at: string | null;
  fetched_at: string;
  expires_at: string | null;
  expires_note: string | null;
  status: string;
  meta: Record<string, unknown>;
}

export interface FetchLogRow {
  source_id: string;
  board: string;
  started_at: string;
  status: string;
  items_fetched: number;
  items_upserted: number;
  error: string | null;
}

const CORS_HEADERS: Record<string, string> = {
  "Access-Control-Allow-Origin": "*",
  "Access-Control-Allow-Methods": "GET, OPTIONS",
  "Access-Control-Allow-Headers": "Content-Type",
  // 空间是公开展示页，允许跨域 GET；如需收紧，把 * 改成空间域名。
  // 不要在这里加写接口，写接口不在本设计范围内。
};

function json(data: unknown, status = 200, cacheSeconds = 60): Response {
  return new Response(JSON.stringify(data), {
    status,
    headers: {
      "Content-Type": "application/json; charset=utf-8",
      ...CORS_HEADERS,
      // 热点 / 羊毛 2 小时一更，缓存 60 秒足够；
      // 打新每天一更，调用方也可按 board 覆盖缓存时间。
      "Cache-Control": `public, max-age=${cacheSeconds}`,
    },
  });
}

function error(message: string, status = 500): Response {
  return json({ ok: false, error: message }, status, 0);
}

/**
 * 只读查 Supabase PostgREST。
 * - 只查 status=active，避免把已过期 / 已归档的旧条目供出去。
 * - expires_at 为空 或 未过期 的才算在架；已过期但还没被标 expired 的，
 *   在查询层再拦一道，不依赖清理 job 是否准时。
 * - limit 上限 200，防止一次拉全表。
 */
async function queryItems(
  env: Env,
  params: {
    board: string;
    category?: string | null;
    sourceId?: string | null;
    limit: number;
  },
): Promise<BoardItem[]> {
  const url = new URL(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/items`);
  url.searchParams.set("select", "id,source_id,board,title,url,category,rank,published_at,fetched_at,expires_at,expires_note,status,meta");
  url.searchParams.set("board", `eq.${params.board}`);
  url.searchParams.set("status", "eq.active");
  url.searchParams.set("order", "fetched_at.desc");
  url.searchParams.set("limit", String(Math.min(Math.max(params.limit, 1), 200)));
  if (params.category) url.searchParams.set("category", `eq.${params.category}`);
  if (params.sourceId) url.searchParams.set("source_id", `eq.${params.sourceId}`);

  const res = await fetch(url.toString(), {
    headers: {
      apikey: env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${env.SUPABASE_ANON_KEY}`,
      Accept: "application/json",
    },
  });
  if (!res.ok) throw new Error(`Supabase query failed: ${res.status}`);
  const rows = (await res.json()) as BoardItem[];
  const now = Date.now();
  return rows.filter((r) => !r.expires_at || new Date(r.expires_at).getTime() > now);
}

/** 最近一次抓取状态：供空间页脚标注“某路失败，保留旧数据”。 */
async function querySourceHealth(env: Env, board: string): Promise<FetchLogRow[]> {
  const url = new URL(`${env.SUPABASE_URL.replace(/\/$/, "")}/rest/v1/fetch_logs`);
  url.searchParams.set("select", "source_id,board,started_at,status,items_fetched,items_upserted,error");
  url.searchParams.set("board", `eq.${board}`);
  url.searchParams.set("order", "started_at.desc");
  url.searchParams.set("limit", "50");
  const res = await fetch(url.toString(), {
    headers: {
      apikey: env.SUPABASE_ANON_KEY,
      Authorization: `Bearer ${env.SUPABASE_ANON_KEY}`,
      Accept: "application/json",
    },
  });
  if (!res.ok) throw new Error(`Supabase logs query failed: ${res.status}`);
  return (await res.json()) as FetchLogRow[];
}

function parseLimit(searchParams: URLSearchParams, fallback: number): number {
  const n = Number(searchParams.get("limit") ?? fallback);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(Math.max(Math.floor(n), 1), 200);
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);

    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: CORS_HEADERS });
    }
    if (request.method !== "GET") {
      return error("Only GET is supported", 405);
    }

    try {
      // 健康检查：不查库，只确认 Worker 活着、环境变量已配。
      if (url.pathname === "/health") {
        return json({
          ok: true,
          has_supabase_url: Boolean(env.SUPABASE_URL),
          has_anon_key: Boolean(env.SUPABASE_ANON_KEY),
          time: new Date().toISOString(),
        });
      }

      // GET /hot?limit=50 — 全网实时热点：百度 / 头条 / 知乎 / B站
      // 现有空间每期约 119 条；limit 默认 120，调用方可传 limit。
      if (url.pathname === "/hot") {
        const items = await queryItems(env, {
          board: "hot",
          limit: parseLimit(url.searchParams, 120),
        });
        const health = await querySourceHealth(env, "hot");
        return json({ ok: true, board: "hot", count: items.length, items, sources: health }, 200, 60);
      }

      // GET /deals?category=VPS&limit=50 — 全网薅羊毛
      // category 可选：VPS / AI订阅 / Crypto / U卡 / 海外银行 / 免费白嫖 / 会员优惠
      // 不传 category 时返回全部 7 个分类；空间端再按分类切 Tab。
      if (url.pathname === "/deals") {
        const items = await queryItems(env, {
          board: "deals",
          category: url.searchParams.get("category"),
          limit: parseLimit(url.searchParams, 100),
        });
        const health = await querySourceHealth(env, "deals");
        return json({ ok: true, board: "deals", count: items.length, items, sources: health }, 200, 60);
      }

      // GET /jettt — Jettt Unity 打新看板：四路公开 JSON 的快照
      // 返回时按 source_id 分组，空间端按现有四块展示，不混成一列。
      if (url.pathname === "/jettt") {
        const items = await queryItems(env, { board: "jettt", limit: 200 });
        const health = await querySourceHealth(env, "jettt");
        const bySource: Record<string, BoardItem[]> = {};
        for (const item of items) {
          (bySource[item.source_id] ??= []).push(item);
        }
        return json({ ok: true, board: "jettt", count: items.length, bySource, sources: health }, 200, 300);
      }

      // GET /low-threshold?category=做任务赚钱 — 低门槛薅羊毛（人工清单）
      // 这块不是爬虫产物，Worker 只读 Supabase 里的清单，不触发抓取。
      if (url.pathname === "/low-threshold") {
        const items = await queryItems(env, {
          board: "low_threshold",
          category: url.searchParams.get("category"),
          limit: parseLimit(url.searchParams, 100),
        });
        return json({ ok: true, board: "low_threshold", count: items.length, items }, 200, 300);
      }

      // GET /sources?board=hot — 某个板最近一次各路抓取状态
      // 空间页脚用它标注“某路失败，保留旧数据”，不要自己猜。
      if (url.pathname === "/sources") {
        const board = url.searchParams.get("board") ?? "hot";
        const health = await querySourceHealth(env, board);
        return json({ ok: true, board, sources: health }, 200, 60);
      }

      return error(`Unknown route: ${url.pathname}`, 404);
    } catch (err) {
      // Supabase 挂了 / 暂停了 / 密钥错了：返回 502 + 一句话，
      // 空间端看到 502 就继续展示上一次的内置快照，不要白屏。
      // 具体错误不要原样抛给前端（可能含内部地址）。
      console.error(err);
      return error("Upstream unavailable", 502);
    }
  },
};
