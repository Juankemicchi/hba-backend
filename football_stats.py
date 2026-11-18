import football_module as fm

CACHE_TTL_FIXTURE = 30 * 24 * 60 * 60  # partidos terminados no cambian
FINISHED = {"FT", "AET", "PEN"}

TEAM_MARKETS = [
    ("shots", "Remates", 12.5),
    ("shots_on_target", "Remates a puerta", 4.5),
    ("goals_for", "Goles anotados", 1.5),
    ("goals_against", "Goles recibidos", 1.5),
    ("corners_for", "Córners ganados", 4.5),
    ("corners_against", "Córners recibidos", 4.5),
    ("corners_total", "Córners totales", 9.5),
    ("yellow", "Tarjetas amarillas", 1.5),
    ("red", "Tarjetas rojas", 0.5),
    ("cards", "Tarjetas (total)", 2.5),
    ("btts", "Ambos anotan", 0.5),
    ("saves", "Atajadas", 3.5),
    ("offsides", "Offsides", 1.5),
]

PLAYER_MARKETS = [
    ("shots", "Remates", 2.5),
    ("shots_on_target", "Remates a puerta", 0.5),
    ("goals", "Goles", 0.5),
    ("assists", "Asistencias", 0.5),
    ("fouls_committed", "Faltas hechas", 1.5),
    ("fouls_drawn", "Faltas recibidas", 1.5),
    ("saves", "Atajadas", 2.5),
]

DEFAULT_LINES_TEAM = {k: d for k, _l, d in TEAM_MARKETS}
DEFAULT_LINES_PLAYER = {k: d for k, _l, d in PLAYER_MARKETS}
MARKET_LABELS = {k: l for k, l, _d in TEAM_MARKETS + PLAYER_MARKETS}


def _num(v):
    if v is None or v == "":
        return 0.0
    if isinstance(v, str):
        v = v.replace("%", "").strip()
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def _clamp_last_n(last_n):
    try:
        n = int(last_n or 10)
    except (TypeError, ValueError):
        n = 10
    return max(5, min(15, n))


def summarize_series(rows, key, line):
    values = []
    for row in rows:
        if key not in row or row[key] is None:
            continue
        val = _num(row[key])
        hit = val > line
        values.append({
            "date": row.get("date"),
            "opponent": row.get("opponent"),
            "venue": row.get("venue"),
            "value": val,
            "line": line,
            "over": hit,
            "fixture_id": row.get("fixture_id"),
            "result": row.get("result"),
            "score": row.get("score"),
        })
    games = len(values)
    if not games:
        return {
            "avg": None,
            "hit_rate": None,
            "hits": 0,
            "games": 0,
            "line": line,
            "key": key,
            "label": MARKET_LABELS.get(key, key),
            "values": [],
        }
    hits = sum(1 for v in values if v["over"])
    avg = round(sum(v["value"] for v in values) / games, 2)
    return {
        "avg": avg,
        "hit_rate": round(hits * 100 / games, 1),
        "hits": hits,
        "games": games,
        "line": line,
        "key": key,
        "label": MARKET_LABELS.get(key, key),
        "values": values,
    }


def recommend(market):
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


def _stat_maps(response, team_id):
    mine, opp = {}, {}
    for block in response or []:
        tid = (block.get("team") or {}).get("id")
        types = {s.get("type"): s.get("value") for s in (block.get("statistics") or [])}
        if tid == team_id:
            mine = types
        else:
            opp = types
    return mine, opp


def _pick(types, *names):
    for name in names:
        if name in types and types[name] is not None:
            return _num(types[name])
    # presente pero null
    for name in names:
        if name in types:
            return 0.0
    return None


