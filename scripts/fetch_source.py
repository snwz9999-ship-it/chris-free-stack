#!/usr/bin/env python3
"""2号线看板自动化：单路抓取 -> 写 Supabase（或 --dry-run 只打印）。

契约：~/workspace/plans/line2-board-automation/DESIGN.md §3.4 / §3.3 / §4.3
用法：
  python scripts/fetch_source.py --source <source_id> --board <board> [--dry-run]
环境变量：SUPABASE_URL、SUPABASE_SERVICE_ROLE_KEY（--dry-run 时不需要）。
退出码：全成功或部分成功返回 0；只有“完全没抓到且没写日志”才非零
（即抓取失败且 fetch_logs 也写不进去时；--dry-run 下抓取失败即非零）。

写库方式：Supabase REST，不拼 SQL。
  - items：POST /rest/v1/items?on_conflict=url + Prefer: resolution=merge-duplicates
    （upsert；已存在且人工改过 category/expires_at 的行，写库时不覆盖这两个字段）
  - fetch_logs：每次运行必写一行（ok / partial / failed）。
  - sources：更新 last_status / last_fetched_at / last_error。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import requests

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)
TIMEOUT = 25            # 与空间服务端一致：25 秒超时
RETRY_BACKOFF_S = 5     # 单次请求失败重试 1 次，退避 5 秒
REQUEST_GAP_S = 2       # 请求间隔 2 秒以上（本脚本每路只发 1 个列表请求）

BOARDS = ("hot", "deals", "jettt", "low_threshold")


class FetchError(Exception):
    """抓取失败（HTTP 错误、解析失败、上游返回 0 条等）。"""


class BlockedError(FetchError):
    """被对方拦截（403 / Challenge 页）：记 failed，不绕过。"""


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def parse_ts(value):
    """容错时间解析：epoch 秒/毫秒、ISO 字符串 -> ISO 字符串；解析不出返回 None。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            ts = float(value)
            if ts > 1e12:  # 毫秒
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        except (ValueError, OverflowError, OSError):
            return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).isoformat()
        except ValueError:
            pass
        try:
            return parse_ts(float(text))
        except ValueError:
            return None
    return None


def http_get(url: str, accept: str = "application/json") -> requests.Response:
    """GET 一次，失败重试 1 次（退避 5 秒）。403 直接判 Blocked，不重试绕过。"""
    headers = {"User-Agent": UA, "Accept": accept}
    last_exc: Exception | None = None
    for attempt in (0, 1):
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
            if resp.status_code == 403:
                raise BlockedError(f"HTTP 403 (blocked) for {url}")
            resp.raise_for_status()
            return resp
        except BlockedError:
            raise
        except Exception as exc:  # noqa: BLE001 - 统一记失败，不中断整轮
            last_exc = exc
            if attempt == 0:
                time.sleep(RETRY_BACKOFF_S)
    raise FetchError(f"GET {url} failed: {str(last_exc)[:500]}")


def get_json(url: str) -> dict | list:
    resp = http_get(url, accept="application/json")
    try:
        return resp.json()
    except ValueError as exc:
        raise FetchError(f"invalid JSON from {url}: {exc}") from exc


def bscscan_link(addr: str | None) -> str | None:
    if addr and isinstance(addr, str) and addr.strip():
        return f"https://bscscan.com/address/{addr.strip()}"
    return None


def first_url(*candidates) -> str | None:
    """取第一个以 http 开头的候选 URL；都没有返回 None。"""
    for cand in candidates:
        if isinstance(cand, str) and cand.strip().startswith(("http://", "https://")):
            return cand.strip()
    return None


def clean_item(item: dict) -> dict | None:
    """规范化一条 item；无 url 的返回 None（调用方计 items_skipped）。"""
    url = (item.get("url") or "").strip() if isinstance(item.get("url"), str) else None
    if not url:
        return None
    title = item.get("title")
    title = str(title).strip() if title is not None else ""
    if not title:
        title = url
    out = {
        "title": title,
        "url": url,
        "category": item.get("category"),
        "rank": item.get("rank"),
        "published_at": item.get("published_at"),
        "expires_at": item.get("expires_at"),
        "expires_note": item.get("expires_note"),
        "meta": item.get("meta") or {},
        "raw": item.get("raw") or {},
    }
    return out


# ---------------------------------------------------------------------------
# 打新板（jettt）：四路公开 JSON，全部服务端 GET，无登录
# 来源：jettt-unity-da-xin-kan-ban 空间 server/src/actions.ts SOURCE_URLS
# ---------------------------------------------------------------------------

JETTT_URLS = {
    "jettt_mint": "https://mint.jettt.us.ci/mint/data.json",
    "unity_launches": "https://unitytool.app/api/launches",
    "unity_ranking": "https://unitytool.app/api/ranking",
    "smart_wallets": "https://unitytool.app/api/wallets/top",
}

# 空间按 health.verdict 排序展示（ok/opened/suspect/full/ended），同 verdict 内按 pct 降序。
_VERDICT_ORDER = {"ok": 0, "opened": 1, "suspect": 2, "full": 3, "ended": 4}


