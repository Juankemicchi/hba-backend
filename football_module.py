from typing import Optional
import os
import json
import logging
import time
import requests
from pathlib import Path
import threading

API_KEY = os.getenv("API_FOOTBALL_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

# Liga BetPlay (Colombia Primera A) como liga por defecto en la UI.
# La temporada se resuelve sola con /leagues?id=&current=true.
DEFAULT_LEAGUE = 239
CACHE_TTL_STABLE = 12 * 60 * 60  # ligas, equipos, plantillas
CACHE_TTL_LIVE = 2 * 60 * 60     # fixtures, standings, lesions, H2H, stats
CACHE_FILE = Path(os.getenv("FOOTBALL_CACHE_FILE", "/tmp/hba_football_cache.json"))

_memory_cache = {}
_cache_io_lock = threading.Lock()

LOGGER = logging.getLogger("hba.quota")

# Skip background warmup / refresh / grading fetches below this remaining count.
QUOTA_SKIP_BACKGROUND = 1000
# Serve cache only, except on-demand user stats, below this remaining count.
QUOTA_CACHE_ONLY = 300
# Hard cap on uncached API-Football calls in one background cycle.
CYCLE_CALL_CAP = 200

_quota_lock = threading.Lock()
_quota_state = {
    "current": None,
    "limit_day": 7500,
    "remaining": None,
    "ts": 0,
    "process_calls": 0,
}
_request_mode = threading.local()
_cycle_lock = threading.Lock()
_active_cycles: dict = {}


def set_request_mode(mode: str):
    """mode is user_stats, user_picks, or background."""
    _request_mode.value = mode or "background"


def get_request_mode() -> str:
    return getattr(_request_mode, "value", None) or "background"


def begin_call_cycle(name: str, cap: int = CYCLE_CALL_CAP):
    """Start a named call budget. Cycles can overlap (warmup + grading); every live
    API call counts against every active cycle, so one cycle can never erase another's cap."""
    with _cycle_lock:
        _active_cycles[name] = {
            "name": name,
            "cap": int(cap),
            "calls": 0,
            "skipped": 0,
            "started": time.time(),
        }


def end_call_cycle(name: Optional[str] = None):
    with _cycle_lock:
        if name is None and _active_cycles:
            # Backward compatible: end the most recently started cycle.
            name = max(_active_cycles.values(), key=lambda c: c["started"])["name"]
        cycle = _active_cycles.pop(name, None) if name else None
    if not cycle:
        return None
    with _quota_lock:
        remaining = _quota_state.get("remaining")
        process_calls = _quota_state.get("process_calls") or 0
    cycle["seconds"] = round(time.time() - cycle["started"], 1)
    cycle["remaining"] = remaining
    cycle["process_calls"] = process_calls
    msg = (
        f"HBA-QUOTA cycle={cycle['name']} calls={cycle['calls']} "
        f"skipped={cycle['skipped']} cap={cycle['cap']} "
        f"remaining={remaining} seconds={cycle['seconds']}"
    )
    print(msg, flush=True)
    try:
        LOGGER.info(msg)
    except Exception:
        pass
    return cycle


def last_cycle_snapshot():
    with _cycle_lock:
        if _active_cycles:
            return {k: dict(v) for k, v in _active_cycles.items()}
    return None


def cycle_budget_left() -> bool:
    with _cycle_lock:
        return all(c["calls"] < c["cap"] for c in _active_cycles.values())


def _note_cycle_skip():
    with _cycle_lock:
        for c in _active_cycles.values():
            c["skipped"] += 1


def _note_cycle_call():
    with _cycle_lock:
        if not _active_cycles:
            return None, None, None
        for c in _active_cycles.values():
            c["calls"] += 1
        tight = min(_active_cycles.values(), key=lambda c: c["cap"] - c["calls"])
        return tight["calls"], tight["cap"], tight["name"]


def _remember_quota(current, limit_day, remaining=None):
    with _quota_lock:
        if current is not None:
            try:
                _quota_state["current"] = int(current)
            except (TypeError, ValueError):
                pass
        if limit_day:
            try:
                _quota_state["limit_day"] = int(limit_day)
            except (TypeError, ValueError):
                pass
        if remaining is None and _quota_state["current"] is not None and _quota_state["limit_day"]:
            remaining = int(_quota_state["limit_day"]) - int(_quota_state["current"])
        if remaining is not None:
            try:
                _quota_state["remaining"] = int(remaining)
            except (TypeError, ValueError):
                pass
        _quota_state["ts"] = time.time()


def _consume_one_call():
    with _quota_lock:
        _quota_state["process_calls"] = int(_quota_state.get("process_calls") or 0) + 1
        if _quota_state.get("remaining") is not None:
            _quota_state["remaining"] = max(0, int(_quota_state["remaining"]) - 1)
        if _quota_state.get("current") is not None:
            _quota_state["current"] = int(_quota_state["current"]) + 1
        return _quota_state.get("remaining"), _quota_state.get("process_calls")


def quota_remaining(refresh: bool = False):
    """Estimated remaining API-Football calls for today."""
    if refresh:
        snap = usage_snapshot()
        if snap and snap.get("remaining") is not None:
            return snap.get("remaining")
    with _quota_lock:
        return _quota_state.get("remaining")


def quota_guard_snapshot():
    with _quota_lock:
        state = {
            "process_calls": _quota_state.get("process_calls") or 0,
            "remaining_est": _quota_state.get("remaining"),
            "current_est": _quota_state.get("current"),
        }
    state["active_cycle"] = last_cycle_snapshot()
    return state


def background_fetches_allowed() -> bool:
    remaining = quota_remaining()
    if remaining is None:
        return True
    return remaining >= QUOTA_SKIP_BACKGROUND



def _cache_key(path, params):
    items = "&".join(f"{k}={params[k]}" for k in sorted(params))
    return f"{path}?{items}"


def _read_file_cache():
    try:
        if CACHE_FILE.exists():
            return json.loads(CACHE_FILE.read_text())
    except Exception:
        pass
    return {}


def _write_file_cache(store):
    try:
        now = time.time()
        store = {k: v for k, v in store.items() if now - v.get("ts", 0) < v.get("ttl", CACHE_TTL_STABLE)}
        CACHE_FILE.write_text(json.dumps(store))
    except Exception:
        pass


def _cache_entry(key):
    with _cache_io_lock:
        entry = _memory_cache.get(key)
        if entry:
            return entry
        store = _read_file_cache()
        entry = store.get(key)
        if entry:
            _memory_cache[key] = entry
        return entry


def _cache_get(key, allow_stale: bool = False):
    now = time.time()
    entry = _cache_entry(key)
    if not entry:
        return None
    if allow_stale or now - entry.get("ts", 0) < entry.get("ttl", CACHE_TTL_STABLE):
        return entry.get("data")
    return None


def peek_cached(path: str, params: dict, allow_stale: bool = False):
    """Return a cached API payload without spending a call, if present."""
    clean_params = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    return _cache_get(_cache_key(path, clean_params), allow_stale=allow_stale)


def _cache_set(key, data, ttl):
    entry = {"ts": time.time(), "data": data, "ttl": ttl}
    with _cache_io_lock:
        _memory_cache[key] = entry
        store = _read_file_cache()
        store[key] = entry
        _write_file_cache(store)


def _raise_api_errors(data):
    errors = data.get("errors")
    if not errors:
        return
    if isinstance(errors, dict):
        if "plan" in errors:
            raise ValueError(
                "El plan de datos no incluye esta temporada o este recurso. "
                "Revisa la temporada o el plan contratado."
            )
        msg = "; ".join(f"{k}: {v}" for k, v in errors.items() if v)
    elif isinstance(errors, list):
        msg = "; ".join(str(e) for e in errors if e)
    else:
        msg = str(errors)
    if msg:
        raise ValueError(msg)


def _empty_payload():
    return {"response": [], "results": 0, "errors": {}}


def _guard_blocks_fetch(path: str) -> bool:
    """Return True when this call must not hit the live API."""
    mode = get_request_mode()
    remaining = quota_remaining()
    # /status does not count against the API-Football daily quota. Never block it,
    # otherwise a process stuck under the background threshold never sees the
    # 19:00 Bogotá reset and keeps blocking itself.
    if path == "/status":
        return False
    if not cycle_budget_left() and mode != "user_stats":
        return True
    if remaining is None:
        return False
    if remaining < QUOTA_CACHE_ONLY and mode != "user_stats":
        return True
    if remaining < QUOTA_SKIP_BACKGROUND and mode == "background":
        return True
    return False


def api_get(path: str, params: dict, ttl: int = CACHE_TTL_STABLE, force: bool = False):
    if not API_KEY:
        raise ValueError("Faltan credenciales de datos en este entorno.")

    clean_params = {k: v for k, v in params.items() if v is not None and v != ""}
    key = _cache_key(path, clean_params)
    if not force:
        cached = _cache_get(key)
        if cached is not None:
            _raise_api_errors(cached)
            return cached

    blocked = _guard_blocks_fetch(path)
    if blocked:
        stale = _cache_get(key, allow_stale=True)
        _note_cycle_skip()
        if stale is not None:
            try:
                _raise_api_errors(stale)
            except ValueError:
                return stale
            return stale
        if get_request_mode() == "user_stats":
            raise ValueError("Se alcanzó el límite de consultas. Intenta más tarde.")
        return _empty_payload()

    response = requests.get(
        f"{BASE_URL}{path}",
        headers=HEADERS,
        params=clean_params,
        timeout=20,
    )
    remaining, process_calls = _consume_one_call()
    cycle_calls, cap, cycle_name = _note_cycle_call()
    if response.status_code == 429:
        raise ValueError("Se alcanzó el límite de consultas. Intenta más tarde.")
    if response.status_code != 200:
        raise ValueError(f"Error al consultar los datos: {response.status_code}")

    data = response.json()
    # Official daily tally from API-Football, when present on any response.
    try:
        req = ((data.get("response") or {}) if isinstance(data.get("response"), dict) else {}) 
        # most endpoints do not include usage; /status is handled separately.
    except Exception:
        req = {}
    _cache_set(key, data, ttl)
    if cycle_name:
        print(
            f"HBA-QUOTA call path={path} cycle={cycle_name} "
            f"n={cycle_calls}/{cap} remaining_est={remaining} process_calls={process_calls}",
            flush=True,
        )
    _raise_api_errors(data)
    return data



def usage_snapshot():
    """Return today's API-Football request count vs the plan daily limit."""
    with _quota_lock:
        known = {
            "current": _quota_state.get("current"),
            "limit_day": _quota_state.get("limit_day"),
            "remaining": _quota_state.get("remaining"),
            "ts": _quota_state.get("ts") or 0,
        }
    remaining = known.get("remaining")
    ttl = 30 * 60 if (remaining is not None and remaining < QUOTA_SKIP_BACKGROUND) else 5 * 60
    # Reuse a recent snapshot so /health and keep-alives do not spend calls.
    stale_ok = remaining is not None and remaining < QUOTA_CACHE_ONLY
    have_recent = known["ts"] and (time.time() - known["ts"] < ttl) and known["remaining"] is not None
    if have_recent or (stale_ok and known["remaining"] is not None):
        return {
            "plan": "Pro",
            "current": known["current"],
            "limit_day": known["limit_day"],
            "remaining": known["remaining"],
            "active": True,
            "cached": True,
        }
    try:
        data = api_get("/status", {}, ttl=ttl)
    except Exception:
        if known["remaining"] is not None:
            return {
                "plan": "Pro",
                "current": known["current"],
                "limit_day": known["limit_day"],
                "remaining": known["remaining"],
                "active": True,
                "cached": True,
            }
        return None
    resp = data.get("response") or {}
    if isinstance(resp, list):
        resp = resp[0] if resp else {}
    sub = resp.get("subscription") or {}
    req = sub.get("requests") or resp.get("requests") or {}
    current = req.get("current")
    limit_day = req.get("limit_day") or req.get("limit")
    remaining = None
    try:
        if current is not None and limit_day:
            remaining = int(limit_day) - int(current)
    except (TypeError, ValueError):
        remaining = None
    _remember_quota(current, limit_day, remaining)
    return {
        "plan": sub.get("plan") or resp.get("plan"),
        "current": current,
        "limit_day": limit_day,
        "remaining": remaining,
        "active": sub.get("active"),
        "cached": False,
    }


def _map_team(item):
    team = item.get("team") or {}
    venue = item.get("venue") or {}
    return {
        "id": team.get("id"),
        "name": team.get("name"),
        "code": team.get("code"),
        "country": team.get("country"),
        "founded": team.get("founded"),
        "logo": team.get("logo"),
        "venue": {
            "name": venue.get("name"),
            "city": venue.get("city"),
            "capacity": venue.get("capacity"),
            "image": venue.get("image"),
        } if venue else {},
    }


def _map_fixture(item):
    fixture = item.get("fixture") or {}
    league = item.get("league") or {}
    teams = item.get("teams") or {}
    goals = item.get("goals") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    status = (fixture.get("status") or {}).get("short")
    return {
        "date": fixture.get("date"),
        "status": status,
        "league": league.get("name"),
        "season": league.get("season"),
        "round": league.get("round"),
        "home": home.get("name"),
        "away": away.get("name"),
        "home_id": home.get("id"),
        "away_id": away.get("id"),
        "home_logo": home.get("logo"),
        "away_logo": away.get("logo"),
        "goals_home": goals.get("home"),
        "goals_away": goals.get("away"),
    }


def get_league_info(league_id: int):
    data = api_get("/leagues", {"id": int(league_id), "current": "true"}, ttl=CACHE_TTL_STABLE)
    item = (data.get("response") or [None])[0]
    if not item:
        data = api_get("/leagues", {"id": int(league_id)}, ttl=CACHE_TTL_STABLE)
        item = (data.get("response") or [None])[0]
    if not item:
        raise ValueError(f"No se encontró la liga {league_id}")

    league = item.get("league") or {}
    country = item.get("country") or {}
    seasons = item.get("seasons") or []
    current = next((s for s in seasons if s.get("current")), seasons[-1] if seasons else {})
    return {
        "id": league.get("id"),
        "name": league.get("name"),
        "type": league.get("type"),
        "logo": league.get("logo"),
        "country": country.get("name"),
        "season": current.get("year"),
    }


def get_current_season(league_id: int):
    info = get_league_info(league_id)
    season = info.get("season")
    if not season:
        raise ValueError(f"No se pudo resolver la temporada actual de la liga {league_id}")
    return int(season)


def resolve_season(league_id: int, season=None):
    if season not in (None, ""):
        return int(season)
    return get_current_season(league_id)


def get_leagues(country: str = None, search: str = None, current: bool = True):
    params = {}
    if country:
        params["country"] = country
    if search:
        params["search"] = search
    elif current:
        params["current"] = "true"
    data = api_get("/leagues", params, ttl=CACHE_TTL_STABLE)
    leagues = []
    for item in data.get("response") or []:
        league = item.get("league") or {}
        country_info = item.get("country") or {}
        seasons = item.get("seasons") or []
        current_season = next((s for s in seasons if s.get("current")), seasons[-1] if seasons else {})
        leagues.append({
            "id": league.get("id"),
            "name": league.get("name"),
            "type": league.get("type"),
            "logo": league.get("logo"),
            "country": country_info.get("name"),
            "country_code": country_info.get("code"),
            "season": current_season.get("year"),
        })
    return {"leagues": leagues}


def get_teams(league: int = DEFAULT_LEAGUE, season=None):
    season = resolve_season(league, season)
    data = api_get("/teams", {"league": int(league), "season": int(season)}, ttl=CACHE_TTL_STABLE)
    teams = [_map_team(item) for item in data.get("response") or []]
    info = None
    try:
        info = get_league_info(league)
    except ValueError:
        info = {"id": int(league), "season": int(season)}
    return {
        "league": int(league),
        "league_name": (info or {}).get("name"),
        "country": (info or {}).get("country"),
        "season": int(season),
        "teams": teams,
    }


def get_players(team: int, season=None, page: int = 1, league: int = None):
    if season in (None, ""):
        if league:
            season = resolve_season(league, None)
        else:
            season = get_current_season(DEFAULT_LEAGUE)
    data = api_get("/players", {
        "team": int(team),
        "season": int(season),
        "page": int(page),
    }, ttl=CACHE_TTL_STABLE)
    players = []
    for item in data.get("response") or []:
        player = item.get("player") or {}
        stats = (item.get("statistics") or [{}])[0]
        games = stats.get("games") or {}
        goals = stats.get("goals") or {}
        players.append({
            "id": player.get("id"),
            "name": player.get("name"),
            "age": player.get("age"),
            "nationality": player.get("nationality"),
            "photo": player.get("photo"),
            "position": games.get("position"),
            "appearances": games.get("appearances"),
            "goals": goals.get("total"),
            "assists": goals.get("assists"),
        })
    paging = data.get("paging") or {"current": int(page), "total": 1}
    return {
        "team": int(team),
        "season": int(season),
        "players": players,
        "paging": paging,
    }


def get_squad(team: int):
    data = api_get("/players/squads", {"team": int(team)}, ttl=CACHE_TTL_STABLE)
    resp = (data.get("response") or [None])[0] or {}
    team_info = resp.get("team") or {}
    return {
        "team": {
            "id": team_info.get("id"),
            "name": team_info.get("name"),
            "logo": team_info.get("logo"),
        },
        "players": resp.get("players") or [],
    }


def get_standings(league: int, season=None):
    season = resolve_season(league, season)
    data = api_get("/standings", {"league": int(league), "season": int(season)}, ttl=CACHE_TTL_LIVE)
    groups = []
    resp = (data.get("response") or [None])[0] or {}
    league_info = resp.get("league") or {}
    for table in league_info.get("standings") or []:
        rows = []
        group_name = None
        for row in table:
            group_name = row.get("group") or group_name
            team = row.get("team") or {}
            all_stats = row.get("all") or {}
            goals = all_stats.get("goals") or {}
            rows.append({
                "rank": row.get("rank"),
                "team_id": team.get("id"),
                "team": team.get("name"),
                "logo": team.get("logo"),
                "points": row.get("points"),
                "played": all_stats.get("played"),
                "win": all_stats.get("win"),
                "draw": all_stats.get("draw"),
                "lose": all_stats.get("lose"),
                "goals_for": goals.get("for"),
                "goals_against": goals.get("against"),
                "goal_diff": row.get("goalsDiff"),
                "form": row.get("form"),
                "description": row.get("description"),
            })
        groups.append({"group": group_name, "table": rows})
    return {
        "league": league_info.get("name") or league,
        "league_id": int(league),
        "season": int(season),
        "groups": groups,
    }


def get_recent_fixtures(team_id: int, last: int = 8):
    data = api_get("/fixtures", {"team": int(team_id), "last": int(last)}, ttl=CACHE_TTL_LIVE)
    return [_map_fixture(item) for item in data.get("response") or []]


def get_injuries(team_id: int, season: int, league: int = None):
    params = {"team": int(team_id), "season": int(season)}
    if league:
        params["league"] = int(league)
    data = api_get("/injuries", params, ttl=CACHE_TTL_LIVE)
    injuries = []
    seen = set()
    for item in data.get("response") or []:
        player = item.get("player") or {}
        name = player.get("name")
        reason = player.get("reason") or player.get("type")
        key = (name, reason)
        if not name or key in seen:
            continue
        seen.add(key)
        injuries.append({
            "name": name,
            "type": player.get("type"),
            "reason": player.get("reason"),
        })
    return injuries


def get_team_statistics(team_id: int, league: int, season: int):
    data = api_get("/teams/statistics", {
        "team": int(team_id),
        "league": int(league),
        "season": int(season),
    }, ttl=CACHE_TTL_LIVE)
    resp = data.get("response") or {}
    fixtures = resp.get("fixtures") or {}
    goals = resp.get("goals") or {}
    form = resp.get("form")
    return {
        "form": form,
        "played": (fixtures.get("played") or {}).get("total"),
        "wins": (fixtures.get("wins") or {}).get("total"),
        "draws": (fixtures.get("draws") or {}).get("total"),
        "loses": (fixtures.get("loses") or {}).get("total"),
        "goals_for": ((goals.get("for") or {}).get("total") or {}).get("total"),
        "goals_against": ((goals.get("against") or {}).get("total") or {}).get("total"),
    }


def _standing_for_team(standings, team_id):
    for group in standings.get("groups") or []:
        for row in group.get("table") or []:
            if row.get("team_id") == team_id:
                result = dict(row)
                result["group"] = group.get("group")
                return result
    return None


def _name_matches(query: str, name: str, code: str = ""):
    q = (query or "").strip().lower()
    n = (name or "").strip().lower()
    c = (code or "").strip().lower()
    if not q or not n:
        return False
    return q in n or n in q or (c and q == c)


def _leagues_for_team(team_id: int):
    data = api_get("/leagues", {"team": int(team_id), "current": "true"}, ttl=CACHE_TTL_STABLE)
    leagues = []
    for item in data.get("response") or []:
        league = item.get("league") or {}
        seasons = item.get("seasons") or []
        current = next((s for s in seasons if s.get("current")), seasons[-1] if seasons else {})
        if league.get("id"):
            leagues.append({
                "id": league.get("id"),
                "name": league.get("name"),
                "type": league.get("type"),
                "season": current.get("year"),
            })
    return leagues


def find_team(team_name: str, league: int = None, season=None):
    if league:
        try:
            for team in get_teams(league, season)["teams"]:
                if _name_matches(team_name, team.get("name"), team.get("code")):
                    return team
        except ValueError:
            pass

    data = api_get("/teams", {"search": team_name}, ttl=CACHE_TTL_STABLE)
    matches = []
    for item in data.get("response") or []:
        team = _map_team(item)
        if team.get("id"):
            matches.append(team)
    for team in matches:
        if _name_matches(team_name, team.get("name"), team.get("code")):
            return team
    return matches[0] if matches else None


def _resolve_team_league(team, league=None, season=None):
    if league:
        info = get_league_info(league)
        if season in (None, ""):
            season = info.get("season")
        return info, int(season) if season else None

    leagues = _leagues_for_team(team["id"]) if team and team.get("id") else []
    # Prefiere ligas nacionales sobre copas si hay varias
    chosen = next((lg for lg in leagues if (lg.get("type") or "").lower() == "league"), None)
    chosen = chosen or (leagues[0] if leagues else None)
    if not chosen:
        info = get_league_info(DEFAULT_LEAGUE)
        return info, info.get("season")
    info = {
        "id": chosen["id"],
        "name": chosen["name"],
        "type": chosen.get("type"),
        "season": chosen.get("season"),
    }
    return info, int(season) if season not in (None, "") else chosen.get("season")


def get_team_context(team_name: str, league: int = None, season=None):
    team = find_team(team_name, league, season)
    if not team or not team.get("id"):
        return None

    info, season = _resolve_team_league(team, league, season)
    league_id = info.get("id")
    standing = None
    stats = None
    injuries = []
    recent = []
    squad = []

    if league_id and season:
        try:
            standings = get_standings(league_id, season)
            standing = _standing_for_team(standings, team["id"])
        except ValueError:
            standings = None
        try:
            stats = get_team_statistics(team["id"], league_id, season)
        except ValueError:
            stats = None
        try:
            injuries = get_injuries(team["id"], season, league_id)
        except ValueError:
            injuries = []
    try:
        recent = get_recent_fixtures(team["id"], last=8)
    except ValueError:
        recent = []
    try:
        squad = (get_squad(team["id"]).get("players") or [])
    except ValueError:
        squad = []

    return {
        "team": team,
        "league": info,
        "season": season,
        "standing": standing,
        "statistics": stats,
        "recent_matches": recent,
        "injuries": injuries,
        "squad": squad,
    }


def get_team_stats(team_name: str, league: int = None, season=None):
    ctx = get_team_context(team_name, league, season)
    if not ctx:
        return None
    return {
        "team": ctx["team"],
        "league": ctx["league"],
        "season": ctx["season"],
        "standing": ctx["standing"],
        "statistics": ctx["statistics"],
        "recent_matches": ctx["recent_matches"],
        "injuries": ctx["injuries"],
        "squad": ctx["squad"],
    }


def get_h2h(team1: str, team2: str, limit: int = 10, league: int = None, season=None):
    t1 = find_team(team1, league, season)
    t2 = find_team(team2, league, season)
    if not t1 or not t2:
        return None

    data = api_get("/fixtures/headtohead", {
        "h2h": f"{t1['id']}-{t2['id']}",
        "last": int(limit),
    }, ttl=CACHE_TTL_LIVE)
    matches = [_map_fixture(item) for item in data.get("response") or []]

    ctx1 = get_team_context(t1["name"], league, season)
    ctx2 = get_team_context(t2["name"], league, season)
    return {
        "matches": matches,
        "team1": ctx1,
        "team2": ctx2,
    }