def search_entities(query: str, limit: int = 8):
    q = (query or "").strip()
    if len(q) < 2:
        return {"teams": [], "players": []}

    teams = []
    try:
        data = fm.api_get("/teams", {"search": q}, ttl=fm.CACHE_TTL_STABLE)
        for item in data.get("response") or []:
            team = fm._map_team(item)
            if team.get("id"):
                teams.append(team)
            if len(teams) >= limit:
                break
    except ValueError:
        teams = []

    players = []
    try:
        data = fm.api_get("/players/profiles", {"search": q}, ttl=fm.CACHE_TTL_STABLE)
        for item in data.get("response") or []:
            p = item.get("player") or item
            if not p.get("id"):
                continue
            players.append({
                "id": p.get("id"),
                "name": p.get("name"),
                "fullname": " ".join(x for x in [p.get("firstname"), p.get("lastname")] if x) or p.get("name"),
                "age": p.get("age"),
                "photo": p.get("photo"),
                "position": p.get("position") or p.get("pos"),
                "nationality": p.get("nationality"),
            })
    except ValueError:
        players = []

    ql = q.lower()
    def _rank(item, *fields):
        texts = [(item.get(f) or "").lower() for f in fields]
        if any(t == ql for t in texts):
            return 0
        if any(t.startswith(ql) for t in texts):
            return 1
        if any(ql in t for t in texts):
            return 2
        return 3
    teams = sorted(teams, key=lambda x: _rank(x, "name", "code"))[:limit]
    players = sorted(players, key=lambda x: _rank(x, "name", "fullname"))[:limit]
    return {"teams": teams, "players": players}


def _finished_fixtures(team_id, last, venue="all", clamp=True):
    want = _clamp_last_n(last) if clamp else max(1, min(40, int(last or 10)))
    fetch_n = want * 3 if venue in ("home", "away") else max(want, 10)
    fetch_n = min(40, fetch_n)
    data = fm.api_get("/fixtures", {"team": int(team_id), "last": int(fetch_n)}, ttl=fm.CACHE_TTL_LIVE)
    rows = []
    for item in data.get("response") or []:
        mapped = fm._map_fixture(item)
        status = mapped.get("status")
        if status not in FINISHED:
            continue
        home_id = mapped.get("home_id")
        away_id = mapped.get("away_id")
        if home_id == team_id:
            loc = "home"
            opponent = mapped.get("away")
            opponent_id = away_id
            opponent_logo = mapped.get("away_logo")
            goals_for = mapped.get("goals_home")
            goals_against = mapped.get("goals_away")
        elif away_id == team_id:
            loc = "away"
            opponent = mapped.get("home")
            opponent_id = home_id
            opponent_logo = mapped.get("home_logo")
            goals_for = mapped.get("goals_away")
            goals_against = mapped.get("goals_home")
        else:
            continue
        if venue in ("home", "away") and loc != venue:
            continue
        gf = _num(goals_for)
        ga = _num(goals_against)
        if gf > ga:
            result = "W"
        elif gf < ga:
            result = "L"
        else:
            result = "D"
        rows.append({
            "fixture_id": (item.get("fixture") or {}).get("id"),
            "date": mapped.get("date"),
            "league": mapped.get("league"),
            "season": mapped.get("season"),
            "round": mapped.get("round"),
            "venue": loc,
            "opponent": opponent,
            "opponent_id": opponent_id,
            "opponent_logo": opponent_logo,
            "goals_for": gf,
            "goals_against": ga,
            "btts": 1 if gf > 0 and ga > 0 else 0,
            "result": result,
            "score": f"{int(gf)}-{int(ga)}",
        })
    # api already returns most recent first typically
    return rows[:want]


def _enrich_team_row(row, team_id):
    fid = row.get("fixture_id")
    if not fid:
        return row
    try:
        data = fm.api_get("/fixtures/statistics", {"fixture": int(fid)}, ttl=CACHE_TTL_FIXTURE)
    except ValueError:
        return row
    mine, opp = _stat_maps(data.get("response") or [], team_id)
    if not mine and not opp:
        return row
    shots = _pick(mine, "Total Shots")
    sot = _pick(mine, "Shots on Goal")
    corners_for = _pick(mine, "Corner Kicks")
    corners_against = _pick(opp, "Corner Kicks")
    yellow = _pick(mine, "Yellow Cards")
    red = _pick(mine, "Red Cards")
    row.update({
        "shots": shots,
        "shots_on_target": sot,
        "corners_for": corners_for,
        "corners_against": corners_against,
        "corners_total": (None if corners_for is None or corners_against is None else corners_for + corners_against),
        "yellow": yellow,
        "red": red,
        "cards": (None if yellow is None and red is None else _num(yellow) + _num(red)),
        "offsides": _pick(mine, "Offsides"),
        "saves": _pick(mine, "Goalkeeper Saves"),
        "has_stats": bool(mine),
    })
    return row


