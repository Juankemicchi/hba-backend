"""Daily VIP picks built from the same HBA stats code used by the dashboard.

Grok (when XAI_API_KEY is set) only writes short reasons from already
computed numbers. It never invents hit rates or sample sizes.
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import football_module as fm
import football_stats as fs
import nba_module as nba
from openai import OpenAI

BOGOTA = ZoneInfo("America/Bogota")
SLATE_TTL = 45 * 60
MAX_FIXTURES = 6
LAST_N = 8
CACHE_FILE = Path(os.getenv("VIP_PICKS_CACHE_FILE", "/tmp/hba_vip_picks_cache.json"))

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
ALL_PRIORITY_IDS = [239, 39, 140, 2, 135, 13]

_cache_lock = threading.Lock()
_memory_cache: dict = {}


def _today() -> str:
    return datetime.now(BOGOTA).date().isoformat()


def _parse_kickoff(iso_str: Optional[str]) -> Optional[datetime]:
    if not iso_str:
        return None
    try:
        raw = iso_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(BOGOTA)
    except Exception:
        return None


def _kickoff_label(iso_str: Optional[str]) -> Optional[str]:
    dt = _parse_kickoff(iso_str)
    return dt.strftime("%Y-%m-%d %H:%M") if dt else iso_str


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
    return f"{date_str}|{sport}|{league_id or 'all'}|{window}"


def _cache_get(key: str):
    now = time.time()
    item = _memory_cache.get(key)
    if item and now - item.get("ts", 0) < SLATE_TTL:
        return item.get("data")
    try:
        if CACHE_FILE.exists():
            store = json.loads(CACHE_FILE.read_text())
            item = store.get(key)
            if item and now - item.get("ts", 0) < SLATE_TTL:
                _memory_cache[key] = item
                return item.get("data")
    except Exception:
        pass
    return None


def _cache_set(key: str, data: dict):
    item = {"ts": time.time(), "data": data}
    _memory_cache[key] = item
    try:
        store = {}
        if CACHE_FILE.exists():
            store = json.loads(CACHE_FILE.read_text()) or {}
        now = time.time()
        store = {k: v for k, v in store.items() if now - v.get("ts", 0) < SLATE_TTL * 2}
        store[key] = item
        CACHE_FILE.write_text(json.dumps(store, ensure_ascii=False))
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
    if hit >= 75 and games >= 8:
        return "baja"
    if hit >= 65:
        return "media"
    return "alta"


def _confidence(hit: float, games: int, lean: str) -> int:
    if games < 5:
        return 2
    if lean in ("NEUTRAL", "") and 40 <= hit <= 60:
        return 2
    if hit >= 75 and games >= 8:
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
    line = pick.get("line")
    line_txt = f" {pick.get('selection') or ''} {line}".strip() if line is not None else (pick.get("selection") or "")
    return (
        f"{pick.get('entity')}: en los últimos {pick.get('games')} partidos, "
        f"{pick.get('market_label')} {line_txt} acertó el {pick.get('hit_rate')}% "
        f"({pick.get('hits')}/{pick.get('games')})."
    )


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
        })
    prompt = (
        "Eres el analista VIP de HBA. Escribe UNA frase corta en español por cada pick, "
        "en el mismo orden. Usa EXCLUSIVAMENTE los números dados (hit rate, hits, games, línea). "
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


def _collect_football_fixtures(league_id: Optional[int], dates_bogota: list[str]) -> list[dict]:
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
    league_ids = [int(league_id)] if league_id else ALL_PRIORITY_IDS
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


def _upcoming_only(fixtures: list[dict]) -> list[dict]:
    keep = []
    for fx in fixtures:
        status = (fx.get("status") or "").upper()
        if status in {"FT", "AET", "PEN"}:
            continue
        keep.append(fx)
    return keep


def _trim_fixtures(fixtures: list[dict]) -> list[dict]:
    def sort_key(fx):
        dt = _parse_kickoff(fx.get("kickoff") or fx.get("date"))
        return dt or datetime.max.replace(tzinfo=BOGOTA)

    fixtures = sorted(fixtures, key=sort_key)
    return fixtures[:MAX_FIXTURES]


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


def _analyze_football_team(team_id: int, league_id: int, venue: Optional[str] = None) -> Optional[dict]:
    try:
        return fs.analyze_team_props(
            team_id=int(team_id),
            league=int(league_id),
            last_n=LAST_N,
            venue=venue or "all",
        )
    except Exception:
        return None


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


def _picks_for_football_fixture(fx: dict) -> list[dict]:
    home_id, away_id = fx.get("home_id"), fx.get("away_id")
    league_id = fx.get("league_id")
    if not home_id or not away_id or not league_id:
        return []
    home_a = _analyze_football_team(home_id, league_id)
    away_a = _analyze_football_team(away_id, league_id)
    if not home_a or not away_a:
        return []
    candidates = []
    for analysis, side in ((home_a, "home"), (away_a, "away")):
        for market in markets_from_analysis(analysis):
            if market.get("key") not in PREFERRED_TEAM_MARKETS:
                continue
            cand = _candidate_from_market(analysis, market, fx, side)
            if cand:
                candidates.append(cand)
    candidates.extend(_moneyline_candidates(home_a, away_a, fx))
    match_btts = _match_btts(home_a, away_a, fx)
    if match_btts:
        # Prefer the combined match BTTS over single-team BTTS for the same fixture.
        candidates = [c for c in candidates if not (c.get("market") == "btts" and c.get("fixture_id") == fx.get("fixture_id"))]
        candidates.append(match_btts)
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
            "kickoff": row.get("start_time") or row.get("date") or row.get("datetime") or row.get("tipoff"),
            "league": "NBA",
        })
    return games


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


def _rank_top(candidates: list[dict], limit: int = 5) -> list[dict]:
    # Avoid two nearly identical markets from the same match when possible.
    seen = set()
    ranked = []
    for cand in sorted(candidates, key=lambda c: (-(c.get("score") or 0), -(c.get("games") or 0))):
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
    fixtures = _upcoming_only(_collect_football_fixtures(league_id, [today]))
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
        "disclaimer": "Análisis estadístico, no es un consejo de apuesta. 18+. Juega con responsabilidad. Las razones describen únicamente las cifras calculadas por HBA.",
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
            "empty_reason": "NBA no disponible: falta BIGBALLS_API_KEY en este entorno.",
            "disclaimer": "Análisis estadístico, no es un consejo de apuesta. 18+. Juega con responsabilidad.",
        }
    games = _nba_games_on_date(today)
    if not games:
        tomorrow = (datetime.now(BOGOTA).date() + timedelta(days=1)).isoformat()
        games = _nba_games_on_date(tomorrow)
        if games:
            window = "upcoming"
    games = _trim_fixtures(games)
    candidates = []
    for game in games:
        # One team-history call per side; totals may duplicate — ranking dedupes by market+entity.
        candidates.extend(_nba_team_candidates(game.get("home"), game.get("home_id"), game))
        candidates.extend(_nba_team_candidates(game.get("away"), game.get("away_id"), game))
    picks = _rank_top(candidates, 5)
    ai = bool(os.getenv("XAI_API_KEY"))
    if picks:
        picks = _explain_with_grok(picks)
    return {
        "date": today,
        "window": window,
        "timezone": "America/Bogota",
        "sport": "nba",
        "fixtures": _public_fixtures(games, "nba"),
        "picks": picks,
        "ai_explained": bool(ai and picks),
        "empty_reason": None if games else "No hay partidos NBA programados hoy ni mañana.",
        "disclaimer": "Análisis estadístico, no es un consejo de apuesta. 18+. Juega con responsabilidad. Las razones describen únicamente las cifras calculadas por HBA.",
    }


def get_daily_picks(sport: str = "all", league_id: Optional[int] = None) -> dict:
    sport = (sport or "all").lower()
    if sport in {"football", "futbol", "fútbol"}:
        sport = "football"
    if sport not in {"all", "nba", "football"}:
        sport = "all"
    today = _today()
    key = _cache_key(sport, league_id, today, "auto")
    with _cache_lock:
        cached = _cache_get(key)
        if cached:
            out = dict(cached)
            out["cached"] = True
            return out

        if sport == "nba":
            result = _build_nba()
        elif sport == "football":
            result = _build_football(league_id)
        else:
            football = _build_football(league_id)
            nba_part = _build_nba()
            merged = (football.get("picks") or []) + (nba_part.get("picks") or [])
            for p in merged:
                p["score"] = _score(float(p.get("hit_rate") or 0), int(p.get("games") or 0))
            picks = _rank_top(merged, 5)
            result = {
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
                "disclaimer": football.get("disclaimer"),
            }
        result["cached"] = False
        result["generated_at"] = datetime.now(BOGOTA).strftime("%Y-%m-%d %H:%M")
        _cache_set(key, result)
        return result
