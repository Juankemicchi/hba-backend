"""Daily VIP picks built from the same HBA stats code used by the dashboard.

Grok (when XAI_API_KEY is set) only writes short reasons from already
computed numbers. It never invents hit rates or sample sizes.
"""

from __future__ import annotations

import copy
import json
import re
import statistics
import os
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import football_module as fm
import football_stats as fs
import nba_module as nba
import vip_nba_props as nprops
from openai import OpenAI

BOGOTA = ZoneInfo("America/Bogota")
SLATE_TTL = 45 * 60
STALE_MAX = 12 * 60 * 60
MAX_FIXTURES = 6
NBA_MAX_FIXTURES = 12
LAST_N = 15
CACHE_FILE = Path(os.getenv("VIP_PICKS_CACHE_FILE", "/tmp/hba_vip_picks_cache.json"))
WARM_WORKERS = 2

FOOTBALL_COMPETITIONS = [
    {"id": 239, "name": "Liga BetPlay", "sport": "football"},
    {"id": 39, "name": "Premier League", "sport": "football"},
    {"id": 140, "name": "La Liga", "sport": "football"},
    {"id": 135, "name": "Serie A", "sport": "football"},
    {"id": 78, "name": "Bundesliga", "sport": "football"},
    {"id": 61, "name": "Ligue 1", "sport": "football"},
    {"id": 128, "name": "Liga Profesional", "sport": "football"},
    {"id": 71, "name": "Brasileirão", "sport": "football"},
    {"id": 2, "name": "Champions League", "sport": "football"},
    {"id": 13, "name": "Libertadores", "sport": "football"},
    {"id": 253, "name": "MLS", "sport": "football"},
    {"id": 262, "name": "Liga MX", "sport": "football"},
]
ALL_LEAGUE_IDS = [c["id"] for c in FOOTBALL_COMPETITIONS]

# Modest default "all" set — popular + local. Still capped at MAX_FIXTURES.
ALL_PRIORITY_IDS = [239, 71, 39, 140, 2, 135, 13]

_cache_lock = threading.RLock()
_memory_cache: dict = {}
_fixture_picks_lock = threading.Lock()
_fixture_picks_cache: dict = {}
_inflight: dict = {}
_refreshing: set = set()
_bg_started = False
_bg_lock = threading.Lock()


def _today() -> str:
    return datetime.now(BOGOTA).date().isoformat()


def _parse_kickoff(iso_str) -> Optional[datetime]:
    """Parse a kickoff into America/Bogota. Date-only strings are rejected.

    Already-formatted Bogota labels (YYYY-MM-DD HH:MM) stay in Bogota, not UTC.
    """
    if iso_str is None or iso_str == "":
        return None
    if isinstance(iso_str, (int, float)) or (
        isinstance(iso_str, str) and re.fullmatch(r"\d{10,13}", iso_str.strip())
    ):
        try:
            ts = float(iso_str)
            if ts > 1e12:
                ts /= 1000.0
            return datetime.fromtimestamp(ts, tz=ZoneInfo("UTC")).astimezone(BOGOTA)
        except (TypeError, ValueError, OSError):
            return None
    raw = str(iso_str).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}", raw):
        try:
            return datetime.strptime(raw, "%Y-%m-%d %H:%M").replace(tzinfo=BOGOTA)
        except ValueError:
            return None
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", raw):
        return None
    try:
        raw2 = raw.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw2)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(BOGOTA)
    except Exception:
        return None