def analyze_team_props(team=None, team_id=None, league=None, last_n=10, venue="all",
                       opponent=None, opponent_id=None, lines=None, season=None, market=None):
    venue = (venue or "all").lower()
    if venue not in ("all", "home", "away"):
        venue = "all"
    last_n = _clamp_last_n(last_n)

    found = None
    if team_id:
        found = {"id": int(team_id), "name": team}
        # completar logo/nombre desde un search ligero si hay nombre
        if team:
            t = fm.find_team(team, league, season)
            if t and t.get("id") == int(team_id):
                found = t
        if not found.get("name") or not found.get("logo"):
            try:
                data = fm.api_get("/teams", {"id": int(team_id)}, ttl=fm.CACHE_TTL_STABLE)
                item = (data.get("response") or [None])[0]
                if item:
                    found = fm._map_team(item)
            except ValueError:
                pass
    elif team:
        found = fm.find_team(team, league, season)
    if not found or not found.get("id"):
        return None

    team_id = found["id"]
    info, season = fm._resolve_team_league(found, league, season)
    rows = _finished_fixtures(team_id, last_n, venue)
    for row in rows:
        _enrich_team_row(row, team_id)

    merged_lines = dict(DEFAULT_LINES_TEAM)
    if isinstance(lines, dict):
        for k, v in lines.items():
            try:
                merged_lines[k] = float(v)
            except (TypeError, ValueError):
                pass

    stats = {}
    for key, _label, default in TEAM_MARKETS:
        stats[key] = summarize_series(rows, key, merged_lines.get(key, default))

    primary = market if market in stats else "corners_total"
    if stats.get(primary, {}).get("games") in (0, None):
        for key, _l, _d in TEAM_MARKETS:
            if stats[key]["games"]:
                primary = key
                break
    rec = recommend(stats.get(primary))

    form = "".join(r.get("result") or "" for r in rows)
    wins = sum(1 for r in rows if r.get("result") == "W")
    draws = sum(1 for r in rows if r.get("result") == "D")
    losses = sum(1 for r in rows if r.get("result") == "L")

    h2h = None
    if opponent or opponent_id:
        try:
            h2h = fm.get_h2h(found.get("name") or team or str(team_id),
                             opponent or str(opponent_id), 8, league, season)
        except ValueError:
            h2h = None

    return {
        "team": found,
        "league": info,
        "season": season,
        "last_n": last_n,
        "venue": venue,
        "matches": rows,
        "stats": stats,
        "form": form,
        "record": {"wins": wins, "draws": draws, "losses": losses, "played": len(rows)},
        "primary": primary,
        "recommendation": rec,
        "h2h": (h2h.get("matches") if h2h else None),
        "lines": merged_lines,
    }



def _season_finished_fixtures(team_id, season, venue="all"):
    try:
        data = fm.api_get("/fixtures", {"team": int(team_id), "season": int(season)}, ttl=fm.CACHE_TTL_LIVE)
    except ValueError:
        return []
    rows = []
    for item in data.get("response") or []:
        mapped = fm._map_fixture(item)
        if mapped.get("status") not in FINISHED:
            continue
        home_id = mapped.get("home_id")
        away_id = mapped.get("away_id")
        if home_id == team_id:
            loc, opponent, opponent_id, opponent_logo = "home", mapped.get("away"), away_id, mapped.get("away_logo")
            gf, ga = mapped.get("goals_home"), mapped.get("goals_away")
        elif away_id == team_id:
            loc, opponent, opponent_id, opponent_logo = "away", mapped.get("home"), home_id, mapped.get("home_logo")
            gf, ga = mapped.get("goals_away"), mapped.get("goals_home")
        else:
            continue
        if venue in ("home", "away") and loc != venue:
            continue
        gf, ga = _num(gf), _num(ga)
        result = "W" if gf > ga else ("L" if gf < ga else "D")
        rows.append({
            "fixture_id": (item.get("fixture") or {}).get("id"),
            "date": mapped.get("date"),
            "league": mapped.get("league"),
            "season": mapped.get("season"),
            "round": mapped.get("round"),
            "venue": loc,
            "opponent": opponent,
            "opponent_id": opponent_id,
            "opponent_logo": opponent_logo,
            "goals_for": gf,
            "goals_against": ga,
            "btts": 1 if gf > 0 and ga > 0 else 0,
            "result": result,
            "score": f"{int(gf)}-{int(ga)}",
        })
    rows.sort(key=lambda r: r.get("date") or "", reverse=True)
    return rows


