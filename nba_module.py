import json
import math
import os
import time
from pathlib import Path

import pandas as pd
import requests

API_KEY = os.getenv("BIGBALLS_API_KEY", "")
BASE_URL = "https://api.bigballsdata.com/v1"
CACHE_FILE = Path(os.getenv("NBA_CACHE_FILE", "/tmp/hba_nba_cache.json"))
CACHE_TTL_STABLE = 12 * 60 * 60
CACHE_TTL_LIVE = 2 * 60 * 60

_memory_cache = {}

NBA_MARKETS = [
    ("PTS", "Puntos", 20.5),
    ("REB", "Rebotes", 6.5),
    ("AST", "Asistencias", 5.5),
    ("PRA", "PRA", 32.5),
    ("PR", "PR", 26.5),
    ("PA", "PA", 25.5),
    ("RA", "RA", 11.5),
    ("FG3M", "Triples", 2.5),
    ("STL", "Robos", 1.5),
    ("BLK", "Tapones", 0.5),
    ("TO", "Pérdidas", 2.5),
    ("MIN", "Minutos", 32.5),
    ("DD", "Double-Double", 0.5),
    ("TD", "Triple-Double", 0.5),
]
NBA_LABELS = {k: label for k, label, _ in NBA_MARKETS}
DEFAULT_LINES = {k: default for k, _label, default in NBA_MARKETS}

CURRENT_NBA_TEAMS = {
    "Atlanta Hawks", "Boston Celtics", "Brooklyn Nets", "Charlotte Hornets",
    "Chicago Bulls", "Cleveland Cavaliers", "Dallas Mavericks", "Denver Nuggets",
    "Detroit Pistons", "Golden State Warriors", "Houston Rockets", "Indiana Pacers",
    "LA Clippers", "Los Angeles Clippers", "Los Angeles Lakers", "Memphis Grizzlies",
    "Miami Heat", "Milwaukee Bucks", "Minnesota Timberwolves", "New Orleans Pelicans",
    "New York Knicks", "Oklahoma City Thunder", "Orlando Magic", "Philadelphia 76ers",
    "Phoenix Suns", "Portland Trail Blazers", "Sacramento Kings", "San Antonio Spurs",
    "Toronto Raptors", "Utah Jazz", "Washington Wizards",
}

POPULAR_TEAM_NAMES = [
    "Los Angeles Lakers",
    "Boston Celtics",
    "Golden State Warriors",
    "New York Knicks",
    "Dallas Mavericks",
    "Miami Heat",
    "Denver Nuggets",
    "Milwaukee Bucks",
    "Oklahoma City Thunder",
    "Minnesota Timberwolves",
    "Philadelphia 76ers",
    "San Antonio Spurs",
]


def _headers():
    return {"Authorization": f"Bearer {os.getenv('BIGBALLS_API_KEY', API_KEY)}"}


def _cache_key(path, params):
    items = "&".join(f"{k}={params[k]}" for k in sorted(params or {}))
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


def _cache_get(key):
    now = time.time()
    item = _memory_cache.get(key)
    if item and now - item.get("ts", 0) < item.get("ttl", 0):
        return item.get("data")
    store = _read_file_cache()
    item = store.get(key)
    if item and now - item.get("ts", 0) < item.get("ttl", 0):
        _memory_cache[key] = item
        return item.get("data")
    return None


def _cache_set(key, data, ttl):
    item = {"ts": time.time(), "ttl": ttl, "data": data}
    _memory_cache[key] = item
    store = _read_file_cache()
    store[key] = item
    _write_file_cache(store)


def api_get(path: str, params=None, ttl: int = CACHE_TTL_STABLE):
    key_val = os.getenv("BIGBALLS_API_KEY", API_KEY)
    if not key_val:
        raise ValueError("Falta la variable de entorno BIGBALLS_API_KEY en Render")
    clean = {k: v for k, v in (params or {}).items() if v is not None and v != ""}
    key = _cache_key(path, clean)
    cached = _cache_get(key)
    if cached is not None:
        return cached
    response = requests.get(
        f"{BASE_URL}{path}",
        headers={"Authorization": f"Bearer {key_val}"},
        params=clean,
        timeout=20,
    )
    if response.status_code == 429:
        raise ValueError("Se alcanzó el límite de consultas de la API NBA. Intenta más tarde.")
    if response.status_code != 200:
        raise ValueError(f"Error en la API NBA: {response.status_code}")
    data = response.json()
    _cache_set(key, data, ttl)
    return data


