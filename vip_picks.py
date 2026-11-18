"""Daily VIP picks built from the same HBA stats code used by the dashboard.

Grok (when XAI_API_KEY is set) only writes short reasons from already
computed numbers. It never invents hit rates or sample sizes.
"""

from __future__ import annotations

import json
import re
import statistics
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
    return f"v2|{date_str}|{sport}|{league_id or 'all'}|{window}"


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
    text = (
        f"{pick.get('entity')}: en los últimos {pick.get('games')} partidos, "
        f"{pick.get('market_label')} {line_txt} acertó el {pick.get('hit_rate')}% "
        f"({pick.get('hits')}/{pick.get('games')})."
    )
    if pick.get("avg_odd") is not None:
        text += (
            f" Cuota promedio {pick['avg_odd']} "
            f"({pick.get('odd_min')}–{pick.get('odd_max')}, {pick.get('bookmakers')} casas). "
            f"Edge {pick.get('edge')} p.p. frente a la probabilidad implícita "
            f"({pick.get('implied_prob')}%)."
        )
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
            "implied_prob": pick.get("implied_prob"),
        })
    prompt = (
        "Eres el analista VIP de HBA. Escribe UNA frase corta en español por cada pick, "
        "en el mismo orden. Usa EXCLUSIVAMENTE los números dados (hit rate, hits, games, línea, "
        "cuota promedio, rango, casas y edge). Menciona la cuota y si el edge es positivo. "
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



MIN_ODD = 1.40
MIN_BOOKS = 2
MIN_HIT = 60
MIN_GAMES = 5
MIN_EDGE = 0.0

# API-Football /odds bet IDs
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

# Match-level markets use both teams' recent match totals and realistic floors.
MATCH_MARKETS = {
    "corners_total": {"label": "Córners del partido", "min_line": 7.5, "row_key": "corners_total", "bet_id": ODDS_CORNERS_OU},
    "cards_total": {"label": "Tarjetas del partido", "min_line": 3.5, "row_key": "cards_total", "bet_id": ODDS_CARDS_OU},
    "goals_total": {"label": "Goles del partido", "min_line": 1.5, "row_key": "goals_total", "bet_id": ODDS_GOALS_OU},
}

# Team-specific markets must name the team in the label.
TEAM_MARKETS_VIP = {
    "corners_for": {"label": "córners a favor", "min_line": 3.5, "row_key": "corners_for", "bet_home": ODDS_CORNERS_HOME, "bet_away": ODDS_CORNERS_AWAY},
    "goals_for": {"label": "goles anotados", "min_line": 0.5, "row_key": "goals_for", "bet_home": ODDS_GOALS_HOME, "bet_away": ODDS_GOALS_AWAY},
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


def _parse_ou_label(label: str) -> Optional[tuple[str, float]]:
    if not label:
        return None
    m = re.search(r"(over|under)\s+([0-9]+(?:[.,][0-9]+)?)", str(label), re.I)
    if not m:
        return None
    side = "Over" if m.group(1).lower() == "over" else "Under"
    try:
        line = float(m.group(2).replace(",", "."))
    except ValueError:
        return None
    return side, line


def get_fixture_odds(fixture_id) -> dict:
    """Index bookmaker odds for a fixture. Cached with the football API cache."""
    if not fixture_id:
        return {}
    try:
        data = fm.api_get("/odds", {"fixture": int(fixture_id)}, ttl=SLATE_TTL)
    except Exception:
        return {}
    item = (data.get("response") or [None])[0] or {}
    # {bet_id: {(side, line_or_None): {odds: [..], books: set}}}
    index = {}
    for book in item.get("bookmakers") or []:
        bname = book.get("name") or "book"
        for bet in book.get("bets") or []:
            bid = bet.get("id")
            if bid is None:
                continue
            bucket = index.setdefault(int(bid), {})
            for entry in bet.get("values") or []:
                odd = _parse_odd(entry.get("odd"))
                if odd is None:
                    continue
                raw = str(entry.get("value") or "").strip()
                parsed = _parse_ou_label(raw)
                if parsed:
                    key = parsed
                else:
                    low = raw.lower()
                    if low in {"yes", "sí", "si"}:
                        key = ("Yes", None)
                    elif low in {"no"}:
                        key = ("No", None)
                    elif low in {"home"}:
                        key = ("Home", None)
                    elif low in {"away"}:
                        key = ("Away", None)
                    elif low in {"draw"}:
                        key = ("Draw", None)
                    else:
                        continue
                slot = bucket.setdefault(key, {"odds": [], "books": set()})
                if bname not in slot["books"]:
                    slot["books"].add(bname)
                    slot["odds"].append(odd)
    summary = {}
    for bid, lines in index.items():
        out = {}
        for key, slot in lines.items():
            odds_list = slot["odds"]
            if not odds_list:
                continue
            out[key] = {
                "avg": round(sum(odds_list) / len(odds_list), 2),
                "min": round(min(odds_list), 2),
                "max": round(max(odds_list), 2),
                "n": len(odds_list),
                "books": sorted(slot["books"]),
            }
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
    lines = sorted({ln for (side, ln) in table.keys() if ln is not None and ln + 1e-9 >= float(min_line)})
    # Prefer classic .5 lines
    half = [ln for ln in lines if abs(ln - int(ln) - 0.5) < 0.01]
    return half or lines


def _attach_odds(cand: dict, info: Optional[dict]) -> Optional[dict]:
    if not info or info.get("n", 0) < MIN_BOOKS or info.get("avg", 0) < MIN_ODD:
        return None
    avg = float(info["avg"])
    implied = 100.0 / avg
    hit = float(cand["hit_rate"])
    edge = round(hit - implied, 1)
    if edge < MIN_EDGE:
        return None
    cand["avg_odd"] = avg
    cand["odd_min"] = info["min"]
    cand["odd_max"] = info["max"]
    cand["bookmakers"] = info["n"]
    cand["implied_prob"] = round(implied, 1)
    cand["edge"] = edge
    cand["score"] = edge * 2.0 + hit * 0.4 + min(int(cand.get("games") or 0), 12)
    cand["risk"] = _risk(hit, int(cand.get("games") or 0))
    cand["confidence"] = _confidence(hit, int(cand.get("games") or 0), cand.get("selection") or "")
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
    # Prefer the offered .5 line closest to the median, then try neighbors.
    ranked_lines = sorted(offered, key=lambda ln: (abs(ln - target), ln))
    best = None
    for line in ranked_lines[:6]:
        for side in ("Over", "Under"):
            info = _odds_lookup(odds_index, bet_id, side, line)
            cand = _build_ou_candidate(
                fx, values, line, side, market, label, entity, entity_id, entity_side, logo, note, info,
            )
            if not cand:
                continue
            if best is None or cand["score"] > best["score"]:
                best = cand
        # Strong match on the median-adjacent line is enough; still scan a few.
    return best


def _picks_for_football_fixture(fx: dict) -> list[dict]:
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
            # Top 5 prefers football picks with bookmaker odds/edge.
            # NBA stays available on the NBA tab; it has no odds source here.
            merged = list(football.get("picks") or [])
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