def _kickoff_label(iso_str) -> Optional[str]:
    dt = _parse_kickoff(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else None


def list_vip_competitions() -> dict:
    return {
        "sports": [
            {"id": "all", "name": "Todos"},
            {"id": "nba", "name": "NBA", "sport": "nba"},
            {"id": "football", "name": "Fútbol", "sport": "football"},
        ],
        "football_leagues": FOOTBALL_COMPETITIONS,
        "timezone": "America/Bogota",
        "date": _today(),
    }


def _cache_key(sport: str, league_id: Optional[int], date_str: str, window: str) -> str:
    return f"v7|{date_str}|{sport}|{league_id or 'all'}|{window}"


def _cache_fresh(item: Optional[dict], now: Optional[float] = None, allow_stale: bool = False):
    if not item:
        return None
    now = now if now is not None else time.time()
    ts = float(item.get("ts") or 0)
    age = now - ts
    data = item.get("data")
    if not isinstance(data, dict):
        return None
    date_str = data.get("date")
    if date_str and date_str != _today():
        return None
    if age < SLATE_TTL:
        return {"data": data, "ts": ts, "age": age, "stale": False}
    if allow_stale and age < STALE_MAX:
        return {"data": data, "ts": ts, "age": age, "stale": True}
    return None


def _supabase_rest():
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    if not url or not key:
        return None, None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return url, headers


def _supabase_cache_get(key: str):
    url, headers = _supabase_rest()
    if not url:
        return None
    try:
        q = urllib.parse.quote(key, safe="")
        req = urllib.request.Request(
            f"{url}/rest/v1/vip_picks_cache?cache_key=eq.{q}&select=ts,date_str,payload",
            headers=headers,
            method="GET",
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            rows = json.loads(resp.read().decode())
        if not rows:
            return None
        row = rows[0]
        return {"ts": float(row.get("ts") or 0), "data": row.get("payload")}
    except Exception:
        return None


def _supabase_cache_set(key: str, item: dict):
    url, headers = _supabase_rest()
    if not url:
        return
    try:
        hdrs = dict(headers)
        hdrs["Prefer"] = "resolution=merge-duplicates,return=minimal"
        payload = json.dumps({
            "cache_key": key,
            "ts": item.get("ts"),
            "date_str": (item.get("data") or {}).get("date") or _today(),
            "payload": item.get("data"),
        }).encode()
        req = urllib.request.Request(
            f"{url}/rest/v1/vip_picks_cache?on_conflict=cache_key",
            data=payload,
            headers=hdrs,
            method="POST",
        )
        urllib.request.urlopen(req, timeout=8).read()
    except Exception:
        pass


def _cache_lookup(key: str, allow_stale: bool = False):
    now = time.time()
    with _cache_lock:
        found = _cache_fresh(_memory_cache.get(key), now, allow_stale=allow_stale)
        if found:
            return found
        item = None
        try:
            if CACHE_FILE.exists():
                store = json.loads(CACHE_FILE.read_text())
                item = (store or {}).get(key)
        except Exception:
            item = None
        found = _cache_fresh(item, now, allow_stale=allow_stale)
        if found:
            _memory_cache[key] = {"ts": found["ts"], "data": found["data"]}
            return found
    remote = _supabase_cache_get(key)
    found = _cache_fresh(remote, now, allow_stale=allow_stale)
    if found:
        with _cache_lock:
            _memory_cache[key] = {"ts": found["ts"], "data": found["data"]}
        return found
    return None


def _cache_get(key: str):
    found = _cache_lookup(key, allow_stale=False)
    return found["data"] if found else None


def _cache_set(key: str, data: dict):
    item = {"ts": time.time(), "data": data}
    with _cache_lock:
        _memory_cache[key] = item
        try:
            store = {}
            if CACHE_FILE.exists():
                store = json.loads(CACHE_FILE.read_text()) or {}
            now = time.time()
            store = {k: v for k, v in store.items() if now - v.get("ts", 0) < STALE_MAX}
            store[key] = item
            CACHE_FILE.write_text(json.dumps(store, ensure_ascii=False))
        except Exception:
            pass
    _supabase_cache_set(key, item)
    try:
        from vip_history import publish_slate
        publish_slate(data)
    except Exception:
        pass


def _xai_client() -> Optional[OpenAI]:
    key = os.getenv("XAI_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key, base_url="https://api.x.ai/v1")


def _risk(hit: float, games: int) -> str:
    if games < 5:
        return "alta"
    if hit >= 75 and games >= 12:
        return "baja"
    if hit >= 65:
        return "media"
    return "alta"


def _confidence(hit: float, games: int, lean: str) -> int:
    if games < 5:
        return 2
    if lean in ("NEUTRAL", "") and 40 <= hit <= 60:
        return 2
    if hit >= 75 and games >= 12:
        return 5
    if hit >= 70:
        return 4
    if hit >= 60:
        return 3
    return 2


def _score(hit: float, games: int) -> float:
    if games < 5 or hit < 60:
        return 0
    return float(hit) * (1 + min(games, 15) / 30.0)



def markets_from_analysis(analysis: Optional[dict]) -> list[dict]:
    """Normalize HBA analyze payloads (stats dict) into a list of market rows."""
    if not analysis:
        return []
    if isinstance(analysis.get("markets"), list) and analysis.get("markets"):
        rows = []
        for row in analysis["markets"]:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            if not item.get("recommend"):
                try:
                    item["recommend"] = fs.recommend(item)
                except Exception:
                    item["recommend"] = {}
            rows.append(item)
        return rows
    stats = analysis.get("stats")
    if isinstance(stats, dict):
        rows = []
        for key, val in stats.items():
            if not isinstance(val, dict):
                continue
            item = dict(val)
            item.setdefault("key", key)
            try:
                item["recommend"] = item.get("recommend") or fs.recommend(item)
            except Exception:
                item["recommend"] = {}
            rows.append(item)
        return rows
    if isinstance(stats, list):
        return [s for s in stats if isinstance(s, dict)]
    return []


def _statistical_reason(pick: dict) -> str:
    if pick.get("sport") == "nba" and pick.get("kind") == "player_prop":
        return nprops.statistical_reason(pick)
    line = pick.get("line")
    line_txt = f" {pick.get('selection') or ''} {line}".strip() if line is not None else (pick.get("selection") or "")
    text = (
        f"{pick.get('entity')}: en los últimos {pick.get('games')} partidos, "
        f"{pick.get('market_label')} {line_txt} acertó el {pick.get('hit_rate')}% "
        f"({pick.get('hits')}/{pick.get('games')})."
    )
    if pick.get("bet_name"):
        mercado = pick["bet_name"]
        if pick.get("odds_value"):
            mercado += f" · {pick['odds_value']}"
        text += f" Mercado de casas: {mercado}."
    if pick.get("avg_odd") is not None:
        text += (
            f" Cuota promedio {pick['avg_odd']} "
            f"({pick.get('odd_min')}–{pick.get('odd_max')}, {pick.get('bookmakers')} casas). "
            f"Edge conservador {pick.get('edge')} p.p. frente a la probabilidad implícita "
            f"({pick.get('implied_prob')}%), ajustado por el tamaño de la muestra."
        )
        if pick.get("edge_flag") and pick.get("edge_raw") is not None:
            text += f" El edge crudo de la muestra sería {pick['edge_raw']} p.p."
    return text


def _explain_with_grok(picks: list[dict]) -> list[dict]:
    client = _xai_client()
    if not client or not picks:
        for pick in picks:
            pick["reason"] = _statistical_reason(pick)
        return picks
    compact = []
    for pick in picks:
        compact.append({
            "entity": pick.get("entity"),
            "match": pick.get("match"),
            "market_label": pick.get("market_label"),
            "selection": pick.get("selection"),
            "line": pick.get("line"),
            "hit_rate": pick.get("hit_rate"),
            "hits": pick.get("hits"),
            "games": pick.get("games"),
            "avg_odd": pick.get("avg_odd"),
            "odd_min": pick.get("odd_min"),
            "odd_max": pick.get("odd_max"),
            "bookmakers": pick.get("bookmakers"),
            "edge": pick.get("edge"),
            "edge_raw": pick.get("edge_raw"),
            "implied_prob": pick.get("implied_prob"),
            "bet_name": pick.get("bet_name"),
            "odds_value": pick.get("odds_value"),
            "kind": pick.get("kind"),
            "avg": pick.get("avg"),
            "avg_season": pick.get("avg_season"),
            "median": pick.get("median"),
            "avg_minutes": pick.get("avg_minutes"),
            "line_estimated": pick.get("line_estimated"),
            "line_label": pick.get("line_label"),
            "book_line": pick.get("book_line"),
            "hit_rate_season": pick.get("hit_rate_season"),
        })
    prompt = (
        "Eres el analista VIP de HBA. Escribe UNA frase corta en español por cada pick, "
        "en el mismo orden. Usa EXCLUSIVAMENTE los números dados. "
        "Si el pick es un prop NBA: menciona el promedio reciente, la mediana, el hit rate "
        "y deja claro que la línea es una LÍNEA ESTIMADA de HBA, no una cuota de casa, "
        "salvo que venga book_line, en cuyo caso cita esa 'línea en casas' sin nombrar fuentes ni proveedores. "
        "No presentes una estimación como cuota real. No menciones Grok, APIs, ESPN ni URLs. "
        "Si el pick es de fútbol: menciona la cuota promedio y el edge conservador. "
        "No inventes estadísticas, lesiones, cuotas ni noticias. No des consejos de apuesta. "
        "Devuelve un JSON array de strings, nada más.\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    try:
        resp = client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
        )
        text = (resp.choices[0].message.content or "").strip()
        if text.startswith("```"):
            text = text.strip("`")
            if text.lower().startswith("json"):
                text = text[4:].strip()
        reasons = json.loads(text)
        if isinstance(reasons, list) and len(reasons) == len(picks):
            for pick, reason in zip(picks, reasons):
                pick["reason"] = str(reason).strip() or _statistical_reason(pick)
            return picks
    except Exception:
        pass
    for pick in picks:
        pick["reason"] = _statistical_reason(pick)
    return picks


def _map_fixture(item: dict, league_id: int) -> dict:
    base = fm._map_fixture(item)
    fx = item.get("fixture") or {}
    league = item.get("league") or {}
    base["fixture_id"] = fx.get("id")
    base["league_id"] = league.get("id") or league_id
    base["league"] = league.get("name") or base.get("league")
    base["kickoff"] = fx.get("date")
    return base


def _fixtures_on_date(league_id: int, date_str: str) -> list[dict]:
    try:
        season = fm.resolve_season(league_id)
        data = fm.api_get(
            "/fixtures",
            {"league": int(league_id), "season": int(season), "date": date_str},
            ttl=SLATE_TTL,
        )
    except Exception:
        return []
    out = []
    for item in data.get("response") or []:
        mapped = _map_fixture(item, league_id)
        status = (mapped.get("status") or "").upper()
        if status in {"CANC", "PST", "ABD", "AWD", "WO"}:
            continue
        out.append(mapped)
    return out


def _bogota_date(iso_str: Optional[str]) -> Optional[str]:
    dt = _parse_kickoff(iso_str)
    return dt.date().isoformat() if dt else None


def _collect_football_fixtures(league_id: Optional[int], dates_bogota: list[str], ids: Optional[list[int]] = None) -> list[dict]:
    """Fetch fixtures whose kickoff falls on the given America/Bogota calendar days.

    API-Football filters by UTC date, so a late Colombian kickoff can appear on the
    next UTC date. Query one extra UTC day and filter by Bogotá date.
    """
    if not dates_bogota:
        return []
    need = set(dates_bogota)
    first = min(datetime.fromisoformat(d).date() for d in dates_bogota)
    last = max(datetime.fromisoformat(d).date() for d in dates_bogota) + timedelta(days=1)
    utc_dates = []
    cursor = first
    while cursor <= last:
        utc_dates.append(cursor.isoformat())
        cursor += timedelta(days=1)
    league_ids = [int(league_id)] if league_id else list(ids or ALL_PRIORITY_IDS)
    fixtures = []
    seen = set()
    for lid in league_ids:
        for usd in utc_dates:
            for fx in _fixtures_on_date(lid, usd):
                bdate = _bogota_date(fx.get("kickoff") or fx.get("date"))
                if bdate not in need:
                    continue
                fid = fx.get("fixture_id") or (fx.get("home"), fx.get("away"), fx.get("date"))
                if fid in seen:
                    continue
                seen.add(fid)
                fixtures.append(fx)
        if league_id is None and len(fixtures) >= MAX_FIXTURES * 2:
            break
    return fixtures


_FINISHED_STATUS = {
    "FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO",
    "POSTPONED", "CANCELLED", "CANCELED", "CANCELED", "FINAL", "POST",
}
_LIVE_STATUS = {
    "1H", "2H", "HT", "ET", "P", "BT", "LIVE", "INPLAY", "IN_PROGRESS",
    "Q1", "Q2", "Q3", "Q4", "OT", "BT",
}


def _upcoming_only(fixtures: list[dict]) -> list[dict]:
    keep = []
    now = datetime.now(BOGOTA)
    for fx in fixtures:
        status = (fx.get("status") or "").upper()
        if status in _FINISHED_STATUS:
            continue
        dt = _parse_kickoff(fx.get("kickoff") or fx.get("date"))
        if dt and dt < now - timedelta(hours=2) and status not in _LIVE_STATUS:
            continue
        keep.append(fx)
    return keep


def _trim_fixtures(fixtures: list[dict], limit: Optional[int] = None) -> list[dict]:
    def sort_key(fx):
        dt = _parse_kickoff(fx.get("kickoff") or fx.get("date"))
        return dt or datetime.max.replace(tzinfo=BOGOTA)

    fixtures = sorted(fixtures, key=sort_key)
    return fixtures[:(limit or MAX_FIXTURES)]


def _market_from_props(analysis: dict, key: str) -> Optional[dict]:
    for market in markets_from_analysis(analysis):
        if market.get("key") == key:
            return market
    return None


def _candidate_from_market(analysis: dict, market: dict, fixture: dict, entity_side: str) -> Optional[dict]:
    rec = market.get("recommend") or {}
    lean = rec.get("lean") or "NEUTRAL"
    kind = market.get("kind") or "line"
    games = int(market.get("games") or rec.get("games") or 0)
    if kind == "yesno":
        lean = rec.get("lean") or lean
        if lean == "SI":
            side = "yes"
        elif lean == "NO":
            side = "no"
        else:
            side = "yes" if float(market.get("yes_rate") or 0) >= 50 else "no"
        hit = float(market.get("yes_rate") if side == "yes" else market.get("no_rate") or 0)
        hits = int(round(hit / 100.0 * games)) if games else 0
        selection = "Sí" if side == "yes" else "No"
        line = None
        if lean == "NEUTRAL" and hit < 65:
            return None
    else:
        over_hit = float(market.get("hit_rate") or 0)
        if lean == "UNDER":
            hit = round(100.0 - over_hit, 1)
            selection = "Under"
        elif lean == "OVER":
            hit = over_hit
            selection = "Over"
        else:
            return None
        hits = int(market.get("hits") or rec.get("hits") or 0)
        if lean == "UNDER":
            hits = max(games - int(market.get("hits") or 0), 0)
        line = market.get("line")
    if _score(hit, games) <= 0:
        return None
    team = analysis.get("team") or {}
    return {
        "sport": "football",
        "market": market.get("key"),
        "market_label": market.get("label") or market.get("key"),
        "selection": selection,
        "line": line,
        "hit_rate": round(hit, 1),
        "hits": hits,
        "games": games,
        "sample_size": games,
        "confidence": _confidence(hit, games, lean),
        "risk": _risk(hit, games),
        "entity": team.get("name") or analysis.get("name"),
        "entity_id": team.get("id") or analysis.get("team_id"),
        "entity_side": entity_side,
        "match": f"{fixture.get('home')} vs {fixture.get('away')}",
        "kickoff": _kickoff_label(fixture.get("kickoff") or fixture.get("date")),
        "league": fixture.get("league"),
        "league_id": fixture.get("league_id"),
        "fixture_id": fixture.get("fixture_id"),
        "logo": team.get("logo") or (fixture.get("home_logo") if entity_side == "home" else fixture.get("away_logo")),
        "score": _score(hit, games),
        "avg": market.get("avg"),
    }


def _form_win_rate(analysis: dict) -> tuple[Optional[float], int, int]:
    form = (analysis.get("form") or analysis.get("record") or "")
    rec = analysis.get("record")
    if isinstance(rec, dict) and rec.get("played"):
        wins = int(rec.get("wins") or 0)
        games = int(rec.get("played") or 0)
        if games:
            return round(100.0 * wins / games, 1), wins, games
    if isinstance(form, dict):
        return None, 0, 0
    form = str(form).replace("-", "").replace(" ", "")
    if not form:
        return None, 0, 0
    letters = [ch for ch in form.upper() if ch in "WDL"]
    if not letters:
        return None, 0, 0
    wins = letters.count("W")
    games = len(letters)
    return round(100.0 * wins / games, 1), wins, games


def _moneyline_candidates(home_a: dict, away_a: dict, fixture: dict) -> list[dict]:
    out = []
    home_rate, home_wins, home_games = _form_win_rate(home_a)
    away_rate, away_wins, away_games = _form_win_rate(away_a)
    if home_rate is not None and _score(home_rate, home_games) > 0 and (away_rate is None or away_rate <= 45):
        out.append({
            "sport": "football",
            "market": "1x2",
            "market_label": "1X2 / ganador",
            "selection": fixture.get("home"),
            "line": None,
            "hit_rate": home_rate,
            "hits": home_wins,
            "games": home_games,
            "sample_size": home_games,
            "confidence": _confidence(home_rate, home_games, "OVER"),
            "risk": _risk(home_rate, home_games),
            "entity": fixture.get("home"),
            "entity_id": fixture.get("home_id"),
            "entity_side": "home",
            "match": f"{fixture.get('home')} vs {fixture.get('away')}",
            "kickoff": _kickoff_label(fixture.get("kickoff") or fixture.get("date")),
            "league": fixture.get("league"),
            "league_id": fixture.get("league_id"),
            "fixture_id": fixture.get("fixture_id"),
            "logo": fixture.get("home_logo"),
            "score": _score(home_rate, home_games),
            "note": "Hit rate = % de victorias del local en su forma reciente (mismos partidos que usa HBA).",
        })
    if away_rate is not None and _score(away_rate, away_games) > 0 and (home_rate is None or home_rate <= 45):
        out.append({
            "sport": "football",
            "market": "1x2",
            "market_label": "1X2 / ganador",
            "selection": fixture.get("away"),
            "line": None,
            "hit_rate": away_rate,
            "hits": away_wins,
            "games": away_games,
            "sample_size": away_games,
            "confidence": _confidence(away_rate, away_games, "OVER"),
            "risk": _risk(away_rate, away_games),
            "entity": fixture.get("away"),
            "entity_id": fixture.get("away_id"),
            "entity_side": "away",
            "match": f"{fixture.get('home')} vs {fixture.get('away')}",
            "kickoff": _kickoff_label(fixture.get("kickoff") or fixture.get("date")),
            "league": fixture.get("league"),
            "league_id": fixture.get("league_id"),
            "fixture_id": fixture.get("fixture_id"),
            "logo": fixture.get("away_logo"),
            "score": _score(away_rate, away_games),
            "note": "Hit rate = % de victorias del visitante en su forma reciente (mismos partidos que usa HBA).",
        })
    return out


def _match_btts(home_a: dict, away_a: dict, fixture: dict) -> Optional[dict]:
    hm = _market_from_props(home_a, "btts")
    am = _market_from_props(away_a, "btts")
    if not hm or not am:
        return None
    # Combined estimate: average of each team's BTTS-yes rate in their own matches.
    h_games = int(hm.get("games") or 0)
    a_games = int(am.get("games") or 0)
    if h_games < 5 or a_games < 5:
        return None
    yes = (float(hm.get("yes_rate") or 0) + float(am.get("yes_rate") or 0)) / 2.0
    games = min(h_games, a_games)
    side = "Sí" if yes >= 55 else "No"
    hit = yes if side == "Sí" else (100.0 - yes)
    if _score(hit, games) <= 0:
        return None
    hits = int(round(hit / 100.0 * games))
    return {
        "sport": "football",
        "market": "btts",
        "market_label": "Ambos anotan",
        "selection": side,
        "line": None,
        "hit_rate": round(hit, 1),
        "hits": hits,
        "games": games,
        "sample_size": games,
        "confidence": _confidence(hit, games, "SI" if side == "Sí" else "NO"),
        "risk": _risk(hit, games),
        "entity": f"{fixture.get('home')} / {fixture.get('away')}",
        "entity_id": fixture.get("fixture_id"),
        "match": f"{fixture.get('home')} vs {fixture.get('away')}",
        "kickoff": _kickoff_label(fixture.get("kickoff") or fixture.get("date")),
        "league": fixture.get("league"),
        "league_id": fixture.get("league_id"),
        "fixture_id": fixture.get("fixture_id"),
        "logo": fixture.get("home_logo"),
        "score": _score(hit, games),
        "note": "Promedio de la tasa BTTS de ambos equipos en sus partidos recientes.",
    }


_team_hist_lock = threading.Lock()
_team_hist: dict = {}


def _analyze_football_team(team_id: int, league_id: int, venue: Optional[str] = None) -> Optional[dict]:
    """VIP-only 15-match window, cached per team/day to protect API quota."""
    key = (int(team_id), int(league_id), venue or "all", LAST_N, _today())
    with _team_hist_lock:
        hit = _team_hist.get(key)
        if hit is not None:
            return copy.deepcopy(hit)
    try:
        data = fs.analyze_team_props(
            team_id=int(team_id),
            league=int(league_id),
            last_n=LAST_N,
            venue=venue or "all",
        )
    except Exception:
        return None
    with _team_hist_lock:
        _team_hist[key] = data
    # Fill the dashboard default (last 10) from already-cached fixture stats.
    if LAST_N != 10:
        try:
            fs.analyze_team_props(
                team_id=int(team_id),
                league=int(league_id),
                last_n=10,
                venue="all",
            )
        except Exception:
            pass
    return copy.deepcopy(data) if data else None


PREFERRED_TEAM_MARKETS = (
    "corners_total",
    "corners_for",
    "btts",
    "goals_for",
    "goals_against",
    "shots",
    "shots_on_target",
    "corners_against",
    "cards",
    "yellow",
)



MIN_ODD = 1.40
MIN_BOOKS = 3
MIN_BOOKS_PAIR = 2
MIN_HIT = 60
MIN_GAMES = 5
MIN_EDGE = 0.0
MAX_EDGE = 25.0
OUTLIER_DEV = 0.25
PAIR_AGREE = 0.10
SHRINK_K = 8

# API-Football /odds bet IDs — strict whitelist (full-time, no Asian/handicap/halves).
ODDS_MATCH_WINNER = 1
ODDS_GOALS_OU = 5
ODDS_BTTS = 8
ODDS_GOALS_HOME = 16
ODDS_GOALS_AWAY = 17
ODDS_CORNERS_OU = 45
ODDS_CORNERS_HOME = 57
ODDS_CORNERS_AWAY = 58
ODDS_CARDS_OU = 80
ODDS_CARDS_HOME = 82
ODDS_CARDS_AWAY = 83

# Accepted names (case-insensitive) for each whitelisted bet id.
ALLOWED_BETS: dict[int, set[str]] = {
    ODDS_MATCH_WINNER: {"match winner"},
    ODDS_GOALS_OU: {"goals over/under"},
    ODDS_BTTS: {"both teams score"},
    ODDS_GOALS_HOME: {"total - home"},
    ODDS_GOALS_AWAY: {"total - away"},
    ODDS_CORNERS_OU: {"corners over under", "corners over/under"},
    ODDS_CORNERS_HOME: {"home corners over/under"},
    ODDS_CORNERS_AWAY: {"away corners over/under"},
    ODDS_CARDS_OU: {"cards over/under"},
    ODDS_CARDS_HOME: {"home team total cards"},
    ODDS_CARDS_AWAY: {"away team total cards"},
}
BET_CANONICAL_NAME = {
    ODDS_MATCH_WINNER: "Match Winner",
    ODDS_GOALS_OU: "Goals Over/Under",
    ODDS_BTTS: "Both Teams Score",
    ODDS_GOALS_HOME: "Total - Home",
    ODDS_GOALS_AWAY: "Total - Away",
    ODDS_CORNERS_OU: "Corners Over Under",
    ODDS_CORNERS_HOME: "Home Corners Over/Under",
    ODDS_CORNERS_AWAY: "Away Corners Over/Under",
    ODDS_CARDS_OU: "Cards Over/Under",
    ODDS_CARDS_HOME: "Home Team Total Cards",
    ODDS_CARDS_AWAY: "Away Team Total Cards",
}
OU_BET_IDS = {
    ODDS_GOALS_OU, ODDS_GOALS_HOME, ODDS_GOALS_AWAY,
    ODDS_CORNERS_OU, ODDS_CORNERS_HOME, ODDS_CORNERS_AWAY,
    ODDS_CARDS_OU, ODDS_CARDS_HOME, ODDS_CARDS_AWAY,
}
EXCLUDED_BET_NAME = re.compile(
    r"half|1st|2nd|second|first|asian|handicap|exact|race|odd/?even|"
    r"1x2|3[ -]?way|double chance|ht/?ft|range|between|multi|european",
    re.I,
)
OU_VALUE_RE = re.compile(r"^(Over|Under)\s+([0-9]+(?:[.,][0-9]+)?)$", re.I)

# Match-level markets use both teams' recent match totals and realistic floors.
MATCH_MARKETS = {
    "corners_total": {"label": "Córners del partido", "min_line": 7.5, "row_key": "corners_total", "bet_id": ODDS_CORNERS_OU},
    "cards_total": {"label": "Tarjetas del partido", "min_line": 3.5, "row_key": "cards_total", "bet_id": ODDS_CARDS_OU},
    "goals_total": {"label": "Goles del partido", "min_line": 1.5, "row_key": "goals_total", "bet_id": ODDS_GOALS_OU},
}

# Team-specific markets must name the team in the label.
TEAM_MARKETS_VIP = {
    "corners_for": {"label": "córners a favor", "min_line": 3.5, "row_key": "corners_for", "bet_home": ODDS_CORNERS_HOME, "bet_away": ODDS_CORNERS_AWAY},
    "goals_for": {"label": "goles anotados", "min_line": 1.5, "row_key": "goals_for", "bet_home": ODDS_GOALS_HOME, "bet_away": ODDS_GOALS_AWAY},
    "cards": {"label": "tarjetas", "min_line": 1.5, "row_key": "cards", "bet_home": ODDS_CARDS_HOME, "bet_away": ODDS_CARDS_AWAY},
}


def _match_values(analysis: dict, key: str) -> list[float]:
    values = []
    for row in analysis.get("matches") or []:
        if not isinstance(row, dict):
            continue
        val = row.get(key)
        if val is None:
            continue
        try:
            values.append(float(val))
        except (TypeError, ValueError):
            continue
    return values


def _hit_at_line(values: list[float], line: float, side: str) -> tuple[int, int, float]:
    games = len(values)
    if not games:
        return 0, 0, 0.0
    if side == "Over":
        hits = sum(1 for v in values if v > line)
    else:
        # Same convention as summarize_series: Over is strictly above the line.
        hits = sum(1 for v in values if v <= line)
    return hits, games, round(100.0 * hits / games, 1)


def _median_line(values: list[float], min_line: float) -> Optional[float]:
    if len(values) < MIN_GAMES:
        return None
    return max(float(min_line), fs.snap_half(statistics.median(values)))


def _parse_odd(raw) -> Optional[float]:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if val <= 1.0 or val > 100:
        return None
    return val


def _bet_allowed(bet_id, bet_name: str) -> bool:
    try:
        bid = int(bet_id)
    except (TypeError, ValueError):
        return False
    names = ALLOWED_BETS.get(bid)
    if not names:
        return False
    label = (bet_name or "").strip()
    if not label or EXCLUDED_BET_NAME.search(label):
        return False
    return label.casefold() in names


def _parse_odds_value(raw, bet_id: int) -> Optional[tuple[str, Optional[float], str]]:
    """Return (side, line_or_None, canonical_value) only for exact allowed formats."""
    if not raw:
        return None
    text = str(raw).strip()
    if bet_id in OU_BET_IDS:
        m = OU_VALUE_RE.fullmatch(text)
        if not m:
            return None
        side = "Over" if m.group(1).lower() == "over" else "Under"
        try:
            line = float(m.group(2).replace(",", "."))
        except ValueError:
            return None
        # Prefer classic .5 lines later; still index integer lines as fallback.
        canon = f"{side} {m.group(2).replace(',', '.')}"
        return side, line, canon
    low = text.casefold()
    if bet_id == ODDS_BTTS:
        if low in {"yes", "sí", "si"}:
            return "Yes", None, "Yes"
        if low == "no":
            return "No", None, "No"
        return None
    if bet_id == ODDS_MATCH_WINNER:
        if low == "home":
            return "Home", None, "Home"
        if low == "away":
            return "Away", None, "Away"
        if low == "draw":
            return "Draw", None, "Draw"
        return None
    return None


def _consensus_odds(pairs: list[tuple[float, str]]) -> Optional[dict]:
    """Average book odds after dropping outliers. Need 3 books, or 2 that agree within 10%."""
    if len(pairs) < MIN_BOOKS_PAIR:
        return None
    odds = [o for o, _ in pairs]
    med = statistics.median(odds)
    if med <= 0:
        return None
    kept = [(o, b) for o, b in pairs if abs(o - med) / med <= OUTLIER_DEV]
    if len(kept) >= MIN_BOOKS:
        pass
    elif len(kept) == MIN_BOOKS_PAIR:
        a, b_ = kept[0][0], kept[1][0]
        mid = (a + b_) / 2.0
        if mid <= 0 or abs(a - b_) / mid > PAIR_AGREE:
            return None
    else:
        return None
    odds_list = [o for o, _ in kept]
    return {
        "avg": round(sum(odds_list) / len(odds_list), 2),
        "min": round(min(odds_list), 2),
        "max": round(max(odds_list), 2),
        "n": len(odds_list),
        "books": sorted({b for _, b in kept}),
    }


def get_fixture_odds(fixture_id) -> dict:
    """Index only whitelisted full-time markets. Cached with the football API cache."""
    if not fixture_id:
        return {}
    try:
        data = fm.api_get("/odds", {"fixture": int(fixture_id)}, ttl=SLATE_TTL)
    except Exception:
        return {}
    item = (data.get("response") or [None])[0] or {}
    # {bet_id: {(side, line_or_None): [(odd, book), ...]}}
    collected: dict = {}
    names_seen: dict = {}
    values_seen: dict = {}
    for book in item.get("bookmakers") or []:
        bname = book.get("name") or "book"
        for bet in book.get("bets") or []:
            bid = bet.get("id")
            bname_mkt = bet.get("name") or ""
            if not _bet_allowed(bid, bname_mkt):
                continue
            bid = int(bid)
            names_seen[bid] = bname_mkt
            bucket = collected.setdefault(bid, {})
            used_books = bucket.setdefault("_used", {})
            for entry in bet.get("values") or []:
                parsed = _parse_odds_value(entry.get("value"), bid)
                if not parsed:
                    continue
                odd = _parse_odd(entry.get("odd"))
                if odd is None:
                    continue
                side, line, canon = parsed
                key = (side, line)
                values_seen[(bid, key)] = canon
                slot_books = used_books.setdefault(key, set())
                if bname in slot_books:
                    continue
                slot_books.add(bname)
                bucket.setdefault(key, []).append((odd, bname))
    summary = {}
    for bid, lines in collected.items():
        out = {}
        for key, pairs in lines.items():
            if key == "_used":
                continue
            info = _consensus_odds(pairs)
            if not info:
                continue
            info["bet_id"] = bid
            info["bet_name"] = BET_CANONICAL_NAME.get(bid) or names_seen.get(bid)
            info["odds_value"] = values_seen.get((bid, key))
            info["verified"] = True
            out[key] = info
        if out:
            summary[bid] = out
    return summary


def _odds_lookup(odds_index: dict, bet_id: Optional[int], side: str, line=None) -> Optional[dict]:
    if not odds_index or not bet_id:
        return None
    table = odds_index.get(int(bet_id)) or {}
    if line is None:
        return table.get((side, None))
    target = float(line)
    # Exact line first, then very close float noise.
    for (s, ln), info in table.items():
        if s == side and ln is not None and abs(ln - target) < 0.05:
            return info
    return None


def _available_lines(odds_index: dict, bet_id: Optional[int], min_line: float) -> list[float]:
    if not odds_index or not bet_id:
        return []
    table = odds_index.get(int(bet_id)) or {}
    by_line: dict[float, int] = {}
    for (side, ln), info in table.items():
        if ln is None or ln + 1e-9 < float(min_line):
            continue
        by_line[ln] = max(by_line.get(ln, 0), int(info.get("n") or 0))
    if not by_line:
        return []
    # If a liquid main line exists (3+ books), ignore thin alternative lines.
    if any(n >= MIN_BOOKS for n in by_line.values()):
        by_line = {ln: n for ln, n in by_line.items() if n >= MIN_BOOKS}
    lines = sorted(by_line)
    half = [ln for ln in lines if abs(ln - int(ln) - 0.5) < 0.01]
    return half or lines


def _shrunk_hit(hits: float, games: int, implied_pct: float, k: int = SHRINK_K) -> float:
    """Blend the sample hit rate toward the market implied probability."""
    if games <= 0:
        return implied_pct
    phat = 100.0 * float(hits) / float(games)
    return (games * phat + k * implied_pct) / (games + k)


def _attach_odds(cand: dict, info: Optional[dict]) -> Optional[dict]:
    if not info or not info.get("verified"):
        return None
    if info.get("n", 0) < MIN_BOOKS_PAIR or info.get("avg", 0) < MIN_ODD:
        return None
    avg = float(info["avg"])
    implied = 100.0 / avg
    hit = float(cand["hit_rate"])
    games = int(cand.get("games") or 0)
    hits = float(cand.get("hits") or 0)
    raw_edge = round(hit - implied, 1)
    adj_hit = _shrunk_hit(hits, games, implied)
    edge = round(adj_hit - implied, 1)
    if edge < MIN_EDGE:
        return None
    # Conservative edge still above 25% is almost certainly a mismatch or a tiny sample.
    if edge > MAX_EDGE:
        return None
    cand["avg_odd"] = avg
    cand["odd_min"] = info["min"]
    cand["odd_max"] = info["max"]
    cand["bookmakers"] = info["n"]
    cand["bookmaker_names"] = info.get("books") or []
    cand["implied_prob"] = round(implied, 1)
    cand["edge"] = edge
    cand["edge_raw"] = raw_edge
    cand["hit_rate_adj"] = round(adj_hit, 1)
    cand["edge_flag"] = raw_edge > MAX_EDGE
    cand["odds_verified"] = True
    cand["bet_id"] = info.get("bet_id")
    cand["bet_name"] = info.get("bet_name")
    cand["odds_value"] = info.get("odds_value")
    cand["score"] = edge * 2.0 + adj_hit * 0.4 + min(games, 12)
    cand["risk"] = _risk(hit, games)
    cand["confidence"] = _confidence(hit, games, cand.get("selection") or "")
    return cand


def _base_pick(fx: dict, **extra) -> dict:
    pick = {
        "sport": "football",
        "match": f"{fx.get('home')} vs {fx.get('away')}",
        "kickoff": _kickoff_label(fx.get("kickoff") or fx.get("date")),
        "league": fx.get("league"),
        "league_id": fx.get("league_id"),
        "fixture_id": fx.get("fixture_id"),
    }
    pick.update(extra)
    return pick


def _build_ou_candidate(fx, values, line, side, market, label, entity, entity_id, entity_side, logo, note, odds_info):
    hits, games, hit = _hit_at_line(values, line, side)
    if games < MIN_GAMES or hit < MIN_HIT:
        return None
    cand = _base_pick(
        fx,
        market=market,
        market_label=label,
        selection=side,
        line=line,
        hit_rate=hit,
        hits=hits,
        games=games,
        sample_size=games,
        entity=entity,
        entity_id=entity_id,
        entity_side=entity_side,
        logo=logo,
        note=note,
        avg=round(sum(values) / len(values), 2) if values else None,
        sample_values=list(values),
    )
    return _attach_odds(cand, odds_info)


def _best_ou_from_values(fx, values, min_line, bet_id, odds_index, market, label, entity, entity_id, entity_side, logo, note):
    if len(values) < MIN_GAMES or not bet_id:
        return None
    target = _median_line(values, min_line)
    if target is None:
        return None
    offered = _available_lines(odds_index, bet_id, min_line)
    if not offered:
        return None
    # Use only the offered .5 line closest to the historical median.
    # If neither side qualifies, try the next-closest line — do not shop easier lines for fat odds.
    ranked_lines = sorted(offered, key=lambda ln: (abs(ln - target), ln))
    for line in ranked_lines[:2]:
        options = []
        for side in ("Over", "Under"):
            info = _odds_lookup(odds_index, bet_id, side, line)
            cand = _build_ou_candidate(
                fx, values, line, side, market, label, entity, entity_id, entity_side, logo, note, info,
            )
            if cand:
                options.append(cand)
        if options:
            return max(options, key=lambda c: c.get("score") or 0)
    return None


def _picks_for_football_fixture(fx: dict) -> list[dict]:
    fid = fx.get("fixture_id")
    fx_key = f"fx|{_today()}|{fid}" if fid else None
    if fx_key:
        with _fixture_picks_lock:
            item = _fixture_picks_cache.get(fx_key)
            if item and time.time() - item.get("ts", 0) < SLATE_TTL:
                return copy.deepcopy(item.get("data") or [])
    result = _picks_for_football_fixture_compute(fx)
    if fx_key:
        with _fixture_picks_lock:
            _fixture_picks_cache[fx_key] = {"ts": time.time(), "data": copy.deepcopy(result)}
    return result


def _picks_for_football_fixture_compute(fx: dict) -> list[dict]:
    home_id, away_id = fx.get("home_id"), fx.get("away_id")
    league_id = fx.get("league_id")
    if not home_id or not away_id or not league_id:
        return []
    home_a = _analyze_football_team(home_id, league_id)
    away_a = _analyze_football_team(away_id, league_id)
    if not home_a or not away_a:
        return []
    odds_index = get_fixture_odds(fx.get("fixture_id"))
    if not odds_index:
        return []

    candidates = []
    home_name = fx.get("home")
    away_name = fx.get("away")

    # Match-level markets: pool both teams' recent match totals, exclude missing stats.
    for key, spec in MATCH_MARKETS.items():
        values = _match_values(home_a, spec["row_key"]) + _match_values(away_a, spec["row_key"])
        note = (
            f"Muestra combinada de los partidos recientes de {home_name} y {away_name}; "
            "se omiten juegos sin estadística."
        )
        cand = _best_ou_from_values(
            fx, values, spec["min_line"], spec["bet_id"], odds_index,
            key, spec["label"],
            f"{home_name} / {away_name}", fx.get("fixture_id"), "match",
            fx.get("home_logo"), note,
        )
        if cand:
            candidates.append(cand)

    # Team-specific markets, clearly named.
    for analysis, side, team_name, team_id, logo in (
        (home_a, "home", home_name, home_id, fx.get("home_logo")),
        (away_a, "away", away_name, away_id, fx.get("away_logo")),
    ):
        bet_key = "bet_home" if side == "home" else "bet_away"
        for key, spec in TEAM_MARKETS_VIP.items():
            values = _match_values(analysis, spec["row_key"])
            label = f"{team_name} {spec['label']}"
            cand = _best_ou_from_values(
                fx, values, spec["min_line"], spec.get(bet_key), odds_index,
                key, label, team_name, team_id, side, logo,
                "Mercado del equipo, no del partido. Solo partidos con estadística reportada.",
            )
            if cand:
                candidates.append(cand)

    # BTTS (match)
    hm = _market_from_props(home_a, "btts")
    am = _market_from_props(away_a, "btts")
    if hm and am and int(hm.get("games") or 0) >= MIN_GAMES and int(am.get("games") or 0) >= MIN_GAMES:
        yes = (float(hm.get("yes_rate") or 0) + float(am.get("yes_rate") or 0)) / 2.0
        games = min(int(hm["games"]), int(am["games"]))
        for side, hit, odds_side in (("Sí", yes, "Yes"), ("No", 100.0 - yes, "No")):
            if hit < MIN_HIT:
                continue
            hits = int(round(hit / 100.0 * games))
            cand = _base_pick(
                fx,
                market="btts",
                market_label="Ambos anotan",
                selection=side,
                line=None,
                hit_rate=round(hit, 1),
                hits=hits,
                games=games,
                sample_size=games,
                entity=f"{home_name} / {away_name}",
                entity_id=fx.get("fixture_id"),
                logo=fx.get("home_logo"),
                note="Promedio de la tasa BTTS de ambos equipos en sus partidos recientes.",
            )
            attached = _attach_odds(cand, _odds_lookup(odds_index, ODDS_BTTS, odds_side, None))
            if attached:
                candidates.append(attached)

    # 1X2 using recent win rate vs bookmaker odds
    for analysis, side, team_name, team_id, logo, odds_side in (
        (home_a, "home", home_name, home_id, fx.get("home_logo"), "Home"),
        (away_a, "away", away_name, away_id, fx.get("away_logo"), "Away"),
    ):
        rate, wins, games = _form_win_rate(analysis)
        if rate is None or games < MIN_GAMES or rate < MIN_HIT:
            continue
        cand = _base_pick(
            fx,
            market="1x2",
            market_label="1X2 / ganador",
            selection=team_name,
            line=None,
            hit_rate=rate,
            hits=wins,
            games=games,
            sample_size=games,
            entity=team_name,
            entity_id=team_id,
            entity_side=side,
            logo=logo,
            note="Hit rate = % de victorias del equipo en su forma reciente (mismos partidos que usa HBA).",
        )
        attached = _attach_odds(cand, _odds_lookup(odds_index, ODDS_MATCH_WINNER, odds_side, None))
        if attached:
            candidates.append(attached)

    return candidates


def _iter_rows(payload):
    if payload is None:
        return []
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for key in ("data", "matches", "games", "response", "results"):
        val = payload.get(key)
        if isinstance(val, list):
            return val
    return []



def _nba_games_on_bogota_days(dates_bogota: list[str]) -> list[dict]:
    """Collect NBA games whose tipoff falls on the given Bogotá dates."""
    query_dates = set(dates_bogota)
    for d in dates_bogota:
        try:
            query_dates.add((datetime.fromisoformat(d).date() + timedelta(days=1)).isoformat())
        except ValueError:
            continue
    found: list[dict] = []
    seen = set()
    for q in sorted(query_dates):
        for game in _nba_games_on_date(q):
            key = game.get("game_id") or (game.get("home"), game.get("away"), game.get("kickoff"))
            if key in seen:
                continue
            seen.add(key)
            found.append(game)
    for d in dates_bogota:
        found = _fill_nba_kickoffs(found, d)
    want = set(dates_bogota)
    keep = []
    for game in found:
        dt = _parse_kickoff(game.get("kickoff"))
        if dt and dt.date().isoformat() in want:
            keep.append(game)
    return keep


def _nba_games_on_date(date_str: str) -> list[dict]:
    if not os.getenv("BIGBALLS_API_KEY"):
        return []
    try:
        data = nba.api_get(
            "/matches",
            {
                "sport": "basketball",
                "league": "nba",
                "date": date_str,
                "tz": "America/Bogota",
                "limit": 20,
            },
            ttl=SLATE_TTL,
        )
    except Exception:
        return []
    games = []
    for row in _iter_rows(data):
        home = row.get("home_team") or row.get("home") or {}
        away = row.get("away_team") or row.get("away") or {}
        if not isinstance(home, dict):
            home = {"name": home}
        if not isinstance(away, dict):
            away = {"name": away}
        home_name = home.get("name") or home.get("full_name") or row.get("home_name")
        away_name = away.get("name") or away.get("full_name") or row.get("away_name")
        if not home_name or not away_name:
            continue
        status = (row.get("status") or row.get("state") or "").lower()
        if status in {"final", "cancelled", "canceled", "postponed", "ft"}:
            continue
        games.append({
            "sport": "nba",
            "game_id": row.get("id") or row.get("game_id"),
            "home": home_name,
            "away": away_name,
            "home_id": home.get("id") or row.get("home_id") or row.get("home_team_id"),
            "away_id": away.get("id") or row.get("away_id") or row.get("away_team_id"),
            "kickoff": _nba_raw_kickoff(row),
            "league": "NBA",
            "status": row.get("status") or row.get("state"),
        })
    return _fill_nba_kickoffs(games, date_str)


def _nba_raw_kickoff(row: dict):
    for key in (
        "kickoff_utc", "start_time_utc", "tipoff_utc", "commence_time",
        "scheduled_at", "datetime_utc", "starts_at", "startDate", "start_at",
        "start_time", "tipoff", "tip_off", "datetime", "date_time", "game_time",
        "kickoff", "date",
    ):
        val = row.get(key)
        if val in (None, ""):
            continue
        # Date-only is not a tip time; keep looking.
        if isinstance(val, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", val.strip()):
            continue
        if _parse_kickoff(val):
            return val
        if val:
            return val
    return None


def _nba_team_recent(team_id) -> list[dict]:
    if not team_id:
        return []
    try:
        data = nba.api_get(
            f"/teams/{team_id}/matches",
            {"sport": "basketball", "league": "nba", "limit": 12},
            ttl=nba.CACHE_TTL_LIVE,
        )
    except Exception:
        return []
    return _iter_rows(data)


def _nba_score_pair(row: dict, team_id) -> Optional[tuple[int, int, bool]]:
    home = row.get("home_team") or row.get("home") or {}
    away = row.get("away_team") or row.get("away") or {}
    home_id = (home.get("id") if isinstance(home, dict) else None) or row.get("home_id")
    away_id = (away.get("id") if isinstance(away, dict) else None) or row.get("away_id")
    hs = row.get("home_score") if row.get("home_score") is not None else row.get("home_pts")
    aws = row.get("away_score") if row.get("away_score") is not None else row.get("away_pts")
    result = row.get("result") or {}
    if hs is None and isinstance(result, dict):
        hs = result.get("home")
        aws = result.get("away")
    try:
        hs_i, aws_i = int(hs), int(aws)
    except (TypeError, ValueError):
        return None
    won = None
    if team_id is not None and home_id is not None and str(home_id) == str(team_id):
        won = hs_i > aws_i
    elif team_id is not None and away_id is not None and str(away_id) == str(team_id):
        won = aws_i > hs_i
    return hs_i, aws_i, won


def _nba_team_candidates(team_name, team_id, fixture: dict) -> list[dict]:
    rows = _nba_team_recent(team_id)
    totals = []
    wins = 0
    decided = 0
    for row in rows:
        pair = _nba_score_pair(row, team_id)
        if not pair:
            continue
        hs, aws, won = pair
        totals.append(hs + aws)
        if won is not None:
            decided += 1
            if won:
                wins += 1
    out = []
    if decided >= 5:
        rate = round(100.0 * wins / decided, 1)
        if _score(rate, decided) > 0:
            out.append({
                "sport": "nba",
                "market": "moneyline",
                "market_label": "Ganador (moneyline)",
                "selection": team_name,
                "line": None,
                "hit_rate": rate,
                "hits": wins,
                "games": decided,
                "sample_size": decided,
                "confidence": _confidence(rate, decided, "OVER"),
                "risk": _risk(rate, decided),
                "entity": team_name,
                "entity_id": team_id,
                "match": f"{fixture.get('home')} vs {fixture.get('away')}",
                "kickoff": _kickoff_label(fixture.get("kickoff")),
                "league": "NBA",
                "score": _score(rate, decided),
                "note": "Hit rate = % de victorias del equipo en sus partidos recientes.",
            })
    if len(totals) >= 5:
        avg = sum(totals) / len(totals)
        line = 224.5
        overs = sum(1 for t in totals if t > line)
        hit = round(100.0 * overs / len(totals), 1)
        lean = "OVER" if hit >= 60 else ("UNDER" if hit <= 40 else "NEUTRAL")
        if lean == "UNDER":
            hit = round(100.0 - hit, 1)
            hits = len(totals) - overs
            selection = "Under"
        elif lean == "OVER":
            hits = overs
            selection = "Over"
        else:
            return out
        if _score(hit, len(totals)) > 0:
            out.append({
                "sport": "nba",
                "market": "totals",
                "market_label": "Total puntos del partido",
                "selection": selection,
                "line": line,
                "hit_rate": hit,
                "hits": hits,
                "games": len(totals),
                "sample_size": len(totals),
                "confidence": _confidence(hit, len(totals), lean),
                "risk": _risk(hit, len(totals)),
                "entity": team_name,
                "entity_id": team_id,
                "match": f"{fixture.get('home')} vs {fixture.get('away')}",
                "kickoff": _kickoff_label(fixture.get("kickoff")),
                "league": "NBA",
                "score": _score(hit, len(totals)),
                "avg": round(avg, 1),
            })
    return out


def _nba_prop_sort_key(cand: dict) -> tuple:
    score = float(cand.get("score") or 0)
    # A 0.5 threes under without a sourced book line is a thin, often juiced lean.
    if cand.get("market") == "FG3M" and (cand.get("line") or 0) <= 0.5 and cand.get("book_line") is None:
        score -= 20
    return (-score, -(cand.get("hit_rate") or 0), -(cand.get("games") or 0))


def _rank_nba_props(candidates: list[dict], limit: int = 5, keep_score: bool = False) -> list[dict]:
    seen_player = set()
    per_game: dict = {}
    ranked = []
    for cand in sorted(candidates, key=_nba_prop_sort_key):
        pid = cand.get("entity_id") or cand.get("entity")
        if pid in seen_player:
            continue
        gid = cand.get("fixture_id") or cand.get("match")
        if per_game.get(gid, 0) >= nprops.MAX_PROPS_PER_GAME:
            continue
        seen_player.add(pid)
        per_game[gid] = per_game.get(gid, 0) + 1
        ranked.append(cand)
        if len(ranked) >= limit:
            break
    for i, pick in enumerate(ranked, start=1):
        pick["rank"] = i
        if not keep_score:
            pick.pop("score", None)
    return ranked


def _rank_top(candidates: list[dict], limit: int = 5) -> list[dict]:
    # Avoid two nearly identical markets from the same match when possible.
    seen = set()
    ranked = []
    for cand in sorted(candidates, key=lambda c: (-(c.get("score") or 0), -(c.get("games") or 0))):
        # Top 5 football value picks must have a verified bookmaker market and a capped edge.
        if cand.get("sport") == "football":
            if not cand.get("odds_verified"):
                continue
            if cand.get("edge") is None or cand.get("edge") > MAX_EDGE:
                continue
        key = (cand.get("fixture_id") or cand.get("match"), cand.get("market"), cand.get("entity"))
        if key in seen:
            continue
        seen.add(key)
        ranked.append(cand)
        if len(ranked) >= limit:
            break
    for i, pick in enumerate(ranked, start=1):
        pick["rank"] = i
        pick.pop("score", None)
    return ranked



def _sample_recent_hits(pick: dict) -> bool:
    """The most recent match in the sample must have hit the line."""
    values = pick.get("sample_values") or pick.get("recent_values") or []
    line = pick.get("line")
    side = pick.get("selection")
    if not values or line is None or side not in {"Over", "Under"}:
        return False
    try:
        v = float(values[0])
        line = float(line)
    except (TypeError, ValueError, IndexError):
        return False
    return v <= line if side == "Under" else v > line


def _is_maxima_confianza(pick: dict) -> bool:
    """Strict badge for real-money use. Never awarded to 1X2 form or unsourced NBA estimates."""
    if (pick.get("sport") or "football") != "football":
        return False
    market = (pick.get("market") or "").lower()
    bet_name = (pick.get("bet_name") or "").lower()
    if market in {"1x2", "moneyline"} or bet_name in {"match winner"} or pick.get("bet_id") == 1:
        return False
    if not pick.get("odds_verified"):
        return False
    try:
        hit = float(pick.get("hit_rate") or 0)
        games = int(pick.get("games") or 0)
        books = int(pick.get("bookmakers") or 0)
        odd = float(pick.get("avg_odd") or 0)
        edge = float(pick.get("edge") or 0)
    except (TypeError, ValueError):
        return False
    if hit < 80 or games < 12 or books < 3 or odd < 1.40 or edge <= 0:
        return False
    if not _sample_recent_hits(pick):
        return False
    return True


def _apply_maxima_confianza(picks: list[dict]) -> list[dict]:
    for pick in picks:
        pick["maxima_confianza"] = _is_maxima_confianza(pick)
        pick.pop("sample_values", None)
        pick.pop("recent_values", None)
    return picks


def _nba_team_nick(name: str) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower()).strip()
    special = {
        "trail blazers": "blazers",
        "trailblazers": "blazers",
        "76ers": "76ers",
        "sixers": "76ers",
        "timberwolves": "timberwolves",
    }
    for key, nick in special.items():
        if key in s:
            return nick
    parts = s.split()
    return parts[-1] if parts else s


def _espn_nba_tipoffs(date_str: str) -> list[dict]:
    """Public ESPN scoreboard times for a Bogota calendar date. Fail soft."""
    try:
        ymd = date_str.replace("-", "")
        url = (
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/"
            f"scoreboard?dates={ymd}"
        )
        req = urllib.request.Request(url, headers={"User-Agent": "HBA-VIP-picks/1.0"})
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return []
    out = []
    for ev in data.get("events") or []:
        comps = ev.get("competitions") or []
        if not comps:
            continue
        comp = comps[0]
        status = ((comp.get("status") or {}).get("type") or {}).get("description") or ""
        if str(status).lower() in {"final", "postponed", "canceled", "cancelled"}:
            continue
        home = away = None
        for team in comp.get("competitors") or []:
            nm = (team.get("team") or {}).get("displayName")
            if team.get("homeAway") == "home":
                home = nm
            else:
                away = nm
        kickoff = comp.get("date") or ev.get("date")
        if home and away and kickoff:
            out.append({"home": home, "away": away, "kickoff": kickoff, "status": status})
    return out


def _fill_nba_kickoffs(games: list[dict], date_str: str) -> list[dict]:
    missing = [g for g in games if not _parse_kickoff(g.get("kickoff"))]
    if not missing:
        return games
    board = _espn_nba_tipoffs(date_str)
    if not board:
        return games
    used = set()
    for game in games:
        if _parse_kickoff(game.get("kickoff")):
            continue
        g_home = _nba_team_nick(game.get("home") or "")
        g_away = _nba_team_nick(game.get("away") or "")
        for i, ev in enumerate(board):
            if i in used:
                continue
            if _nba_team_nick(ev["home"]) == g_home and _nba_team_nick(ev["away"]) == g_away:
                game["kickoff"] = ev["kickoff"]
                if ev.get("status") and not game.get("status"):
                    game["status"] = ev["status"]
                used.add(i)
                break
    return games


def _public_fixtures(fixtures: list[dict], sport: str) -> list[dict]:
    out = []
    for fx in fixtures:
        out.append({
            "sport": sport,
            "match": f"{fx.get('home')} vs {fx.get('away')}",
            "home": fx.get("home"),
            "away": fx.get("away"),
            "kickoff": _kickoff_label(fx.get("kickoff") or fx.get("date")),
            "league": fx.get("league"),
            "status": fx.get("status"),
        })
    return out


def _build_football(league_id: Optional[int]) -> dict:
    today = _today()
    window = "today"
    today_ids = None if league_id else ALL_LEAGUE_IDS
    fixtures = _upcoming_only(_collect_football_fixtures(league_id, [today], ids=today_ids))
    if not fixtures:
        upcoming_days = [
            (datetime.now(BOGOTA).date() + timedelta(days=i)).isoformat()
            for i in range(1, 4)
        ]
        fixtures = _upcoming_only(_collect_football_fixtures(league_id, upcoming_days))
        if fixtures:
            window = "upcoming"
    fixtures = _trim_fixtures(fixtures)
    candidates = []
    for fx in fixtures:
        candidates.extend(_picks_for_football_fixture(fx))
    picks = _rank_top(candidates, 5)
    ai = bool(os.getenv("XAI_API_KEY"))
    if picks:
        picks = _explain_with_grok(picks)
    picks = _apply_maxima_confianza(picks)
    return {
        "date": today,
        "window": window,
        "timezone": "America/Bogota",
        "sport": "football",
        "league": league_id,
        "fixtures": _public_fixtures(fixtures, "football"),
        "picks": picks,
        "ai_explained": bool(ai and picks),
        "empty_reason": None if fixtures else "No hay partidos programados hoy ni mañana en las competiciones seleccionadas.",
        "disclaimer": "+18. Apuesta bajo tu propia responsabilidad.",
    }


def _build_nba() -> dict:
    today = _today()
    window = "today"
    if not os.getenv("BIGBALLS_API_KEY"):
        return {
            "date": today,
            "window": window,
            "timezone": "America/Bogota",
            "sport": "nba",
            "fixtures": [],
            "picks": [],
            "ai_explained": False,
            "empty_reason": "NBA no disponible en este entorno.",
            "disclaimer": "+18. Apuesta bajo tu propia responsabilidad.",
        }
    # BigBalls (and many NBA feeds) date games in UTC, so a 19:00 Bogotá
    # tipoff lives on the next UTC date. Query today+tomorrow and keep the
    # games whose kickoff falls on the requested Bogotá calendar day(s).
    games = _nba_games_on_bogota_days([today])
    if not games:
        upcoming_days = [
            (datetime.now(BOGOTA).date() + timedelta(days=i)).isoformat()
            for i in range(1, 3)
        ]
        games = _nba_games_on_bogota_days(upcoming_days)
        if games:
            window = "upcoming"
    games = _trim_fixtures(games, NBA_MAX_FIXTURES)
    # Player props are the NBA daily product. Team totals remain a fallback only.
    candidates = nprops.collect_nba_prop_candidates(games)
    pool = _rank_nba_props(candidates, 12, keep_score=True)
    grok_lines = {"attempted": False, "ok": False, "reason": "no_picks"}
    if pool:
        pool, grok_lines = nprops.enrich_props_with_grok_lines(pool)
        pool = [p for p in pool if not p.get("book_conflict")]
    picks = _rank_nba_props(pool, 5)
    for pick in picks:
        pick["kickoff"] = _kickoff_label(pick.get("kickoff"))
    if len(picks) < 5:
        fallback = []
        for game in games:
            fallback.extend(_nba_team_candidates(game.get("home"), game.get("home_id"), game))
            fallback.extend(_nba_team_candidates(game.get("away"), game.get("away_id"), game))
        for extra in _rank_top(fallback, 5):
            if len(picks) >= 5:
                break
            if any((p.get("market"), p.get("entity")) == (extra.get("market"), extra.get("entity")) for p in picks):
                continue
            picks.append(extra)
        for i, pick in enumerate(picks, start=1):
            pick["rank"] = i
            pick.pop("score", None)
    ai = bool(os.getenv("XAI_API_KEY"))
    if picks:
        picks = _explain_with_grok(picks)
    picks = _apply_maxima_confianza(picks)
    empty = None
    if not games:
        empty = "No hay partidos NBA programados hoy ni mañana."
    elif not picks:
        empty = "No hay props NBA con muestra, minutos y línea estimada suficientes para esta pizarra."
    return {
        "date": today,
        "window": window,
        "timezone": "America/Bogota",
        "sport": "nba",
        "fixtures": _public_fixtures(games, "nba"),
        "picks": picks,
        "ai_explained": bool(ai and picks),
        "empty_reason": empty,
        "disclaimer": "+18. Apuesta bajo tu propia responsabilidad.",
    }


def _assemble_all(football: dict, nba_part: dict, league_id: Optional[int] = None) -> dict:
    today = _today()
    picks = nprops._mix_all_picks(football.get("picks") or [], nba_part.get("picks") or [], 5)
    for i, pick in enumerate(picks, start=1):
        pick["rank"] = i
        pick.pop("score", None)
    return {
        "date": today,
        "window": football.get("window") if football.get("fixtures") else nba_part.get("window"),
        "timezone": "America/Bogota",
        "sport": "all",
        "league": league_id,
        "fixtures": (football.get("fixtures") or []) + (nba_part.get("fixtures") or []),
        "picks": picks,
        "ai_explained": bool((football.get("ai_explained") or nba_part.get("ai_explained")) and picks),
        "empty_reason": None if (football.get("fixtures") or nba_part.get("fixtures")) else "No hay partidos programados hoy ni mañana.",
        "parts": {
            "football_empty": football.get("empty_reason"),
            "nba_empty": nba_part.get("empty_reason"),
        },
        "disclaimer": football.get("disclaimer") or nba_part.get("disclaimer") or "+18. Apuesta bajo tu propia responsabilidad.",
    }


def _cached_slate(sport: str, league_id: Optional[int], allow_stale: bool = True):
    key = _cache_key(sport, league_id, _today(), "auto")
    return key, _cache_lookup(key, allow_stale=allow_stale)


def _compute_slate(sport: str, league_id: Optional[int], reuse: bool = True) -> dict:
    today = _today()
    key = _cache_key(sport, league_id, today, "auto")
    ev = None
    wait_for = None
    with _cache_lock:
        found = _cache_lookup(key, allow_stale=False)
        if found:
            out = copy.deepcopy(found["data"])
            out["cached"] = True
            return out
        if key in _inflight:
            wait_for = _inflight[key]
        else:
            ev = threading.Event()
            _inflight[key] = ev
    if wait_for is not None:
        wait_for.wait(timeout=240)
        found = _cache_lookup(key, allow_stale=True)
        if found:
            out = copy.deepcopy(found["data"])
            out["cached"] = True
            return out
        raise RuntimeError("No se pudieron calcular los picks.")
    try:
        if sport == "nba":
            result = _build_nba()
        elif sport == "football":
            result = _build_football(league_id)
        else:
            football = None
            nba_part = None
            if reuse:
                _, fb_hit = _cached_slate("football", league_id, allow_stale=True)
                _, nba_hit = _cached_slate("nba", None, allow_stale=True)
                if fb_hit:
                    football = copy.deepcopy(fb_hit["data"])
                if nba_hit:
                    nba_part = copy.deepcopy(nba_hit["data"])
            if football is None:
                football = _compute_slate("football", league_id, reuse=reuse)
            if nba_part is None:
                nba_part = _compute_slate("nba", None, reuse=reuse)
            result = _assemble_all(football, nba_part, league_id)
        result["cached"] = False
        result["generated_at"] = datetime.now(BOGOTA).strftime("%Y-%m-%d %H:%M")
        _cache_set(key, result)
        return result
    finally:
        ev.set()
        with _cache_lock:
            _inflight.pop(key, None)


def _schedule_refresh(sport: str, league_id: Optional[int], key: str):
    if not fm.background_fetches_allowed():
        return
    with _cache_lock:
        if key in _refreshing or key in _inflight:
            return
        _refreshing.add(key)

    def _run():
        try:
            _compute_slate(sport, league_id, reuse=True)
        except Exception:
            pass
        finally:
            with _cache_lock:
                _refreshing.discard(key)

    threading.Thread(target=_run, name=f"vip-refresh-{key[-24:]}", daemon=True).start()


def _history_rows_today() -> list[dict]:
    try:
        from vip_history import fetch_rows
        today = _today()
        rows = fetch_rows(limit=500, only_pending=False)
        return [r for r in rows if str(r.get("pick_date") or "")[:10] == today]
    except Exception:
        return []


def _history_has_today() -> bool:
    return bool(_history_rows_today())


def _slate_from_history(sport: str, league_id: Optional[int]) -> Optional[dict]:
    """Serve today's already-published picks when quota is too tight to recompute."""
    rows = _history_rows_today()
    if not rows:
        return None
    if sport in {"football", "nba"}:
        rows = [r for r in rows if (r.get("sport") or "").lower() == sport]
    if league_id:
        try:
            lid = int(league_id)
        except (TypeError, ValueError):
            lid = None
        if lid is not None:
            rows = [r for r in rows if r.get("league_id") == lid]
    if not rows:
        return None
    picks = []
    fixtures = []
    seen_fx = set()
    ranked = sorted(
        rows,
        key=lambda r: (
            0 if r.get("maxima_confianza") else 1,
            -(int(r.get("confidence") or 0)),
        ),
    )
    limit = 5
    for r in ranked[:limit]:
        raw = r.get("raw") or {}
        if isinstance(raw, str):
            try:
                raw = json.loads(raw)
            except Exception:
                raw = {}
        if not isinstance(raw, dict):
            raw = {}
        picks.append({
            "sport": r.get("sport"),
            "market": r.get("market"),
            "market_label": r.get("market_label"),
            "selection": r.get("selection"),
            "line": r.get("line"),
            "avg_odd": r.get("odd"),
            "confidence": r.get("confidence"),
            "maxima_confianza": bool(r.get("maxima_confianza")),
            "entity": r.get("entity"),
            "entity_id": r.get("entity_id"),
            "entity_side": r.get("entity_side"),
            "match": r.get("match"),
            "kickoff": r.get("kickoff"),
            "league": r.get("league"),
            "league_id": r.get("league_id"),
            "fixture_id": r.get("fixture_id"),
            "reason": raw.get("reason"),
            "hit_rate": raw.get("hit_rate"),
            "games": raw.get("games"),
            "odds_verified": raw.get("odds_verified"),
            "odds_value": raw.get("odds_value"),
        })
        match = r.get("match") or ""
        if match and match not in seen_fx:
            seen_fx.add(match)
            parts = match.split(" vs ", 1)
            fixtures.append({
                "sport": r.get("sport"),
                "match": match,
                "home": parts[0] if parts else match,
                "away": parts[1] if len(parts) > 1 else "",
                "kickoff": r.get("kickoff"),
                "league": r.get("league"),
            })
    return {
        "date": _today(),
        "window": "today",
        "timezone": "America/Bogota",
        "sport": sport,
        "league": league_id,
        "fixtures": fixtures,
        "picks": picks,
        "ai_explained": False,
        "empty_reason": None,
        "disclaimer": "+18. Apuesta bajo tu propia responsabilidad.",
        "cached": True,
        "from_history": True,
        "generated_at": datetime.now(BOGOTA).strftime("%Y-%m-%d %H:%M"),
    }


def get_daily_picks(sport: str = "all", league_id: Optional[int] = None) -> dict:
    sport = (sport or "all").lower()
    if sport in {"football", "futbol", "fútbol"}:
        sport = "football"
    if sport not in {"all", "nba", "football"}:
        sport = "all"
    today = _today()
    key = _cache_key(sport, league_id, today, "auto")
    found = _cache_lookup(key, allow_stale=True)
    remaining = fm.quota_remaining()
    if found:
        out = copy.deepcopy(found["data"])
        out["cached"] = True
        # Do not background-refresh slates when the remaining budget is tight.
        if found.get("stale") and (remaining is None or remaining >= 2000):
            _schedule_refresh(sport, league_id, key)
        return out
    # After a deploy the in-memory cache is empty. A single VIP picks
    # request used to recompute the full 15-game slate (~200 calls).
    # When remaining is under 2000, serve today's published history instead.
    if remaining is not None and remaining < 2000:
        hist = _slate_from_history(sport, league_id)
        if hist:
            return hist
        if remaining < fm.QUOTA_CACHE_ONLY:
            raise RuntimeError("Cuota diaria casi agotada; se sirve solo la caché.")
        if remaining < fm.QUOTA_SKIP_BACKGROUND:
            raise RuntimeError("Cuota diaria baja; no se puede recalcular la pizarra.")
    return _compute_slate(sport, league_id, reuse=True)


def _warm_targets():
    targets = [("nba", None), ("football", None)]
    for lid in ALL_LEAGUE_IDS:
        targets.append(("football", lid))
    targets.append(("all", None))
    return targets


LAST_WARMUP = {"seconds": None, "finished_at": None, "started_at": None, "ok": 0, "skipped": 0, "errors": 0, "running": False}


def get_warmup_status() -> dict:
    return dict(LAST_WARMUP)


def warm_picks_cache(force: bool = False) -> dict:
    """Precompute NBA, each football league, then Todos. Reuses fixture/slate cache."""
    started = time.time()
    LAST_WARMUP.update({
        "started_at": datetime.now(BOGOTA).strftime("%Y-%m-%d %H:%M"),
        "running": True,
    })
    report = {"ok": [], "skipped": [], "errors": []}
    fm.set_request_mode("background")
    try:
        remaining = fm.quota_remaining(refresh=True)
        tight = remaining is not None and remaining < 2000
        if remaining is not None and remaining < fm.QUOTA_SKIP_BACKGROUND:
            report["skipped"].append(f"quota:{remaining}")
            print(f"HBA-QUOTA skip warmup remaining={remaining}", flush=True)
            return report
        # A full 12-league 15-game re-warm would blow a tight remaining budget,
        # especially after a deploy wipes /tmp caches. If today's slate already
        # exists locally or in history, skip the rebuild.
        if tight:
            force = False
            if _has_today_slate() or _history_has_today():
                report["skipped"].append(f"tight-quota:{remaining}")
                print(f"HBA-QUOTA skip full warmup, serve cache/history remaining={remaining}", flush=True)
                return report

        fm.begin_call_cycle("warmup", cap=fm.CYCLE_CALL_CAP)

        def _one(sport: str, league_id: Optional[int]):
            if not fm.cycle_budget_left() or not fm.background_fetches_allowed():
                return "skipped", f"{sport}:{league_id or 'all'}:quota"
            key = _cache_key(sport, league_id, _today(), "auto")
            found = _cache_lookup(key, allow_stale=False)
            if found and not force:
                return "skipped", f"{sport}:{league_id or 'all'}"
            _compute_slate(sport, league_id, reuse=True)
            return "ok", f"{sport}:{league_id or 'all'}"

        # Core slates only first: NBA + combined football + Todos.
        with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
            futs = {pool.submit(_one, s, lid): (s, lid) for s, lid in (("nba", None), ("football", None))}
            for fut in as_completed(futs):
                try:
                    kind, label = fut.result()
                    report[kind].append(label)
                except Exception as exc:
                    s, lid = futs[fut]
                    report["errors"].append(f"{s}:{lid or 'all'}:{type(exc).__name__}")
        try:
            kind, label = _one("all", None)
            report[kind].append(label)
        except Exception as exc:
            report["errors"].append(f"all:{type(exc).__name__}")
        remaining_now = fm.quota_remaining()
        # Per-league slates are optional and were the long-running quota burn.
        skip_leagues = (
            tight
            or not fm.cycle_budget_left()
            or (remaining_now is not None and remaining_now < 2000)
        )
        if not skip_leagues:
            rest = [("football", lid) for lid in ALL_LEAGUE_IDS]
            with ThreadPoolExecutor(max_workers=WARM_WORKERS) as pool:
                futs = {pool.submit(_one, s, lid): (s, lid) for s, lid in rest}
                for fut in as_completed(futs):
                    try:
                        kind, label = fut.result()
                        report[kind].append(label)
                    except Exception as exc:
                        s, lid = futs[fut]
                        report["errors"].append(f"{s}:{lid or 'all'}:{type(exc).__name__}")
        else:
            report["skipped"].append("per-league:quota")
    except Exception as exc:
        report["errors"].append(type(exc).__name__)
    finally:
        fm.end_call_cycle()
        LAST_WARMUP.update({
            "seconds": round(time.time() - started, 1),
            "finished_at": datetime.now(BOGOTA).strftime("%Y-%m-%d %H:%M"),
            "ok": len(report["ok"]),
            "skipped": len(report["skipped"]),
            "errors": len(report["errors"]),
            "running": False,
        })
    return report



def _has_today_slate() -> bool:
    """True when a same-day VIP slate is already in memory or the cache file."""
    today = _today()
    prefix = f"v7|{today}|"
    now = time.time()
    with _cache_lock:
        items = list(_memory_cache.items())
    for key, item in items:
        if not str(key).startswith(prefix):
            continue
        found = _cache_fresh(item, now, allow_stale=True)
        if found and isinstance(found.get("data"), dict) and found["data"].get("date") == today:
            return True
    try:
        if CACHE_FILE.exists():
            store = json.loads(CACHE_FILE.read_text()) or {}
            for key, item in store.items():
                if not str(key).startswith(prefix):
                    continue
                found = _cache_fresh(item, now, allow_stale=True)
                if found and isinstance(found.get("data"), dict) and found["data"].get("date") == today:
                    return True
    except Exception:
        pass
    return False


def _grade_on_wake():
    """Grade leftover picks as soon as the process wakes, without waiting for warmup."""
    try:
        if not fm.background_fetches_allowed():
            return None
        from vip_history import grade_pending
        grade_pending()
        return (datetime.now(BOGOTA).date() - timedelta(days=1)).isoformat()
    except Exception:
        return None


def start_picks_background():
    """Warm the slates on boot and refresh them on the 45-minute odds cadence."""
    global _bg_started
    with _bg_lock:
        if _bg_started:
            return
        _bg_started = True

    def _seconds_until_clock(hour: int, minute: int) -> float:
        now = datetime.now(BOGOTA)
        target = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target <= now:
            target += timedelta(days=1)
        return (target - now).total_seconds()

    def _loop():
        first = True
        last_forced_date = None
        last_morning_date = None
        # Grade yesterday immediately on wake so we don't wait for a long
        # 15-game warmup if the instance slept through 00:05.
        threading.Thread(target=_grade_on_wake, name="vip-grade-wake", daemon=True).start()
        while True:
            now = datetime.now(BOGOTA)
            today = _today()
            missing_today = not _has_today_slate()
            new_day = (last_forced_date is not None and last_forced_date != today) or missing_today
            morning = (
                last_morning_date != today
                and (now.hour, now.minute) >= (7, 0)
                and now.hour < 10
            )
            # Refresh odds on the 45-minute cadence during the day only.
            # Overnight we keep the process awake, but only force a new-day
            # slate at 00:05 and a fresh-odds pass at 07:00, to stay inside
            # the API-Football Pro daily cap.
            # If the process slept through midnight, missing_today forces an
            # immediate compute on any wake.
            daytime = 7 <= now.hour <= 23
            remaining = fm.quota_remaining()
            allow_bg = remaining is None or remaining >= fm.QUOTA_SKIP_BACKGROUND
            # Daytime 45-minute refreshes stay on when quota is healthy, but
            # never force a full rebuild if remaining is under 2000.
            force = allow_bg and ((not first and daytime) or new_day or morning or missing_today)
            if remaining is not None and remaining < 2000:
                force = False
            try:
                if allow_bg:
                    warm_picks_cache(force=force)
                else:
                    print(f"HBA-QUOTA skip background loop remaining={remaining}", flush=True)
                if new_day or missing_today or last_forced_date is None:
                    last_forced_date = _today()
                if morning:
                    last_morning_date = _today()
            except Exception:
                pass
            try:
                if allow_bg:
                    from vip_history import grade_pending
                    grade_pending()
                    if datetime.now(BOGOTA).hour >= 22 and fm.background_fetches_allowed():
                        grade_pending()
            except Exception:
                pass
            first = False
            now = datetime.now(BOGOTA)
            if last_forced_date == _today() and now.hour < 7 and _has_today_slate():
                delay = _seconds_until_clock(7, 0)
            else:
                delay = min(SLATE_TTL, _seconds_until_clock(0, 5), _seconds_until_clock(7, 0))
            time.sleep(max(15, delay))

    threading.Thread(target=_loop, name="vip-picks-warm", daemon=True).start()