def _num(value, default=0):
    if value is None or value == "":
        return default
    if isinstance(value, str) and ":" in value:
        try:
            parts = value.split(":")
            return round(int(parts[0]) + int(parts[1]) / 60, 1)
        except (TypeError, ValueError, IndexError):
            return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def snap_half(value, default=0.5):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    snapped = max(0.5, round(number - 0.5) + 0.5)
    return snapped


def _map_player(item):
    if not item or not isinstance(item, dict):
        return None
    team_obj = item.get("team") or item.get("current_team") or {}
    if not isinstance(team_obj, dict):
        team_obj = {}
    native = item.get("nba_id") or item.get("league_player_id") or item.get("person_id") or item.get("external_id")
    photo = item.get("headshot_url") or item.get("photo") or item.get("image_url")
    # Only use the official NBA CDN when the source exposes a numeric NBA person id.
    if not photo and native is not None:
        try:
            person_id = int(str(native).strip())
            if person_id > 0:
                photo = f"https://cdn.nba.com/headshots/nba/latest/1040x760/{person_id}.png"
        except (TypeError, ValueError):
            photo = None
    return {
        "id": item.get("id"),
        "name": item.get("name") or item.get("display_name") or item.get("full_name"),
        "position": item.get("position"),
        "number": item.get("jersey_number") or item.get("shirt_number"),
        "photo": photo,
        "team_id": item.get("team_id") or item.get("current_team_id") or team_obj.get("id"),
        "team_name": item.get("team_name") or team_obj.get("name"),
        "nationality": item.get("nationality"),
        "age": item.get("age"),
    }


def _map_team(item):
    if not item:
        return None
    return {
        "id": item.get("id"),
        "name": item.get("name"),
        "short_name": item.get("short_name"),
        "logo": item.get("logo_url") or item.get("logo"),
        "league": item.get("league"),
        "popular": (item.get("name") or "") in POPULAR_TEAM_NAMES,
    }


def _opponent(game):
    opp = game.get("opponent") or game.get("opp") or game.get("opponent_name")
    if isinstance(opp, dict):
        opp = opp.get("name") or opp.get("short_name") or opp.get("abbr")
    return opp or game.get("abbr") or "Rival"


def _map_game(game):
    date = game.get("date") or game.get("game_date") or game.get("GAME_DATE")
    pts = _num(game.get("pts") if game.get("pts") is not None else game.get("points") or game.get("PTS"))
    reb = _num(game.get("reb") if game.get("reb") is not None else game.get("rebounds") or game.get("REB"))
    ast = _num(game.get("ast") if game.get("ast") is not None else game.get("assists") or game.get("AST"))
    fg3 = _num(game.get("fg3m") if game.get("fg3m") is not None else game.get("three_pointers_made") or game.get("FG3M"))
    mins = _num(game.get("min_played") if game.get("min_played") is not None else game.get("minutes") or game.get("min") or game.get("MIN"))
    stl = _num(game.get("stl") if game.get("stl") is not None else game.get("steals") or game.get("STL"))
    blk = _num(game.get("blk") if game.get("blk") is not None else game.get("blocks") or game.get("BLK"))
    tov = _num(game.get("turnovers") if game.get("turnovers") is not None else game.get("tov") or game.get("TO"))
    row = {
        "GAME_DATE": date,
        "date": date,
        "opponent": _opponent(game),
        "abbr": game.get("abbr"),
        "PTS": pts,
        "REB": reb,
        "AST": ast,
        "FG3M": fg3,
        "MIN": mins,
        "STL": stl,
        "BLK": blk,
        "TO": tov,
        "PRA": pts + reb + ast,
        "PR": pts + reb,
        "PA": pts + ast,
        "RA": reb + ast,
        "DD": int((int(pts >= 10) + int(reb >= 10) + int(ast >= 10)) >= 2),
        "TD": int((int(pts >= 10) + int(reb >= 10) + int(ast >= 10)) >= 3),
        "postseason": game.get("postseason"),
        "is_home": _game_is_home(game),
    }
    return row