def _lookup_player(player=None, player_id=None):
    if player_id:
        try:
            data = fm.api_get("/players/profiles", {"player": int(player_id)}, ttl=fm.CACHE_TTL_STABLE)
            item = (data.get("response") or [None])[0]
            if item:
                p = item.get("player") or item
                return {
                    "id": p.get("id") or int(player_id),
                    "name": p.get("name"),
                    "fullname": " ".join(x for x in [p.get("firstname"), p.get("lastname")] if x) or p.get("name"),
                    "age": p.get("age"),
                    "photo": p.get("photo"),
                    "position": p.get("position") or p.get("pos"),
                    "nationality": p.get("nationality"),
                }
        except ValueError:
            pass
        return {"id": int(player_id), "name": player}
    if player:
        data = search_entities(player, limit=8)
        for p in data.get("players") or []:
            if fm._name_matches(player, p.get("name") or "") or fm._name_matches(player, p.get("fullname") or ""):
                return p
        if data.get("players"):
            return data["players"][0]
    return None


def _player_team(player_id, season, team_id=None, league=None):
    if team_id:
        try:
            data = fm.api_get("/teams", {"id": int(team_id)}, ttl=fm.CACHE_TTL_STABLE)
            item = (data.get("response") or [None])[0]
            if item:
                team = fm._map_team(item)
                return team, season
        except ValueError:
            pass
    params = {"id": int(player_id), "season": int(season)}
    if league:
        params["league"] = int(league)
    try:
        data = fm.api_get("/players", params, ttl=fm.CACHE_TTL_STABLE)
    except ValueError:
        return None, season
    item = (data.get("response") or [None])[0]
    if not item:
        return None, season
    stats = (item.get("statistics") or [{}])[0]
    team = stats.get("team") or {}
    if team.get("id"):
        return {
            "id": team.get("id"),
            "name": team.get("name"),
            "logo": team.get("logo"),
        }, (stats.get("league") or {}).get("season") or season
    return None, season


def _player_from_fixture(fixture_id, player_id):
    try:
        data = fm.api_get("/fixtures/players", {"fixture": int(fixture_id)}, ttl=CACHE_TTL_FIXTURE)
    except ValueError:
        return None
    for block in data.get("response") or []:
        for item in block.get("players") or []:
            info = item.get("player") or {}
            if info.get("id") != player_id:
                continue
            st = (item.get("statistics") or [{}])[0]
            games = st.get("games") or {}
            minutes = games.get("minutes")
            if minutes is None or _num(minutes) <= 0:
                return {"dnp": True, "minutes": minutes, "position": games.get("position")}
            shots = st.get("shots") or {}
            goals = st.get("goals") or {}
            fouls = st.get("fouls") or {}
            return {
                "dnp": False,
                "minutes": _num(minutes),
                "position": games.get("position"),
                "rating": games.get("rating"),
                "shots": _num(shots.get("total")),
                "shots_on_target": _num(shots.get("on")),
                "goals": _num(goals.get("total")),
                "assists": _num(goals.get("assists")),
                "saves": _num(goals.get("saves")),
                "fouls_committed": _num(fouls.get("committed")),
                "fouls_drawn": _num(fouls.get("drawn")),
            }
    return None