def fetch_jettt_mint() -> list[dict]:
    """Jettt mint：{updated_at, projects[]}，projects 原样取用。"""
    data = get_json(JETTT_URLS["jettt_mint"])
    projects = data.get("projects") if isinstance(data, dict) else None
    if not isinstance(projects, list):
        raise FetchError("jettt_mint: unexpected payload, no projects[]")
    if not projects:
        raise FetchError("jettt_mint: upstream returned 0 projects")

    def sort_key(p: dict):
        health = p.get("health") if isinstance(p.get("health"), dict) else {}
        verdict = str(health.get("verdict") or "").lower()
        pct = p.get("pct")
        try:
            pct = float(pct)
        except (TypeError, ValueError):
            pct = 0.0
        return (_VERDICT_ORDER.get(verdict, 9), -pct)

    items: list[dict] = []
    skipped = 0
    for rank, p in enumerate(sorted(projects, key=sort_key), start=1):
        if not isinstance(p, dict):
            skipped += 1
            continue
        safety = p.get("safety") if isinstance(p.get("safety"), dict) else {}
        health = p.get("health") if isinstance(p.get("health"), dict) else {}
        url = first_url(
            p.get("url"),
            p.get("explorer"),
            bscscan_link(p.get("contract")),
        )
        item = clean_item({
            "title": p.get("name") or p.get("key") or "Jettt mint",
            "url": url,
            "rank": rank,
            "meta": {
                "key": p.get("key"),
                "sold": p.get("sold"),
                "target": p.get("target"),
                "remaining": p.get("remaining"),
                "pct": p.get("pct"),
                "price": p.get("price"),
                "currency": p.get("currency"),
                "stale": p.get("stale"),
                "phase": p.get("phase"),
                "free_text": p.get("free_text"),
                "free_remaining": p.get("free_remaining"),
                "free_claimed": p.get("free_claimed"),
                "paid_text": p.get("paid_text"),
                "safety": safety,
                "health": health,
                "note": p.get("note"),
            },
            "raw": {"key": p.get("key")},
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_jettt_mint.skipped = skipped
    return items


def fetch_unity_launches() -> list[dict]:
    """Unity 新盘：{items[]}，按上游顺序取前 60 条（空间实际行为，未按 first_seen 排序；
    DATA-PLAN.md 称按 first_seen 倒序，与代码不一致，见空间诊断；此处与空间实际行为一致）。"""
    data = get_json(JETTT_URLS["unity_launches"])
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise FetchError("unity_launches: unexpected payload, no items[]")
    if not raw_items:
        raise FetchError("unity_launches: upstream returned 0 items")
    items: list[dict] = []
    skipped = 0
    for rank, it in enumerate(raw_items[:60], start=1):
        if not isinstance(it, dict):
            skipped += 1
            continue
        addr = it.get("addr")
        url = first_url(
            it.get("website"),
            it.get("x"),
            it.get("tg"),
            bscscan_link(addr),
        )
        item = clean_item({
            "title": it.get("name") or it.get("symbol") or "Unity launch",
            "url": url,
            "category": it.get("kind"),
            "rank": rank,
            "published_at": parse_ts(it.get("first_seen") or it.get("created_ts")),
            "meta": {
                "symbol": it.get("symbol"),
                "first_seen": it.get("first_seen"),
                "created_ts": it.get("created_ts"),
                "agents": it.get("agents"),
                "agents1h": it.get("agents1h"),
                "callers_total": it.get("callers_total"),
                "price": it.get("price"),
                "mcap": it.get("mcap"),
                "liq": it.get("liq"),
                "vol24": it.get("vol24"),
                "chg24": it.get("chg24"),
                "holders": it.get("holders"),
                "risk_level": it.get("risk_level"),
                "risks": it.get("risks"),
                "trading": it.get("trading"),
                "gate_ref": it.get("gate_ref"),
                "platform": it.get("platform"),
                "description": it.get("description"),
                "total_supply": it.get("total_supply"),
            },
            "raw": {"addr": addr},
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_unity_launches.skipped = skipped
    return items


def fetch_unity_ranking() -> list[dict]:
    """Unity 热度榜：{items[]}，按上游热度顺序取前 40 条。"""
    data = get_json(JETTT_URLS["unity_ranking"])
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise FetchError("unity_ranking: unexpected payload, no items[]")
    if not raw_items:
        raise FetchError("unity_ranking: upstream returned 0 items")
    items: list[dict] = []
    skipped = 0
    for rank, it in enumerate(raw_items[:40], start=1):
        if not isinstance(it, dict):
            skipped += 1
            continue
        addr = it.get("addr")
        url = first_url(
            it.get("website"),
            it.get("x"),
            it.get("tg"),
            bscscan_link(addr),
        )
        item = clean_item({
            "title": it.get("name") or it.get("symbol") or "Unity ranking",
            "url": url,
            "rank": rank,
            "published_at": parse_ts(it.get("last")),
            "meta": {
                "symbol": it.get("symbol"),
                "agents": it.get("agents"),
                "txs": it.get("txs"),
                "buys": it.get("buys"),
                "sells": it.get("sells"),
                "price": it.get("price"),
                "mcap": it.get("mcap"),
                "liq": it.get("liq"),
                "vol24": it.get("vol24"),
                "chg5m": it.get("chg5m"),
                "chg1h": it.get("chg1h"),
                "chg24": it.get("chg24"),
                "holders": it.get("holders"),
                "top10": it.get("top10"),
                "trading": it.get("trading"),
                "platform": it.get("platform"),
                "risk_level": it.get("risk_level"),
                "alert_tier": it.get("alert_tier"),
                "callers_total": it.get("callers_total"),
                "arb": it.get("arb"),
                "launch": it.get("launch"),
                "rk": it.get("rk"),
            },
            "raw": {"addr": addr},
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_unity_ranking.skipped = skipped
    return items


def fetch_smart_wallets() -> list[dict]:
    """24h 盈利聪明钱包：{at, wallets, items[]}，按 pnlUsd 倒序取前 15 条。"""
    data = get_json(JETTT_URLS["smart_wallets"])
    raw_items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(raw_items, list):
        raise FetchError("smart_wallets: unexpected payload, no items[]")
    if not raw_items:
        raise FetchError("smart_wallets: upstream returned 0 items")

    def pnl(it: dict) -> float:
        try:
            return float(it.get("pnlUsd") or 0)
        except (TypeError, ValueError):
            return 0.0

    at = data.get("at") if isinstance(data, dict) else None
    items: list[dict] = []
    skipped = 0
    for rank, it in enumerate(sorted(raw_items, key=pnl, reverse=True)[:15], start=1):
        if not isinstance(it, dict):
            skipped += 1
            continue
        agent = it.get("agent")
        url = bscscan_link(agent)
        name = it.get("aname") or (agent[:10] + "…" if isinstance(agent, str) and agent else "smart wallet")
        item = clean_item({
            "title": name,
            "url": url,
            "rank": rank,
            "published_at": parse_ts(it.get("last")),
            "meta": {
                "agent": agent,
                "n": it.get("n"),
                "txs": it.get("txs"),
                "buys": it.get("buys"),
                "sells": it.get("sells"),
                "claims": it.get("claims"),
                "trades": it.get("trades"),
                "tmints": it.get("tmints"),
                "nmints": it.get("nmints"),
                "spentUsd": it.get("spentUsd"),
                "recvUsd": it.get("recvUsd"),
                "holdUsd": it.get("holdUsd"),
                "pnlUsd": it.get("pnlUsd"),
                "at": at,
                "wallets_total": data.get("wallets") if isinstance(data, dict) else None,
            },
            "raw": {"agent": agent},
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_smart_wallets.skipped = skipped
    return items


# ---------------------------------------------------------------------------
# 热点板（hot）：四路公开 JSON，服务端 GET，无鉴权
# 来源：quan-wang-shi-shi-re-dian 空间 server/src/actions.ts
# 每路取前 30 条，rank = 上游返回顺序（不按热度重排）。
# ---------------------------------------------------------------------------

import math
import re
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
from urllib.parse import urlencode

HOT_URLS = {
    "baidu": "https://top.baidu.com/api/board?platform=pc&tab=realtime",
    "zhihu": "https://api.zhihu.com/topstory/hot-list?limit=50",
    "toutiao": "https://www.toutiao.com/hot-event/hot-board/?origin=toutiao_pc",
    "bilibili": "https://api.bilibili.com/x/web-interface/popular?ps=30&pn=1",
}

# 头条 InterestCategory 英文码 -> 中文。检查报告只给出两个例子
# （military→军事、finance→财经），其余未知码按空间规则置 null，不猜。
TOUTIAO_CATEGORY_ZH = {"military": "军事", "finance": "财经"}
TOUTIAO_LABEL_ZH = {"hot": "热", "boil": "沸", "new": "新"}


def _heat_number(value) -> int | None:
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def _parse_zhihu_heat(text: str | None) -> int | None:
    """知乎 detail_text 如 "1057 万热度" -> 10570000；"1.2 亿热度" -> 120000000。"""
    if not text:
        return None
    m = re.search(r"([\d.]+)\s*(亿|万)?", text.replace("热度", ""))
    if not m:
        return None
    num = float(m.group(1))
    if m.group(2) == "亿":
        num *= 100_000_000
    elif m.group(2) == "万":
        num *= 10_000
    return int(num)


def fetch_baidu() -> list[dict]:
    data = get_json(HOT_URLS["baidu"])
    cards = data.get("data", {}).get("cards") if isinstance(data, dict) else None
    content = None
    if isinstance(cards, list) and cards:
        content = cards[0].get("content")
    # 兼容 wise 嵌套（content[0]）与 pc 扁平两种布局
    if isinstance(content, list) and content and isinstance(content[0], list):
        content = content[0]
    if not isinstance(content, list) or not content:
        raise FetchError("baidu: upstream returned empty content")
    items, skipped = [], 0
    for rank, c in enumerate(content[:30], start=1):
        if not isinstance(c, dict):
            skipped += 1
            continue
        title = c.get("word") or c.get("query")
        if not title:
            skipped += 1
            continue
        item = clean_item({
            "title": title,
            "url": c.get("url") or c.get("rawUrl"),
            "rank": rank,
            "meta": {
                "heat": _heat_number(c.get("hotScore")),
                "summary": c.get("desc"),
                "tag": "上升" if c.get("hotChange") == "rise" else None,
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_baidu.skipped = skipped
    if not items:
        raise FetchError("baidu: 0 usable items")
    return items


def fetch_zhihu() -> list[dict]:
    data = get_json(HOT_URLS["zhihu"])
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise FetchError("zhihu: upstream returned empty data")
    items, skipped = [], 0
    for entry in entries[:50]:
        if not isinstance(entry, dict):
            skipped += 1
            continue
        target = entry.get("target")
        if not isinstance(target, dict):
            skipped += 1
            continue
        title = target.get("title")
        qid = target.get("id")
        if not title or not qid:
            skipped += 1
            continue
        excerpt = target.get("excerpt") or entry.get("excerpt") or ""
        item = clean_item({
            "title": title,
            "url": f"https://www.zhihu.com/question/{qid}",
            "rank": len(items) + 1,
            "meta": {
                "heat": _parse_zhihu_heat(entry.get("detail_text")),
                "heat_text": entry.get("detail_text"),
                "summary": excerpt[:220] if excerpt else None,
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
        if len(items) >= 30:
            break
    fetch_zhihu.skipped = skipped
    if not items:
        raise FetchError("zhihu: 0 usable items")
    return items


def fetch_toutiao() -> list[dict]:
    data = get_json(HOT_URLS["toutiao"])
    entries = data.get("data") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise FetchError("toutiao: upstream returned empty data")
    items, skipped = [], 0
    for rank, e in enumerate(entries[:30], start=1):
        if not isinstance(e, dict):
            skipped += 1
            continue
        title = e.get("Title")
        url = e.get("Url")
        if not title or not url:
            skipped += 1
            continue
        cats = e.get("InterestCategory") or []
        cat_raw = cats[0] if isinstance(cats, list) and cats else None
        category = None
        if isinstance(cat_raw, str):
            if any("\u4e00" <= ch <= "\u9fff" for ch in cat_raw):
                category = cat_raw  # 已是中文则保留
            else:
                category = TOUTIAO_CATEGORY_ZH.get(cat_raw)
        label = e.get("Label")
        item = clean_item({
            "title": title,
            "url": url,
            "category": category,
            "rank": rank,
            "meta": {
                "heat": _heat_number(e.get("HotValue")),
                "tag": TOUTIAO_LABEL_ZH.get(label, label) if label else None,
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_toutiao.skipped = skipped
    if not items:
        raise FetchError("toutiao: 0 usable items")
    return items


def fetch_bilibili() -> list[dict]:
    data = get_json(HOT_URLS["bilibili"])
    entries = data.get("data", {}).get("list") if isinstance(data, dict) else None
    if not isinstance(entries, list) or not entries:
        raise FetchError("bilibili: upstream returned empty list")
    items, skipped = [], 0
    for rank, e in enumerate(entries[:30], start=1):
        if not isinstance(e, dict):
            skipped += 1
            continue
        title = e.get("title")
        if not title:
            skipped += 1
            continue
        stat = e.get("stat") if isinstance(e.get("stat"), dict) else {}
        owner = e.get("owner") if isinstance(e.get("owner"), dict) else {}
        desc = e.get("desc") or ""
        url = e.get("short_link_v2") or (
            f"https://www.bilibili.com/video/{e['bvid']}" if e.get("bvid")
            else "https://www.bilibili.com"
        )
        item = clean_item({
            "title": title,
            "url": url,
            "category": e.get("tnamev2") or e.get("tname"),
            "rank": rank,
            "meta": {
                "heat": _heat_number(stat.get("view")),
                "heat_text": f"{stat.get('view')} 播放" if stat.get("view") is not None else None,
                "summary": desc[:160] if desc else None,
                "author": owner.get("name"),
                "bvid": e.get("bvid"),
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_bilibili.skipped = skipped
    if not items:
        raise FetchError("bilibili: 0 usable items")
    return items


# ---------------------------------------------------------------------------
# 羊毛墙（deals）：NodeSeek / V2EX / LINUX DO / 小众软件
# 来源：quan-wang-hao-yang-mao 空间 server/src/actions.ts
# 直连通道用普通 HTTP；搜索补齐通道（spawnTask 网页搜索）在 Actions 跑道里
# 没有等价物，不实现（LINUX DO 因此只能按 403 记 failed/degraded，见 §7.1）。
# 唯一例外：SoV2EX 搜索是普通 HTTP API，可以直接调。
# ---------------------------------------------------------------------------

CST = timezone(timedelta(hours=8))
DC_NS = "http://purl.org/dc/elements/1.1/"


def parse_rss(text: str) -> list[dict]:
    """解析 RSS 2.0，返回 [{title, link, pub_date, creator, description, categories}]。"""
    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise FetchError(f"invalid RSS XML: {exc}") from exc
    out = []
    for it in root.iter("item"):
        def _text(tag, ns=None):
            el = it.find(tag, ns) if ns else it.find(tag)
            return (el.text or "").strip() if el is not None and el.text else None

        pub = _text("pubDate")
        published = None
        if pub:
            try:
                published = parsedate_to_datetime(pub).isoformat()
            except (TypeError, ValueError):
                published = None
        out.append({
            "title": _text("title"),
            "link": _text("link"),
            "pub_date": published,
            "creator": _text("dc:creator", {"dc": DC_NS}),
            "description": _text("description"),
            "categories": [c.text.strip() for c in it.findall("category") if c.text],
        })
    return out


# 七分类：vps / ai / crypto / ucard / bank / free / member
# 机制按空间检查报告移植：小写 title+snippet 加权打分，tie-break 按特异性
# ucard > bank > crypto > vps > ai > member > free；总分 0 丢弃（Appinn 主 feed 例外归 free）。
# 关键词表只包含检查报告中明确提到的词、分类名与搜索词本身；空间的完整
# 关键词表未由检查暴露，分错的条目按 DESIGN.md §7.3 在 Dashboard 置 archived。
CATEGORY_KEYWORDS: dict[str, dict[str, int]] = {
    "ucard": {"u卡": 6, "虚拟卡": 6, "redotpay": 5, "小红卡": 5, "bybit": 4, "starryblu": 4, "开卡": 3},
    "bank": {"港卡": 6, "海外银行": 6, "开户": 5, "众安银行": 5, "蚂蚁银行": 5, "银行": 4, "开户奖励": 5},
    "crypto": {"加密货币": 5, "空投": 5, "交易所": 4, "crypto": 4},
    "vps": {"vps": 5, "服务器": 4, "云服务器": 4, "trade": 1},
    "ai": {"chatgpt": 5, "claude": 5, "免费额度": 4, "代充": 3, "ai": 3, "订阅": 3},
    "member": {"carpool": 6, "拼车": 5, "会员": 4, "订阅": 3},
    "free": {"免费": 4, "白嫖": 4, "限免": 4, "免费白嫖": 5},
}
CATEGORY_TIEBREAK = ["ucard", "bank", "crypto", "vps", "ai", "member", "free"]
FINANCE_CATEGORIES = {"crypto", "ucard", "bank"}
# 金融类严格门与 deal-intent 用词（检查报告明确提到：奖励/返现/白嫖等）。
FINANCE_TITLE_INTENT = ["奖励", "返现", "白嫖", "开户", "空投", "优惠"]
TITLE_INTENT = ["优惠", "奖励", "返现", "白嫖", "免费", "开户", "空投", "限免", "特价", "折扣", "补贴", "红包", "羊毛", "薅"]
NON_DEAL_MARKERS = ["求助", "请问"]  # 求助/提问帖剔除


def _keyword_hit(keyword: str, text: str) -> bool:
    """关键词命中：中文词用子串；纯 ASCII 词要求前后不是 ASCII 字母，
    避免 'ai' 命中 domain/available/email 这类词（'AI中转' 仍能命中，
    因为中文字符不算 ASCII 字母）。"""
    if keyword.isascii() and keyword.isalpha():
        return re.search(r"(?<![A-Za-z])" + re.escape(keyword) + r"(?![A-Za-z])", text) is not None
    return keyword in text


def classify_deal(title: str, snippet: str, replies: int = 0):
    """七分类打分。返回 (category, best, total, deal_score)；
    category 为 None 表示总分 0，应丢弃（Appinn 主 feed 例外，调用方处理）。"""
    text = f"{title or ''} {snippet or ''}".lower()
    scores: dict[str, int] = {}
    for cat, kws in CATEGORY_KEYWORDS.items():
        scores[cat] = sum(w for kw, w in kws.items() if _keyword_hit(kw.lower(), text))
    best = max(scores.values()) if scores else 0
    total = sum(scores.values())
    if total == 0:
        return None, 0, 0, 0.0
    winners = [c for c, s in scores.items() if s == best]
    category = next((c for c in CATEGORY_TIEBREAK if c in winners), winners[0])
    deal_score = best * 2 + min(6, math.log10(1 + max(replies, 0)) * 3) + min(total, 10) / 5
    return category, best, total, deal_score


def parse_expiry(text: str, fetched_at: datetime):
    """有效期只取原帖自述。返回 (expires_at_iso_or_None, expires_note, already_expired)。
    优先级：显式日期 > 相对天数 > 今日/本月 > 节日日期；长期有效返回 None；
    无匹配则 expires_at = fetched_at + 7 天（DESIGN.md §3.3）。已过期的返回 already_expired=True。"""
    if text and "长期有效" in text:
        return None, "长期有效", False
    today = fetched_at.astimezone(CST).date()
    exp_date = None
    if text:
        m = re.search(r"(\d{4})[-/.年](\d{1,2})[-/.月](\d{1,2})", text)
        if not m:
            m = re.search(r"(\d{1,2})[-/.月](\d{1,2})[日号]?", text)
            if m:
                try:
                    exp_date = datetime(today.year, int(m.group(1)), int(m.group(2))).date()
                except ValueError:
                    exp_date = None
        else:
            try:
                exp_date = datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date()
            except ValueError:
                exp_date = None
        if exp_date is None:
            m = re.search(r"(\d+)\s*天内有效|有效期\s*(\d+)\s*天", text)
            if m:
                days = int(m.group(1) or m.group(2))
                exp_date = today + timedelta(days=days)
        if exp_date is None:
            if "今日" in text or "今天" in text:
                exp_date = today
            elif "本月" in text:
                if today.month == 12:
                    exp_date = datetime(today.year, 12, 31).date()
                else:
                    exp_date = datetime(today.year, today.month + 1, 1).date() - timedelta(days=1)
            elif "双十一" in text or "双11" in text:
                exp_date = datetime(today.year, 11, 11).date()
            elif "双十二" in text or "双12" in text:
                exp_date = datetime(today.year, 12, 12).date()
            elif "618" in text:
                exp_date = datetime(today.year, 6, 18).date()
    if exp_date is not None:
        if exp_date < today:
            return None, None, True
        exp_dt = datetime(exp_date.year, exp_date.month, exp_date.day, 23, 59, 59, tzinfo=CST)
        return exp_dt.isoformat(), f"有效期至 {exp_date.year}/{exp_date.month}/{exp_date.day}", False
    exp_dt = fetched_at + timedelta(days=7)
    return (
        exp_dt.isoformat(),
        f"原帖未标注有效期 · 看板保留至 {exp_dt.astimezone(CST).strftime('%Y-%m-%d')}",
        False,
    )


def fetch_nodeseek() -> list[dict]:
    """NodeSeek 直连 RSS。搜索补齐通道是 spawnTask 网页搜索，Actions 跑道无等价物，不实现。"""
    resp = http_get("https://rss.nodeseek.com/",
                    accept="application/rss+xml, application/xml, */*")
    raw_items = parse_rss(resp.text)
    if not raw_items:
        raise FetchError("nodeseek: RSS returned 0 items")
    fetched_at = datetime.now(timezone.utc)
    items, skipped, seen = [], 0, set()
    for r in raw_items:
        link = r["link"] or ""
        if not link.startswith("https://www.nodeseek.com/post-"):
            skipped += 1
            continue
        if link in seen:
            skipped += 1
            continue
        seen.add(link)
        title = r["title"] or ""
        if not title or any(mk in title for mk in NON_DEAL_MARKERS):
            skipped += 1
            continue
        snippet = (r["description"] or "")[:300]
        category, best, total, score = classify_deal(title, snippet)
        if category is None:
            skipped += 1
            continue
        expires_at, note, expired = parse_expiry(title + " " + snippet, fetched_at)
        if expired:
            skipped += 1
            continue
        item = clean_item({
            "title": title,
            "url": link,
            "category": category,
            "published_at": r["pub_date"],
            "expires_at": expires_at,
            "expires_note": note,
            "meta": {
                "snippet": snippet,
                "author": r["creator"],
                "rss_categories": r["categories"],
                "deal_score": round(score, 2),
                "label": "NodeSeek",
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_nodeseek.skipped = skipped
    if not items:
        raise FetchError("nodeseek: 0 usable items after classify/filter")
    return items


SOV2EX_QUERIES = ["众安银行", "港卡", "蚂蚁银行", "开户奖励", "空投", "虚拟卡", "RedotPay"]


def fetch_v2ex() -> list[dict]:
    """V2EX 直连节点 API（promotions + deals）+ SoV2EX 搜索 API 补齐。"""
    fetched_at = datetime.now(timezone.utc)
    candidates: list[dict] = []
    for node in ("promotions", "deals"):
        data = get_json(f"https://www.v2ex.com/api/topics/show.json?node_name={node}")
        if isinstance(data, list):
            for t in data:
                if not isinstance(t, dict):
                    continue
                member = t.get("member") if isinstance(t.get("member"), dict) else {}
                node_info = t.get("node") if isinstance(t.get("node"), dict) else {}
                candidates.append({
                    "title": t.get("title") or "",
                    "url": t.get("url") or "",
                    "snippet": (t.get("content") or "")[:300],
                    "published_at": parse_ts(t.get("created")),
                    "replies": t.get("replies") or 0,
                    "label": "V2EX",
                    "via_search": False,
                    "author": member.get("username"),
                    "node": node_info.get("title"),
                })
        time.sleep(REQUEST_GAP_S)
    # SoV2EX 搜索补齐（普通 HTTP API；失败不拖垮主路）
    for q in SOV2EX_QUERIES:
        try:
            url = "https://www.sov2ex.com/api/search?" + urlencode(
                {"q": q, "sort": "created", "order": 0, "size": 12})
            data = get_json(url)
        except FetchError:
            continue
        hits = data.get("hits") if isinstance(data, dict) else None
        if not isinstance(hits, list):
            continue
        for h in hits:
            src = h.get("_source") if isinstance(h, dict) else None
            if not isinstance(src, dict) or not src.get("id"):
                continue
            candidates.append({
                "title": src.get("title") or "",
                "url": f"https://www.v2ex.com/t/{src['id']}",
                "snippet": (src.get("content") or "")[:300],
                "published_at": parse_ts(src.get("created")),
                "replies": src.get("replies") or 0,
                "label": "V2EX · 全站搜集",
                "via_search": True,
                "author": (src.get("member") or {}).get("username") if isinstance(src.get("member"), dict) else None,
                "node": None,
            })
        time.sleep(REQUEST_GAP_S)
    if not candidates:
        raise FetchError("v2ex: no candidates from node APIs or SoV2EX search")
    items, skipped, seen = [], 0, set()
    for c in candidates:
        url = c["url"]
        if not url or url in seen:
            skipped += 1
            continue
        seen.add(url)
        title = c["title"]
        if not title or any(mk in title for mk in NON_DEAL_MARKERS):
            skipped += 1
            continue
        category, best, total, score = classify_deal(title, c["snippet"], c["replies"])
        if category is None:
            skipped += 1
            continue
        if c["via_search"] and category in FINANCE_CATEGORIES:
            # 金融类严格门：best≥8 且标题含金融 intent 且无新闻/招聘/提问标记
            if (best < 8
                    or not any(w in title for w in FINANCE_TITLE_INTENT)
                    or any(w in title for w in ("新闻", "招聘", "请问", "求助"))):
                skipped += 1
                continue
        expires_at, note, expired = parse_expiry(title + " " + c["snippet"], fetched_at)
        if expired:
            skipped += 1
            continue
        item = clean_item({
            "title": title,
            "url": url,
            "category": category,
            "published_at": c["published_at"],
            "expires_at": expires_at,
            "expires_note": note,
            "meta": {
                "snippet": c["snippet"],
                "author": c.get("author"),
                "node": c.get("node"),
                "replies": c["replies"],
                "deal_score": round(score, 2),
                "label": c["label"],
                "search_finance": c["via_search"],
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_v2ex.skipped = skipped
    if not items:
        raise FetchError("v2ex: 0 usable items after classify/filter")
    return items


def fetch_linuxdo() -> list[dict]:
    """LINUX DO 无直连：latest.json 与福利 RSS 均被 Cloudflare 拦截。
    按 DESIGN.md §7.1：遇到 403/Challenge 直接记 failed + degraded，不绕过。
    搜索搜集通道是 spawnTask 网页搜索，Actions 跑道无等价物，不实现。"""
    try:
        resp = http_get("https://linux.do/latest.json", accept="application/json")
    except BlockedError:
        raise
    except FetchError as exc:
        # 直连被拦时常表现为连接被掐断而非标准 403，同样记为 blocked/degraded。
        raise BlockedError(f"linux.do unreachable (likely blocked): {exc}") from exc
    content_type = resp.headers.get("Content-Type", "")
    if "text/html" in content_type or "challenge" in resp.text[:2000].lower():
        raise BlockedError("linux.do returned a Cloudflare challenge page (no direct access)")
    try:
        data = resp.json()
    except ValueError as exc:
        raise FetchError(f"linuxdo: invalid JSON from latest.json: {exc}") from exc
    topics = None
    if isinstance(data, dict):
        topic_list = data.get("topic_list")
        if isinstance(topic_list, dict):
            topics = topic_list.get("topics")
    if not isinstance(topics, list) or not topics:
        raise FetchError("linuxdo: unexpected latest.json payload")
    fetched_at = datetime.now(timezone.utc)
    items, skipped, seen = [], 0, set()
    for t in topics[:40]:
        if not isinstance(t, dict):
            skipped += 1
            continue
        tid = t.get("id")
        slug = t.get("slug") or ""
        if not tid:
            skipped += 1
            continue
        url = f"https://linux.do/t/{slug}/{tid}" if slug else f"https://linux.do/t/topic/{tid}"
        if url in seen:
            skipped += 1
            continue
        seen.add(url)
        title = (t.get("title") or "").strip()
        if not title or any(mk in title for mk in NON_DEAL_MARKERS):
            skipped += 1
            continue
        category, best, total, score = classify_deal(title, "")
        if category is None:
            skipped += 1
            continue
        if category in FINANCE_CATEGORIES:
            if (best < 8
                    or not any(w in title for w in FINANCE_TITLE_INTENT)
                    or any(w in title for w in ("新闻", "招聘", "请问", "求助"))):
                skipped += 1
                continue
        expires_at, note, expired = parse_expiry(title, fetched_at)
        if expired:
            skipped += 1
            continue
        item = clean_item({
            "title": title,
            "url": url,
            "category": category,
            "published_at": parse_ts(t.get("created_at")),
            "expires_at": expires_at,
            "expires_note": note,
            "meta": {
                "replies": t.get("posts_count"),
                "deal_score": round(score, 2),
                "label": "LINUX DO",
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_linuxdo.skipped = skipped
    if not items:
        raise FetchError("linuxdo: 0 usable items after classify/filter")
    return items


def fetch_xiaozhong() -> list[dict]:
    """小众软件直连双 feed（主 feed + 限免 tag feed），按 URL 去重、限免优先。
    Appinn 主 feed 例外：分类总分 0 也归 free，不丢弃。"""
    fetched_at = datetime.now(timezone.utc)
    feeds = [
        ("https://www.appinn.com/feed/", False),
        ("https://www.appinn.com/tag/%E9%99%90%E5%85%8D/feed/", True),
    ]
    by_url: dict[str, dict] = {}
    for feed_url, is_free_tag in feeds:
        resp = http_get(feed_url, accept="application/rss+xml, application/xml, */*")
        raw_items = parse_rss(resp.text)
        for r in raw_items:
            link = r["link"] or ""
            if not link.startswith("https://www.appinn.com/"):
                continue
            if link in by_url and not is_free_tag:
                continue  # 限免优先：后到的限免 feed 覆盖主 feed 的同 URL
            snippet = (r["description"] or "")[:260]
            if is_free_tag:
                snippet = (snippet + " 限免").strip()
            by_url[link] = {
                "title": r["title"] or "",
                "link": link,
                "pub_date": r["pub_date"],
                "creator": r["creator"],
                "snippet": snippet,
                "is_free_tag": is_free_tag,
                "rss_categories": r["categories"],
            }
        time.sleep(REQUEST_GAP_S)
    if not by_url:
        raise FetchError("xiaozhong: both feeds returned 0 items")
    items, skipped = [], 0
    for c in by_url.values():
        title = c["title"]
        if not title or any(mk in title for mk in NON_DEAL_MARKERS):
            skipped += 1
            continue
        category, best, total, score = classify_deal(title, c["snippet"])
        if category is None:
            category, best, total, score = "free", 0, 0, 0.0  # Appinn 例外归 free
        if category in FINANCE_CATEGORIES:
            # Appinn 金融类须标题分≥5 且有 deal-intent
            title_best = classify_deal(title, "")[1]
            if title_best < 5 or not any(w in title for w in TITLE_INTENT):
                skipped += 1
                continue
        expires_at, note, expired = parse_expiry(title + " " + c["snippet"], fetched_at)
        if expired:
            skipped += 1
            continue
        item = clean_item({
            "title": title,
            "url": c["link"],
            "category": category,
            "published_at": c["pub_date"],
            "expires_at": expires_at,
            "expires_note": note,
            "meta": {
                "snippet": c["snippet"],
                "author": c["creator"],
                "rss_categories": c["rss_categories"],
                "deal_score": round(score, 2),
                "label": "小众软件 · 限免" if c["is_free_tag"] else "小众软件",
            },
        })
        if item is None:
            skipped += 1
        else:
            items.append(item)
    fetch_xiaozhong.skipped = skipped
    if not items:
        raise FetchError("xiaozhong: 0 usable items after classify/filter")
    return items


FETCHERS = {
    # hot
    "baidu": fetch_baidu,
    "toutiao": fetch_toutiao,
    "zhihu": fetch_zhihu,
    "bilibili": fetch_bilibili,
    # deals
    "nodeseek": fetch_nodeseek,
    "v2ex": fetch_v2ex,
    "linuxdo": fetch_linuxdo,
    "xiaozhong": fetch_xiaozhong,
    # jettt
    "jettt_mint": fetch_jettt_mint,
    "unity_launches": fetch_unity_launches,
    "unity_ranking": fetch_unity_ranking,
    "smart_wallets": fetch_smart_wallets,
}

SOURCE_BOARD = {
    "baidu": "hot", "toutiao": "hot", "zhihu": "hot", "bilibili": "hot",
    "nodeseek": "deals", "v2ex": "deals", "linuxdo": "deals", "xiaozhong": "deals",
    "jettt_mint": "jettt", "unity_launches": "jettt",
    "unity_ranking": "jettt", "smart_wallets": "jettt",
}


# ---------------------------------------------------------------------------
# Supabase REST（只用 requests，不引 supabase-py）
# ---------------------------------------------------------------------------

class SupabaseClient:
    def __init__(self, url: str, service_key: str):
        self.base = url.rstrip("/")
        self.headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "return=minimal",
        }

    def _req(self, method: str, path: str, **kwargs):
        resp = requests.request(
            method, self.base + path, headers=self.headers, timeout=TIMEOUT, **kwargs
        )
        resp.raise_for_status()
        return resp

    def existing_items(self, source_id: str) -> dict:
        """取该 source 已有行的 url -> {category, expires_at, expires_note}，
        upsert 时不覆盖人工改过的这两个字段（DESIGN.md §3.2）。"""
        existing: dict[str, dict] = {}
        try:
            resp = self._req(
                "GET",
                f"/rest/v1/items?source_id=eq.{source_id}"
                "&select=url,category,expires_at,expires_note&limit=1000",
            )
            for row in resp.json():
                existing[row["url"]] = row
        except Exception:
            # 查不到就按“全部当新行”处理，不阻塞抓取；upsert 仍会写库。
            return {}
        return existing

    def upsert_items(self, rows: list[dict]) -> int:
        if not rows:
            return 0
        headers = dict(self.headers)
        headers["Prefer"] = "resolution=merge-duplicates"
        resp = requests.post(
            self.base + "/rest/v1/items?on_conflict=url",
            headers=headers,
            json=rows,
            timeout=TIMEOUT,
        )
        resp.raise_for_status()
        return len(rows)

    def insert_fetch_log(self, row: dict) -> None:
        self._req("POST", "/rest/v1/fetch_logs", json=row)

    def update_source(self, source_id: str, status: str, error: str | None) -> None:
        self._req(
            "PATCH",
            f"/rest/v1/sources?id=eq.{source_id}",
            json={
                "last_status": status,
                "last_fetched_at": now_iso(),
                "last_error": (error or "")[:500] if error else None,
            },
        )


def build_upsert_rows(items: list[dict], source_id: str, board: str,
                      existing: dict, fetched_at: str) -> list[dict]:
    """拼 upsert 行：已存在且人工改过 category/expires_at 的行不覆盖这两个字段。"""
    rows: list[dict] = []
    for item in items:
        row = {
            "source_id": source_id,
            "board": board,
            "title": item["title"],
            "url": item["url"],
            "rank": item.get("rank"),
            "published_at": item.get("published_at"),
            "fetched_at": fetched_at,
            "meta": item.get("meta") or {},
            "raw": item.get("raw") or {},
            "status": "active",
        }
        old = existing.get(item["url"])
        if old is None:
            # 新行：分类 / 有效期按抓取结果写。
            row["category"] = item.get("category")
            row["expires_at"] = item.get("expires_at")
            row["expires_note"] = item.get("expires_note")
        else:
            # 旧行：只在抓取方没给值、或旧值为空时才写；人工改过的值保留。
            if item.get("category") is not None and old.get("category") is None:
                row["category"] = item["category"]
            if item.get("expires_at") is not None and old.get("expires_at") is None:
                row["expires_at"] = item["expires_at"]
                row["expires_note"] = item.get("expires_note")
            elif item.get("expires_note") is not None and old.get("expires_note") is None:
                row["expires_note"] = item["expires_note"]
        rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def run(source_id: str, board: str, dry_run: bool) -> int:
    started = time.monotonic()
    started_at = now_iso()
    runner = "github-actions" if os.environ.get("GITHUB_ACTIONS") else "manual"
    status = "failed"
    items: list[dict] = []
    skipped = 0
    error: str | None = None
    log_written = False

    try:
        if SOURCE_BOARD.get(source_id) != board:
            raise FetchError(
                f"source {source_id} belongs to board {SOURCE_BOARD.get(source_id)}, "
                f"not {board}"
            )
        fetcher = FETCHERS[source_id]
        items = fetcher()
        skipped = getattr(fetcher, "skipped", 0)
        # items.url 全局唯一：同一轮内重复 URL 只保留第一条（rank 最小），
        # 否则 PostgREST upsert 会因 "ON CONFLICT DO UPDATE ... affect row a second time" 整批失败。
        seen_urls: set[str] = set()
        deduped: list[dict] = []
        for it in items:
            if it["url"] in seen_urls:
                skipped += 1
                continue
            seen_urls.add(it["url"])
            deduped.append(it)
        items = deduped
        if not items:
            raise FetchError(f"{source_id}: fetched 0 usable items")
        status = "ok" if skipped == 0 else "partial"
    except BlockedError as exc:
        status = "failed"
        error = f"blocked: {str(exc)[:500]}"
    except FetchError as exc:
        status = "failed"
        error = str(exc)[:500]
    except Exception as exc:  # noqa: BLE001 - 任何未预料的错都记 failed，不抛到 Actions 日志外
        status = "failed"
        error = f"unexpected: {type(exc).__name__}: {str(exc)[:450]}"

    duration_ms = int((time.monotonic() - started) * 1000)
    log_row = {
        "source_id": source_id,
        "board": board,
        "started_at": started_at,
        "finished_at": now_iso(),
        "status": status,
        "items_fetched": len(items),
        "items_upserted": 0,
        "items_skipped": skipped,
        "error": error,
        "duration_ms": duration_ms,
        "runner": runner,
    }

    if dry_run:
        print(json.dumps(
            {"dry_run": True, "source": source_id, "board": board,
             "items": items, "fetch_log": log_row},
            ensure_ascii=False, indent=2,
        ))
        print(
            f"[dry-run] {source_id}/{board}: status={status} "
            f"fetched={len(items)} skipped={skipped} "
            f"duration_ms={duration_ms}"
            + (f" error={error}" if error else ""),
            file=sys.stderr,
        )
        return 0 if status in ("ok", "partial") else 1

    # 非 dry-run：写库。密钥只从环境变量来，绝不写进代码或日志。
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        print(
            "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY not set; "
            "cannot write items or fetch_logs",
            file=sys.stderr,
        )
        return 1

    client = SupabaseClient(url, key)
    try:
        if items:
            existing = client.existing_items(source_id)
            rows = build_upsert_rows(items, source_id, board, existing, started_at)
            log_row["items_upserted"] = client.upsert_items(rows)
        try:
            client.insert_fetch_log(log_row)
            log_written = True
        except Exception as exc:  # noqa: BLE001 - 日志写失败也要记下来
            print(f"failed to write fetch_logs: {exc}", file=sys.stderr)
        try:
            last_status = (
                "ok" if status == "ok"
                else "degraded" if status == "partial" or isinstance(error, str) and error.startswith("blocked")
                else "failed"
            )
            client.update_source(source_id, last_status, error)
        except Exception as exc:  # noqa: BLE001
            print(f"failed to update sources: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - 写库失败：日志能写就写，不能写就返回非零
        print(f"supabase write failed: {exc}", file=sys.stderr)
        if not log_written:
            try:
                log_row["status"] = "failed"
                log_row["error"] = f"supabase write failed: {str(exc)[:450]}"
                log_row["items_upserted"] = 0
                client.insert_fetch_log(log_row)
                log_written = True
            except Exception:
                pass
        return 0 if log_written else 1

    # 契约：全成功/部分成功返回 0；失败但日志已写也返回 0（失败留痕在库里）；
    # 只有“完全没抓到且没写日志”才非零——上面已处理，走到这里日志已写。
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="2号线：单路抓取并写 Supabase")
    parser.add_argument("--source", required=True, choices=sorted(FETCHERS),
                        help="source_id，如 baidu / v2ex / jettt_mint")
    parser.add_argument("--board", required=True, choices=BOARDS,
                        help="hot / deals / jettt / low_threshold")
    parser.add_argument("--dry-run", action="store_true",
                        help="只抓取+解析、不写库，打印将写入的 items 与 fetch_logs 行")
    args = parser.parse_args(argv)
    return run(args.source, args.board, args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())