def _game_is_home(game):
    for key in ("is_home", "at_home", "home"):
        val = game.get(key)
        if isinstance(val, bool):
            return val
    loc = str(game.get("location") or game.get("ha") or game.get("home_away") or "").strip().lower()
    if loc in {"home", "h"}:
        return True
    if loc in {"away", "a", "road"}:
        return False
    return None


def search_players(query: str, limit: int = 8):
    q = (query or "").strip()
    if len(q) < 2:
        return []
    data = api_get("/players", {"sport": "basketball", "league": "nba", "name": q, "limit": 25}, ttl=CACHE_TTL_STABLE)
    players = [_map_player(item) for item in data.get("data") or [] if item]
    players = [p for p in players if p and p.get("id")]
    ql = q.lower()
    words = [w for w in ql.replace(".", " ").split() if w]

    def rank(item):
        name = (item.get("name") or "").lower()
        nw = name.replace(".", " ").split()
        if name == ql:
            return (0, len(name))
        if words and all(w in nw or w in name for w in words) and nw[:len(words)] == words:
            return (1, len(name))
        if words and all(w in name for w in words):
            return (2, -len(name))
        if any(name.startswith(w) for w in words):
            return (3, len(name))
        if ql in name:
            return (4, len(name))
        return (9, 99)

    players.sort(key=rank)
    return players[:limit]


def search_teams(query: str, limit: int = 8):
    q = (query or "").strip().lower()
    teams = list_nba_teams()
    if len(q) < 2:
        return []
    scored = []
    for team in teams:
        name = (team.get("name") or "").lower()
        short = (team.get("short_name") or "").lower()
        if name == q or short == q:
            score = 0
        elif name.startswith(q) or short.startswith(q):
            score = 1
        elif q in name or q in short:
            score = 2
        else:
            continue
        scored.append((score, len(name), team))
    scored.sort()
    return [t for _s, _n, t in scored[:limit]]


def search_nba(query: str, limit: int = 8):
    return {"teams": search_teams(query, limit), "players": search_players(query, limit)}


def list_nba_teams():
    rows = []
    offset = 0
    while offset < 400:
        data = api_get("/teams", {"sport": "basketball", "league": "nba", "limit": 50, "offset": offset}, ttl=CACHE_TTL_STABLE)
        batch = data.get("data") or []
        if not batch:
            break
        rows.extend(batch)
        total = ((data.get("pagination") or {}).get("total")) or 0
        offset += len(batch)
        if total and offset >= total:
            break
        if len(batch) < 50:
            break
    seen = {}
    for item in rows:
        team = _map_team(item)
        name = (team or {}).get("name") or ""
        if team and team.get("id") and name in CURRENT_NBA_TEAMS and name not in seen:
            seen[name] = team
    teams = list(seen.values())
    teams.sort(key=lambda t: (not t.get("popular"), t.get("name") or ""))
    return teams


def get_nba_roster(team_id: str):
    if not team_id:
        return {"team": None, "players": []}
    team = None
    for item in list_nba_teams():
        if str(item.get("id")) == str(team_id):
            team = item
            break
    if not team:
        try:
            data = api_get(f"/teams/{team_id}", {"sport": "basketball"}, ttl=CACHE_TTL_STABLE)
            raw = data.get("data") or data
            if isinstance(raw, dict) and raw.get("id"):
                team = _map_team(raw)
            elif isinstance(raw, dict) and raw.get("name"):
                team = _map_team(raw)
        except ValueError:
            team = {"id": team_id, "name": None, "logo": None}
    data = api_get("/players", {"sport": "basketball", "teamId": team_id, "limit": 200}, ttl=CACHE_TTL_STABLE)
    players = [_map_player(item) for item in data.get("data") or [] if item]
    players = [p for p in players if p and p.get("id")]
    players.sort(key=lambda p: ((p.get("number") or "99").zfill(3), p.get("name") or ""))
    return {"team": team, "players": players}


