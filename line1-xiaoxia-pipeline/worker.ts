// ============================================================
// 小夏视频流水线 1号线 — Cloudflare Workers API 骨架
// 文件：worker.ts
// 日期：2026-11-09（设计版，未部署）
// 用途：最小可读接口，给 Supabase 做一层薄 API，
//   供内部看板（个人模型实验室 / 故事集空间）读取进度，
//   以及更新分镜状态。不是对外公开 API。
//
// 部署前必须确认（见 DESIGN.md §0 / §9）：
//   - Cloudflare 账号是否已有；R2 桶名是否为 xiaoxia-assets
//   - Supabase URL + anon/service key 是否已有
//   - 不要把 service_role key 写进代码、不要提交到 Git。
//     用 `wrangler secret put SUPABASE_SERVICE_KEY` 单独放。
//
// 免费档约束（2026-11-09 核查，见 DESIGN.md §1）：
//   - Workers Free：100,000 请求/天（UTC 午夜重置），
//     每次调用 CPU 10 ms 上限，内存 128 MB，
//     每个请求最多 50 个子请求（fetch 计数）。
//   - 因此：Worker 里只做薄转发 + 简单校验，
//     不做重计算、不做图片处理、不循环调多次 Supabase。
//     复杂查询交给 Supabase 视图 v_episode_progress。
// ============================================================

export interface Env {
  // Supabase 项目 URL，如 https://xxxx.supabase.co
  SUPABASE_URL: string;
  // 读用 anon key 即可；写状态建议用 service_role key，
  // 但必须走 Secret，不能写死。
  SUPABASE_ANON_KEY: string;
  SUPABASE_SERVICE_KEY: string;
  // 写接口的简单口令，部署时用 wrangler secret put 设置。
  // 不是正式鉴权，只是防止陌生人随手改状态。
  // 对外开放前必须换成 Supabase Auth / RLS。
  WRITE_TOKEN: string;
  // R2 绑定（可选，本骨架不直接读 R2，只返回 r2_key；
  // 需要签名 URL 时再加 R2 绑定）。
  // ASSETS: R2Bucket;
}

const JSON_HEADERS = { 'content-type': 'application/json; charset=utf-8' };

function json(data: unknown, status = 200): Response {
  return new Response(JSON.stringify(data), { status, headers: JSON_HEADERS });
}

function badRequest(message: string): Response {
  return json({ ok: false, error: message }, 400);
}

function unauthorized(): Response {
  return json({ ok: false, error: 'unauthorized' }, 401);
}

// Supabase REST 调用：只用最简单的 select / update，不拼复杂 SQL。
async function sb(
  env: Env,
  path: string,
  init: RequestInit = {},
  useServiceKey = false
): Promise<Response> {
  const key = useServiceKey ? env.SUPABASE_SERVICE_KEY : env.SUPABASE_ANON_KEY;
  return fetch(`${env.SUPABASE_URL}/rest/v1/${path}`, {
    ...init,
    headers: {
      apikey: key,
      Authorization: `Bearer ${key}`,
      'Content-Type': 'application/json',
      Prefer: 'return=representation',
      ...(init.headers || {}),
    },
  });
}

