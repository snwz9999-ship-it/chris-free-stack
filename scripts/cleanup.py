#!/usr/bin/env python3
"""2号线：清理 job — 按 schema.sql §5 规则删旧快照。

顺序（不要一步 DELETE）：先把 expires_at 已过、仍为 active 的标 expired，
再按保留期删：
  - hot：只保留最近 24 小时快照；
  - deals：expired 超过 7 天的删；无 expires_at 的按 fetched_at + 7 天算，超期删；
  - jettt：只保留最近 7 天快照；
  - fetch_logs：只保留最近 30 天；
  - low_threshold：expired 的不自动删，等人工复核（只标，不删）。
失败不删旧数据：抓取 job 失败时不跑清理（靠分开的 cron）；清理本身失败只报错。

用法：
  python scripts/cleanup.py --dry-run-check   # 只打印将删数量（fetch.yml 现在就是这个）
  python scripts/cleanup.py                   # 真正执行
环境变量：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

import requests

TIMEOUT = 25


def now() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.isoformat()


def count_rows(base: str, headers: dict, table: str, params: dict) -> int:
    """PostgREST count=exact 计数。"""
    resp = requests.get(
        base + f"/rest/v1/{table}",
        headers={**headers, "Prefer": "count=exact"},
        params={**params, "select": "id", "limit": 1},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    cr = resp.headers.get("Content-Range", "")
    if "/" in cr:
        try:
            return int(cr.rsplit("/", 1)[1])
        except ValueError:
            pass
    return len(resp.json())


def delete_rows(base: str, headers: dict, table: str, params: dict) -> int:
    """DELETE 一组行，返回删掉的数量（return=representation）。"""
    resp = requests.delete(
        base + f"/rest/v1/{table}",
        headers={**headers, "Prefer": "return=representation"},
        params=params,
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return len(body) if isinstance(body, list) else 0


def mark_expired(base: str, headers: dict) -> int:
    """先把 expires_at 已过、仍为 active 的标 expired（全板，幂等）。"""
    resp = requests.patch(
        base + "/rest/v1/items",
        headers={**headers, "Prefer": "return=representation"},
        params={"status": "eq.active", "expires_at": f"lt.{iso(now())}"},
        json={"status": "expired", "updated_at": iso(now())},
        timeout=TIMEOUT,
    )
    resp.raise_for_status()
    body = resp.json()
    return len(body) if isinstance(body, list) else 0


# 删除规则：(表, 描述, 过滤参数) — 过滤参数在运行时算好。
def delete_rules(now_dt: datetime) -> list[tuple[str, str, dict]]:
    day_ago = now_dt - timedelta(hours=24)
    week_ago = now_dt - timedelta(days=7)
    month_ago = now_dt - timedelta(days=30)
    return [
        ("items", "hot older than 24h",
         {"board": "eq.hot", "fetched_at": f"lt.{iso(day_ago)}"}),
        ("items", "deals expired over 7d",
         {"board": "eq.deals", "status": "eq.expired",
          "updated_at": f"lt.{iso(week_ago)}"}),
        ("items", "deals no expiry, fetched over 7d ago",
         {"board": "eq.deals", "expires_at": "is.null",
          "fetched_at": f"lt.{iso(week_ago)}"}),
        ("items", "jettt older than 7d",
         {"board": "eq.jettt", "fetched_at": f"lt.{iso(week_ago)}"}),
        ("fetch_logs", "fetch_logs older than 30d",
         {"started_at": f"lt.{iso(month_ago)}"}),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="2号线：按保留期清理旧快照")
    parser.add_argument("--dry-run-check", action="store_true",
                        help="只打印将要删的数量，不真删（上线第一周用这个）")
    parser.add_argument("--dry-run", action="store_true",
                        help="--dry-run-check 的别名")
    args = parser.parse_args(argv)
    dry = args.dry_run_check or args.dry_run

    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set", file=sys.stderr)
        return 1
    base = url.rstrip("/")
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    now_dt = now()
    try:
        # 第一步永远是“只数数”：dry-run 直接报数；真删先数、后标过期、再删。
        counts = {}
        for table, desc, params in delete_rules(now_dt):
            counts[desc] = count_rows(base, headers, table, params)
        if dry:
            print(json.dumps({"dry_run": True, "to_delete": counts},
                             ensure_ascii=False, indent=2))
            print("[dry-run] cleanup would delete: "
                  + ", ".join(f"{k}={v}" for k, v in counts.items()), file=sys.stderr)
            return 0
        marked = mark_expired(base, headers)
        deleted = {}
        for table, desc, params in delete_rules(now_dt):
            deleted[desc] = delete_rows(base, headers, table, params)
    except Exception as exc:  # noqa: BLE001
        print(f"cleanup failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"marked_expired": marked, "deleted": deleted,
                      "counted_before": counts}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