def get_player_id(player_name: str):
    found = search_players(player_name, limit=1)
    if not found:
        # Fallback to the original list call in case ranking filtered everyone out.
        data = api_get("/players", {"sport": "basketball", "league": "nba", "name": player_name, "limit": 5}, ttl=CACHE_TTL_STABLE)
        rows = data.get("data") or []
        if not rows:
            return None, None, None
        player = _map_player(rows[0])
        return player["id"], player.get("name", player_name), player
    player = found[0]
    return player["id"], player.get("name", player_name), player


def _fetch_player(player_id: str):
    if not player_id:
        return None
    try:
        data = api_get(f"/players/{player_id}", {"sport": "basketball"}, ttl=CACHE_TTL_STABLE)
    except ValueError:
        return None
    raw = data.get("data") or data
    if isinstance(raw, dict) and (raw.get("id") or raw.get("name") or raw.get("full_name")):
        mapped = _map_player(raw)
        if mapped and mapped.get("id"):
            return mapped
        # Some envelopes nest the player.
        for key in ("player", "players"):
            inner = raw.get(key)
            if isinstance(inner, dict):
                return _map_player(inner)
            if isinstance(inner, list) and inner:
                return _map_player(inner[0] if not isinstance(inner[0], dict) or "name" in inner[0] or "id" in inner[0] else inner[0])
    return None


def get_player_games(player_name: str, last_n: int = 15, player_id: str = None):
    profile = None
    full_name = player_name
    if player_id:
        profile = _fetch_player(player_id)
        full_name = (profile or {}).get("name") or player_name
    if not player_id:
        player_id, full_name, profile = get_player_id(player_name)
    if not player_id:
        raise ValueError(f"Jugador '{player_name}' no encontrado")

    data = api_get(f"/players/{player_id}/gamelog", {"limit": max(int(last_n), 1)}, ttl=CACHE_TTL_LIVE)
    rows_raw = data.get("data") if isinstance(data, dict) else data
    if isinstance(rows_raw, dict):
        rows_raw = rows_raw.get("data") or rows_raw.get("games") or rows_raw.get("log") or []
    rows = [_map_game(game) for game in (rows_raw or [])]
    if not rows:
        return pd.DataFrame(), full_name, profile
    df = pd.DataFrame(rows)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
    df = df.sort_values("GAME_DATE", ascending=False)
    return df.head(int(last_n)), full_name, profile


def _apply_prop(df, prop: str):
    df = df.copy()
    if prop in ("PRA", "PR", "PA", "RA", "DD", "TD"):
        # Computed in _map_game; keep a safety net if older rows are missing them.
        if prop == "PRA":
            df["PRA"] = df["PTS"] + df["REB"] + df["AST"]
        elif prop == "PR":
            df["PR"] = df["PTS"] + df["REB"]
        elif prop == "PA":
            df["PA"] = df["PTS"] + df["AST"]
        elif prop == "RA":
            df["RA"] = df["REB"] + df["AST"]
        elif prop == "DD":
            df["DD"] = ((df["PTS"] >= 10).astype(int) + (df["REB"] >= 10).astype(int) + (df["AST"] >= 10).astype(int) >= 2).astype(int)
        elif prop == "TD":
            df["TD"] = ((df["PTS"] >= 10).astype(int) + (df["REB"] >= 10).astype(int) + (df["AST"] >= 10).astype(int) >= 3).astype(int)
        return df, prop
    if prop not in df.columns:
        raise ValueError(f"Prop '{prop}' no disponible")
    return df, prop


def analyze_prop(player_name: str, prop: str = "PTS", line: float = 20.5, last_n: int = 15, player_id: str = None):
    line = snap_half(line, 20.5)
    df, full_name, _profile = get_player_games(player_name, last_n, player_id=player_id)
    if df.empty:
        raise ValueError(f"No se encontraron partidos para {player_name}")
    df, value_col = _apply_prop(df, prop)
    df["Over"] = df[value_col] > line
    stats = {
        "player": full_name,
        "avg": round(float(df[value_col].mean()), 1),
        "hit_rate": round(float(df["Over"].mean()) * 100, 1),
        "games": int(len(df)),
        "prop": prop,
        "line": line,
        "value_col": value_col,
    }
    return df, stats


