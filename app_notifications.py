"""Lightweight state feed for the HBA Android app's notification poller.

Reads only what already exists (in-memory/disk/Supabase picks cache and
Supabase vip_pick_history). It never calls API-Football, never computes a
slate and never schedules a warmup/refresh. No pick details are exposed:
only counts and an opaque slate hash.
"""
import hashlib
import threading
import time
from typing import Optional

import vip_picks as vp
from vip_history import fetch_rows

_MEMO_TTL = 60  # seconds; the app polls every 15 minutes, many clients share this
_memo = {"ts": 0.0, "data": None}
_memo_lock = threading.Lock()

_GRADED = {"ganado", "perdido", "nulo"}


def _norm_line(v) -> str:
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v or "")


def _pick_sig(p: dict) -> str:
    parts = [str(p.get(k) or "").strip().lower() for k in ("sport", "match", "market_label", "selection")]
    parts.append(_norm_line(p.get("line")))
    return "|".join(parts)


def _today_slate() -> tuple[list[dict], str]:
    """Today's main card from cache only. Returns (picks, source)."""
    today = vp._today()
    try:
        found = vp._cache_lookup(vp._cache_key("all", None, today, "auto"), allow_stale=True)
    except Exception:
        found = None
    if found and isinstance(found.get("data"), dict) and found["data"].get("picks") is not None:
        return list(found["data"].get("picks") or []), "cache"
    picks: list[dict] = []
    got = False
    for sport in ("football", "nba"):
        try:
            part = vp._cache_lookup(vp._cache_key(sport, None, today, "auto"), allow_stale=True)
        except Exception:
            part = None
        if part and isinstance(part.get("data"), dict):
            got = True
            picks.extend(part["data"].get("picks") or [])
    if got:
        return picks, "cache"
    return [], "none"


def _compute() -> dict:
    today = vp._today()
    try:
        rows = fetch_rows(limit=1000)
    except Exception:
        rows = []
    # Same rule as the Historial view: each day keeps only its own matches.
    rows = [
        r for r in rows
        if not r.get("kickoff") or str(r.get("kickoff"))[:10] == str(r.get("pick_date") or "")[:10]
    ]

    # Primary: today's published (frozen) card in vip_pick_history, which is
    # stable across restarts/deploys. Fallback: the cached slate.
    today_rows = [r for r in rows if str(r.get("pick_date") or "")[:10] == today]
    if today_rows:
        picks = [{
            "sport": r.get("sport"), "match": r.get("match"),
            "market_label": r.get("market_label"), "selection": r.get("selection"),
            "line": r.get("line"), "maxima_confianza": bool(r.get("maxima_confianza")),
        } for r in today_rows]
        source = "history"
    else:
        picks, source = _today_slate()

    sigs = sorted({_pick_sig(p) for p in picks if isinstance(p, dict)})
    slate_id = None
    if sigs:
        slate_id = hashlib.sha1((today + "\n" + "\n".join(sigs)).encode("utf-8")).hexdigest()[:16]
    max_conf = sum(1 for p in picks if isinstance(p, dict) and p.get("maxima_confianza"))

    by_day: dict[str, list] = {}
    for r in rows:
        if (r.get("result") or "").lower() in _GRADED:
            by_day.setdefault(str(r.get("pick_date") or "")[:10], []).append(r)
    latest = max(by_day.keys()) if by_day else None
    graded = by_day.get(latest, []) if latest else []
    won = sum(1 for r in graded if (r.get("result") or "").lower() == "ganado")
    lost = sum(1 for r in graded if (r.get("result") or "").lower() == "perdido")
    void = sum(1 for r in graded if (r.get("result") or "").lower() == "nulo")

    return {
        "ok": True,
        "date": today,
        "timezone": "America/Bogota",
        "slate_id": slate_id,
        "picks_count": len(sigs),
        "max_confidence_count": max_conf,
        "slate_source": source,
        "history_latest_date": latest,
        "history_graded_count": len(graded),
        "history_won": won,
        "history_lost": lost,
        "history_void": void,
        "generated_at": int(time.time()),
    }


def app_notifications_state() -> dict:
    now = time.time()
    with _memo_lock:
        data = _memo["data"]
        if data and now - _memo["ts"] < _MEMO_TTL and data.get("date") == vp._today():
            return data
    data = _compute()
    with _memo_lock:
        _memo["ts"] = now
        _memo["data"] = data
    return data