// 分镜状态白名单，与 schema.sql 的 shot_status 保持一致。
const SHOT_STATUSES = new Set([
  'todo',
  'first_frame',
  'video',
  'candidate',
  'locked',
  'published',
]);

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;
    const method = request.method;

    // 健康检查：不碰数据库。
    if (pathname === '/health' && method === 'GET') {
      return json({ ok: true, service: 'xiaoxia-pipeline', version: '0.1-design' });
    }

    // GET /episodes — 集列表 + 每集进度（用视图 v_episode_progress）
    // 例：GET /episodes?status=candidate
    if (pathname === '/episodes' && method === 'GET') {
      const status = url.searchParams.get('status');
      const query = status
        ? `v_episode_progress?episode_status=eq.${encodeURIComponent(status)}&order=episode_no.asc`
        : `v_episode_progress?order=episode_no.asc`;
      const res = await sb(env, query);
      if (!res.ok) return json({ ok: false, error: 'supabase_error', status: res.status }, 502);
      const rows = await res.json();
      return json({ ok: true, episodes: rows });
    }

    // GET /episodes/:id — 单集详情（含分镜、资产、文案的 id 列表，不展开大文本）
    // :id 接受 uuid 或 episode_no（如 Ep7）。先按 episode_no 查，查不到再按 id 查。
    const episodeMatch = pathname.match(/^\/episodes\/([^/]+)$/);
    if (episodeMatch && method === 'GET') {
      const idOrNo = decodeURIComponent(episodeMatch[1]);
      const isUuid = /^[0-9a-f-]{36}$/i.test(idOrNo);
      const filter = isUuid ? `id=eq.${idOrNo}` : `episode_no=eq.${encodeURIComponent(idOrNo)}`;
      const res = await sb(env, `episodes?${filter}&select=*`);
      if (!res.ok) return json({ ok: false, error: 'supabase_error', status: res.status }, 502);
      const rows = (await res.json()) as Array<Record<string, unknown>>;
      if (rows.length === 0) return json({ ok: false, error: 'not_found' }, 404);
      return json({ ok: true, episode: rows[0] });
    }

    // GET /episodes/:id/shots — 某集全部分镜（按 shot_no 升序）
    const shotsMatch = pathname.match(/^\/episodes\/([^/]+)\/shots$/);
    if (shotsMatch && method === 'GET') {
      const idOrNo = decodeURIComponent(shotsMatch[1]);
      // 先把 episode_no / id 换成 episodes.id
      const isUuid = /^[0-9a-f-]{36}$/i.test(idOrNo);
      const epFilter = isUuid ? `id=eq.${idOrNo}` : `episode_no=eq.${encodeURIComponent(idOrNo)}`;
      const epRes = await sb(env, `episodes?${epFilter}&select=id`);
      if (!epRes.ok) return json({ ok: false, error: 'supabase_error', status: epRes.status }, 502);
      const eps = (await epRes.json()) as Array<{ id: string }>;
      if (eps.length === 0) return json({ ok: false, error: 'episode_not_found' }, 404);
      const episodeId = eps[0].id;
      const res = await sb(
        env,
        `shots?episode_id=eq.${episodeId}&order=shot_no.asc&select=*`
      );
      if (!res.ok) return json({ ok: false, error: 'supabase_error', status: res.status }, 502);
      const rows = await res.json();
      return json({ ok: true, episode_id: episodeId, shots: rows });
    }

    // POST /shots/:id/status — 更新单个分镜状态（写接口，需 WRITE_TOKEN）
    // 请求头：Authorization: Bearer <WRITE_TOKEN>
    // 请求体：{ "status": "locked", "status_note": "Chris 11-09 已锁" }
    const statusMatch = pathname.match(/^\/shots\/([^/]+)\/status$/);
    if (statusMatch && method === 'POST') {
      const token = request.headers.get('Authorization')?.replace(/^Bearer\s+/i, '');
      if (!token || token !== env.WRITE_TOKEN) return unauthorized();

      const shotId = decodeURIComponent(statusMatch[1]);
      let body: { status?: string; status_note?: string };
      try {
        body = (await request.json()) as typeof body;
      } catch {
        return badRequest('invalid_json');
      }
      if (!body.status || !SHOT_STATUSES.has(body.status)) {
        return badRequest('invalid_status: must be todo/first_frame/video/candidate/locked/published');
      }
      const res = await sb(
        env,
        `shots?id=eq.${encodeURIComponent(shotId)}`,
        {
          method: 'PATCH',
          body: JSON.stringify({
            status: body.status,
            status_note: body.status_note ?? null,
          }),
        },
        true // 写操作走 service key
      );
      if (!res.ok) return json({ ok: false, error: 'supabase_error', status: res.status }, 502);
      const rows = await res.json();
      return json({ ok: true, shot: rows[0] ?? null });
    }

    // GET /episodes/:id/assets — 某集资产（只返回 r2_key 等元数据，不返回文件本身）
    const assetsMatch = pathname.match(/^\/episodes\/([^/]+)\/assets$/);
    if (assetsMatch && method === 'GET') {
      const idOrNo = decodeURIComponent(assetsMatch[1]);
      const isUuid = /^[0-9a-f-]{36}$/i.test(idOrNo);
      const epFilter = isUuid ? `id=eq.${idOrNo}` : `episode_no=eq.${encodeURIComponent(idOrNo)}`;
      const epRes = await sb(env, `episodes?${epFilter}&select=id`);
      if (!epRes.ok) return json({ ok: false, error: 'supabase_error', status: epRes.status }, 502);
      const eps = (await epRes.json()) as Array<{ id: string }>;
      if (eps.length === 0) return json({ ok: false, error: 'episode_not_found' }, 404);
      const res = await sb(
        env,
        `assets?episode_id=eq.${eps[0].id}&order=created_at.asc&select=id,shot_id,kind,r2_key,r2_bucket,file_name,mime_type,size_bytes,width,height,sha256,is_current,source`
      );
      if (!res.ok) return json({ ok: false, error: 'supabase_error', status: res.status }, 502);
      return json({ ok: true, assets: await res.json() });
    }

    return json({ ok: false, error: 'not_found' }, 404);
  },
};