def _season_player_totals(player_id, season, team_id=None, league=None):
    params = {"id": int(player_id), "season": int(season)}
    if team_id:
        params["team"] = int(team_id)
    if league:
        params["league"] = int(league)
    try:
        data = fm.api_get("/players", params, ttl=fm.CACHE_TTL_STABLE)
    except ValueError:
        return None
    rows = []
    position = None
    for item in data.get("response") or []:
        for st in item.get("statistics") or []:
            games = st.get("games") or {}
            shots = st.get("shots") or {}
            goals = st.get("goals") or {}
            fouls = st.get("fouls") or {}
            apps = games.get("appearences") or games.get("appearances") or 0
            position = games.get("position") or position
            rows.append({
                "league": (st.get("league") or {}).get("name"),
                "team": (st.get("team") or {}).get("name"),
                "appearances": apps,
                "minutes": games.get("minutes"),
                "shots": shots.get("total"),
                "shots_on_target": shots.get("on"),
                "goals": goals.get("total"),
                "assists": goals.get("assists"),
                "saves": goals.get("saves"),
                "fouls_committed": fouls.get("committed"),
                "fouls_drawn": fouls.get("drawn"),
                "rating": games.get("rating"),
            })
    return {"position": position, "leagues": rows} if rows else None


def analyze_player_props(player=None, player_id=None, team=None, team_id=None, league=None,
                         last_n=10, lines=None, season=None, market=None):
    last_n = _clamp_last_n(last_n)
    prof = _lookup_player(player, player_id)
    if not prof or not prof.get("id"):
        return None
    player_id = prof["id"]

    if not season:
        if league:
            try:
                season = fm.get_current_season(league)
            except ValueError:
                season = 2026
        else:
            season = 2026

    team_info, season = _player_team(player_id, season, team_id, league)
    if not team_info and team:
        found = fm.find_team(team, league, season)
        if found:
            team_info = found
    if not team_info:
        return {
            "player": prof,
            "team": None,
            "season": season,
            "last_n": last_n,
            "matches": [],
            "stats": {},
            "error": "No se encontró el equipo actual del jugador para armar el historial.",
        }

    team_id = team_info["id"]
    season_totals = _season_player_totals(player_id, season, team_id, league)
    fixtures = _season_finished_fixtures(team_id, season, "all")
    if not fixtures:
        fixtures = _finished_fixtures(team_id, 30, "all", clamp=False)
    rows = []
    pos = prof.get("position") or (season_totals or {}).get("position")
    scanned = 0
    for fx in fixtures:
        if scanned >= 40:
            break
        rec = _player_from_fixture(fx["fixture_id"], player_id)
        scanned += 1
        if not rec or rec.get("dnp"):
            continue
        if rec.get("position"):
            pos = rec.get("position")
        row = dict(fx)
        row.update({k: rec.get(k) for k in (
            "minutes", "position", "rating", "shots", "shots_on_target",
            "goals", "assists", "saves", "fouls_committed", "fouls_drawn",
        )})
        rows.append(row)
        if len(rows) >= last_n:
            break

    merged_lines = dict(DEFAULT_LINES_PLAYER)
    if isinstance(lines, dict):
        for k, v in lines.items():
            try:
                merged_lines[k] = float(v)
            except (TypeError, ValueError):
                pass

    # Porteros: atajadas primero; de campo: remates
    is_gk = str(pos or "").lower() in ("g", "gk", "goalkeeper", "portero")
    keys = list(PLAYER_MARKETS)
    if is_gk:
        keys = [PLAYER_MARKETS[-1]] + PLAYER_MARKETS[:-1]

    stats = {}
    for key, _label, default in keys:
        stats[key] = summarize_series(rows, key, merged_lines.get(key, default))

    primary = market if market in stats else ("saves" if is_gk else "shots")
    if stats.get(primary, {}).get("games") in (0, None):
        for key, _l, _d in keys:
            if stats[key]["games"]:
                primary = key
                break
    rec = recommend(stats.get(primary))
    if prof.get("position") is None and pos:
        prof["position"] = pos

    return {
        "player": prof,
        "team": team_info,
        "season": season,
        "last_n": last_n,
        "is_goalkeeper": is_gk,
        "matches": rows,
        "stats": stats,
        "primary": primary,
        "recommendation": rec,
        "lines": merged_lines,
        "played": len(rows),
        "season_totals": season_totals,
        "per_match": bool(rows),
    }
