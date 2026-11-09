#!/usr/bin/env python3
"""2号线：低门槛清单复核 — 只标过期，不删除。

低门槛清单是人工核查的 36 条清单，不是爬虫产物（DESIGN.md §2）。
每天跑一次：把 board=low_threshold、status=active、expires_at 已过的标 expired，
等 Chris / Muse 在 Supabase Dashboard 里人工确认后再删。
新条目也只在 Dashboard 里加，不由脚本写。

用法：
  python scripts/review_low_threshold.py --mark-expired-only [--dry-run]
环境变量：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（--dry-run 时不需要）。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone

import requests

TIMEOUT = 25


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def count_expired(base: str, headers: dict) -> int:
    """数出将要标过期的条数（PostgREST count=exact）。"""
    resp = requests.get(
        base + "/rest/v1/items",
        headers={**headers, "Prefer": "count=exact"},
        params={
            "board": "eq.low_threshold",
            "status": "eq.active",
            "expires_at": f"lt.{now_iso()}",
            "select": "id",
            "limit": 1,
        },
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="低门槛清单：只标过期，不删除")
    parser.add_argument("--mark-expired-only", action="store_true", required=True,
                        help="只把已过期的标 expired，不删除")
    parser.add_argument("--dry-run", action="store_true",
                        help="只打印将要标过期的数量，不写库")
    args = parser.parse_args(argv)

    if args.dry_run:
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
        if not url or not key:
            print("[dry-run] low_threshold: needs SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY "
                  "to count; without them, nothing to do", file=sys.stderr)
            return 0
        headers = {"apikey": key, "Authorization": f"Bearer {key}"}
        try:
            n = count_expired(url.rstrip("/"), headers)
        except Exception as exc:  # noqa: BLE001
            print(f"[dry-run] count failed: {exc}", file=sys.stderr)
            return 1
        print(json.dumps({"dry_run": True, "board": "low_threshold",
                          "to_mark_expired": n}, ensure_ascii=False, indent=2))
        print(f"[dry-run] low_threshold: {n} item(s) would be marked expired",
              file=sys.stderr)
        return 0

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
        "Prefer": "return=representation",
    }
    try:
        before = count_expired(base, headers)
        resp = requests.patch(
            base + "/rest/v1/items",
            headers=headers,
            params={
                "board": "eq.low_threshold",
                "status": "eq.active",
                "expires_at": f"lt.{now_iso()}",
            },
            json={"status": "expired", "updated_at": now_iso()},
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        marked = len(resp.json())
    except Exception as exc:  # noqa: BLE001
        print(f"review failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"board": "low_threshold", "to_mark_expired": before,
                      "marked_expired": marked}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