def _summarize(rows, key, line):
    line = snap_half(line, DEFAULT_LINES.get(key, 0.5))
    values = []
    for row in rows:
        if key not in row or row[key] is None:
            continue
        val = _num(row[key])
        values.append({
            "date": str(row.get("date") or row.get("GAME_DATE") or "")[:10],
            "opponent": row.get("opponent") or "Rival",
            "value": val,
            "line": line,
            "over": val > line,
        })
    games = len(values)
    if not games:
        return {
            "avg": None, "hit_rate": None, "hits": 0, "games": 0,
            "line": line, "key": key, "label": NBA_LABELS.get(key, key), "values": [],
        }
    hits = sum(1 for item in values if item["over"])
    return {
        "avg": round(sum(item["value"] for item in values) / games, 2),
        "hit_rate": round(hits * 100 / games, 1),
        "hits": hits,
        "games": games,
        "line": line,
        "key": key,
        "label": NBA_LABELS.get(key, key),
        "values": values,
    }


def _recommend(market):
    if not market or not market.get("games"):
        return {"lean": "NEUTRAL", "confidence": 1, "market": None, "line": None, "label": None}
    hit = market["hit_rate"]
    games = market["games"]
    avg = market["avg"]
    line = market["line"]
    if hit >= 65 and avg is not None and avg > line:
        lean = "OVER"
    elif hit <= 35 and avg is not None and avg < line:
        lean = "UNDER"
    else:
        lean = "NEUTRAL"
    if games < 5:
        conf = 2
    elif lean == "NEUTRAL":
        conf = 3 if 45 <= hit <= 55 else 2
    elif hit >= 75 and games >= 8:
        conf = 5
    elif hit >= 65 or hit <= 35:
        conf = 4
    else:
        conf = 3
    return {
        "lean": lean,
        "confidence": conf,
        "market": market.get("key"),
        "label": market.get("label"),
        "line": line,
        "hit_rate": hit,
        "avg": avg,
    }


def analyze_nba_player(player=None, player_id=None, last_n=10, lines=None, market=None):
    last_n = max(3, min(int(last_n or 10), 25))
    df, full_name, profile = get_player_games(player or "", last_n, player_id=player_id)
    if df.empty:
        raise ValueError(f"No se encontraron partidos para {player or player_id}")
    rows = df.to_dict(orient="records")
    for row in rows:
        if hasattr(row.get("GAME_DATE"), "isoformat"):
            row["GAME_DATE"] = row["GAME_DATE"].isoformat()
            row["date"] = str(row.get("date") or row["GAME_DATE"])[:10]
        else:
            row["date"] = str(row.get("date") or row.get("GAME_DATE") or "")[:10]

    merged = dict(DEFAULT_LINES)
    if isinstance(lines, dict):
        for key, value in lines.items():
            if key in merged or key in NBA_LABELS:
                merged[key] = snap_half(value, merged.get(key, 0.5))

    stats = {}
    for key, _label, default in NBA_MARKETS:
        stats[key] = _summarize(rows, key, merged.get(key, default))

    primary = market if market in stats else "PTS"
    if stats.get(primary, {}).get("games") in (0, None):
        for key, _label, _default in NBA_MARKETS:
            if stats[key]["games"]:
                primary = key
                break

    team = None
    if profile and (profile.get("team_id") or profile.get("team_name")):
        team = {"id": profile.get("team_id"), "name": profile.get("team_name"), "logo": None}
        if profile.get("team_id"):
            for item in list_nba_teams():
                if str(item.get("id")) == str(profile.get("team_id")):
                    team = item
                    break

    rec = _recommend(stats.get(primary))
    player_info = profile or {"id": player_id, "name": full_name}
    player_info["name"] = full_name or player_info.get("name")
    return {
        "player": player_info,
        "team": team,
        "last_n": last_n,
        "matches": rows,
        "stats": stats,
        "primary": primary,
        "recommendation": rec,
        "lines": merged,
        "played": len(rows),
    }
