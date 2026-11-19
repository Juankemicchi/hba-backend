"""Durable VIP pick history: daily snapshots, auto-grading, running totals.

Writes go through Supabase using SUPABASE_SERVICE_ROLE_KEY when present, or a
SECURITY DEFINER RPC guarded by VIP_HISTORY_SECRET. Reads can use the anon key
(SELECT is allowed). None of these secrets belong in the repo.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo

import football_module as fm
import football_stats as fs
import nba_module as nba

BOGOTA = ZoneInfo("America/Bogota")

FINISHED_FOOTBALL = {"FT", "AET", "PEN"}
VOID_FOOTBALL = {"CANC", "ABD", "AWD", "WO", "PST"}
YES_WORDS = {"yes", "si", "sí", "s", "true", "1"}
NO_WORDS = {"no", "n", "false", "0"}


def _today() -> str:
    return datetime.now(BOGOTA).date().isoformat()


def pick_key(date_str, sport, match, market_label, selection, line) -> str:
    line_s = "na" if line is None or line == "" else str(line)
    parts = [date_str or "", sport or "", match or "", market_label or "", selection or "", line_s]
    return "|".join(str(p).strip() for p in parts)


def units_for(result: Optional[str], odd) -> Optional[float]:
    result = (result or "").lower()
    if result == "perdido":
        return -1.0
    if result == "nulo":
        return 0.0
    if result == "pendiente":
        return None
    if result == "ganado":
        try:
            return round(float(odd) - 1.0, 2)
        except (TypeError, ValueError):
            return None
    return None


def grade_over_under(value, line, side: str) -> Optional[str]:
    if value is None or line is None or side not in {"Over", "Under"}:
        return None
    try:
        value = float(value)
        line = float(line)
    except (TypeError, ValueError):
        return None
    if side == "Under":
        return "ganado" if value <= line else "perdido"
    return "ganado" if value > line else "perdido"


def _num(val):
    if val is None or val == "":
        return None
    if isinstance(val, str) and val.endswith("%"):
        val = val[:-1]
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def row_from_pick(date_str: str, pick: dict) -> dict:
    market_label = pick.get("market_label") or pick.get("market") or ""
    selection = pick.get("selection") or ""
    line = pick.get("line")
    if line is not None:
        try:
            line = float(line)
        except (TypeError, ValueError):
            pass
    odd = pick.get("avg_odd")
    if odd is not None:
        try:
            odd = float(odd)
        except (TypeError, ValueError):
            odd = None
    fid = pick.get("fixture_id")
    return {
        "pick_key": pick_key(
            date_str,
            pick.get("sport") or "",
            pick.get("match") or "",
            market_label,
            selection,
            line,
        ),
        "pick_date": date_str,
        "sport": pick.get("sport") or "",
        "league": pick.get("league"),
        "league_id": pick.get("league_id"),
        "match": pick.get("match") or "",
        "market": pick.get("market"),
        "market_label": market_label,
        "line": line,
        "selection": selection,
        "odd": odd,
        "confidence": pick.get("confidence"),
        "maxima_confianza": bool(pick.get("maxima_confianza")),
        "fixture_id": str(fid) if fid is not None else None,
        "entity": pick.get("entity"),
        "entity_id": str(pick["entity_id"]) if pick.get("entity_id") is not None else None,
        "entity_side": pick.get("entity_side"),
        "kickoff": pick.get("kickoff"),
        "result": "pendiente",
        "units": None,
        "actual_value": None,
        "grade_note": None,
        "raw": {
            "reason": pick.get("reason"),
            "hit_rate": pick.get("hit_rate"),
            "games": pick.get("games"),
            "bookmakers": pick.get("bookmakers"),
            "odds_verified": pick.get("odds_verified"),
            "bet_name": pick.get("bet_name"),
            "odds_value": pick.get("odds_value"),
        },
    }


def evaluate_football(pick: dict, fixture_item: Optional[dict], stats_response) -> dict:
    """Grade one football pick from a fixture payload and /fixtures/statistics."""
    if not fixture_item:
        return {"result": "pendiente", "note": "Sin marcador final", "actual": None, "units": None}

    status = ((fixture_item.get("fixture") or {}).get("status") or {}).get("short") or ""
    status = str(status).upper()
    if status in VOID_FOOTBALL:
        result = "nulo"
        return {"result": result, "note": f"Partido {status}", "actual": None, "units": units_for(result, pick.get("odd") or pick.get("avg_odd"))}
    if status not in FINISHED_FOOTBALL:
        return {"result": "pendiente", "note": f"Estado {status or 'pendiente'}", "actual": None, "units": None}

    goals = fixture_item.get("goals") or {}
    gh, ga = _num(goals.get("home")), _num(goals.get("away"))
    teams = fixture_item.get("teams") or {}
    home = teams.get("home") or {}
    away = teams.get("away") or {}
    home_id = home.get("id")
    away_id = away.get("id")
    home_name = home.get("name") or ""
    away_name = away.get("name") or ""

    entity_id = pick.get("entity_id")
    try:
        entity_id = int(entity_id) if entity_id is not None and str(entity_id).strip() != "" else None
    except (TypeError, ValueError):
        entity_id = None
    side = (pick.get("entity_side") or "").lower()
    if entity_id is None and side == "home":
        entity_id = home_id
    if entity_id is None and side == "away":
        entity_id = away_id

    mine, opp = fs._stat_maps(stats_response or [], entity_id) if entity_id else ({}, {})
    if not mine and not opp and (home_id or away_id):
        # Match-level stats: still parse both teams.
        mine_h, mine_a = fs._stat_maps(stats_response or [], home_id)
        # corners/cards totals from both maps
        mine, opp = mine_h, mine_a

    corners_home = fs._pick(fs._stat_maps(stats_response or [], home_id)[0], "Corner Kicks") if home_id else None
    corners_away = fs._pick(fs._stat_maps(stats_response or [], away_id)[0], "Corner Kicks") if away_id else None
    yellow_home = fs._pick(fs._stat_maps(stats_response or [], home_id)[0], "Yellow Cards") if home_id else None
    yellow_away = fs._pick(fs._stat_maps(stats_response or [], away_id)[0], "Yellow Cards") if away_id else None
    red_home = fs._pick(fs._stat_maps(stats_response or [], home_id)[0], "Red Cards") if home_id else None
    red_away = fs._pick(fs._stat_maps(stats_response or [], away_id)[0], "Red Cards") if away_id else None

    def _cards(y, r):
        if y is None:
            return None
        return y + (r if r is not None else 0)

    cards_home = _cards(yellow_home, red_home)
    cards_away = _cards(yellow_away, red_away)
    corners_total = None if corners_home is None or corners_away is None else corners_home + corners_away
    cards_total = None if cards_home is None or cards_away is None else cards_home + cards_away

    if entity_id == home_id:
        gf, ga_ = gh, ga
        corners_for, corners_against = corners_home, corners_away
        cards_for, cards_against = cards_home, cards_away
        yellow_for = yellow_home
    elif entity_id == away_id:
        gf, ga_ = ga, gh
        corners_for, corners_against = corners_away, corners_home
        cards_for, cards_against = cards_away, cards_home
        yellow_for = yellow_away
    else:
        gf, ga_ = gh, ga
        corners_for = corners_against = None
        cards_for = cards_against = None
        yellow_for = None

    market = (pick.get("market") or "").lower()
    selection = (pick.get("selection") or "").strip()
    line = pick.get("line")
    odd = pick.get("odd") if pick.get("odd") is not None else pick.get("avg_odd")
    actual = None
    result = None
    note = None

    if market in {"1x2", "moneyline"} or (pick.get("bet_id") == 1):
        if gh is None or ga is None:
            return {"result": "pendiente", "note": "Sin marcador", "actual": None, "units": None}
        if gh > ga:
            winner = home_name
        elif ga > gh:
            winner = away_name
        else:
            winner = "Empate"
        actual = f"{int(gh)}-{int(ga)}"
        sel_norm = re.sub(r"\s+", " ", selection).strip().lower()
        win_norm = winner.strip().lower()
        aliases = {
            "empate": "empate",
            "draw": "empate",
            "x": "empate",
            "1": home_name.lower(),
            "2": away_name.lower(),
        }
        target = aliases.get(sel_norm, sel_norm)
        result = "ganado" if target == win_norm or (target and target in win_norm) or (win_norm and win_norm in target) else "perdido"
        note = f"Resultado {actual}"
    elif market == "btts":
        if gh is None or ga is None:
            return {"result": "pendiente", "note": "Sin marcador", "actual": None, "units": None}
        both = gh > 0 and ga > 0
        actual = 1 if both else 0
        word = selection.lower().replace("í", "i")
        if word in YES_WORDS or word.startswith("si"):
            result = "ganado" if both else "perdido"
        elif word in NO_WORDS:
            result = "ganado" if not both else "perdido"
        else:
            return {"result": "pendiente", "note": "Selección BTTS no reconocida", "actual": actual, "units": None}
        note = "Ambos anotaron" if both else "No ambos anotaron"
    else:
        value_map = {
            "goals_for": gf,
            "goals_against": ga_,
            "goals_total": None if gh is None or ga is None else gh + ga,
            "corners_for": corners_for,
            "corners_against": corners_against,
            "corners_total": corners_total,
            "cards": cards_for,
            "yellow": yellow_for,
            "cards_against": cards_against,
            "cards_total": cards_total,
        }
        actual = value_map.get(market)
        if actual is None and market in value_map:
            return {"result": "pendiente", "note": "Estadística final no reportada", "actual": None, "units": None}
        if actual is None:
            return {"result": "pendiente", "note": f"Mercado no graduable: {market}", "actual": None, "units": None}
        result = grade_over_under(actual, line, selection)
        if result is None:
            return {"result": "pendiente", "note": "Línea o selección incompleta", "actual": actual, "units": None}
        note = f"Valor final {actual}"

    return {"result": result, "note": note, "actual": actual, "units": units_for(result, odd)}


def evaluate_nba(pick: dict, game_row: Optional[dict]) -> dict:
    if not game_row:
        return {"result": "pendiente", "note": "Sin box score final", "actual": None, "units": None}
    mins = _num(game_row.get("MIN") if game_row.get("MIN") is not None else game_row.get("min"))
    # A DNP / unplayed row should stay pending, not be graded as a zero.
    if mins is not None and mins <= 0:
        return {"result": "pendiente", "note": "Sin minutos en el box score", "actual": None, "units": None}
    market = (pick.get("market") or "").upper()
    aliases = {
        "PUNTOS": "PTS",
        "REBOTES": "REB",
        "ASISTENCIAS": "AST",
        "TRIPLES": "FG3M",
        "PTS+REB": "PR",
        "PTS + REB": "PR",
        "PUNTOS + REBOTES": "PR",
        "PUNTOS+REBOTES": "PR",
        "PR": "PR",
        "PRA": "PRA",
        "PA": "PA",
        "RA": "RA",
        "PTS": "PTS",
        "REB": "REB",
        "AST": "AST",
        "FG3M": "FG3M",
        "STL": "STL",
        "BLK": "BLK",
        "TO": "TO",
        "MIN": "MIN",
    }
    col = aliases.get(market, market)
    actual = game_row.get(col)
    if actual is None and col == "PR":
        pts, reb = _num(game_row.get("PTS")), _num(game_row.get("REB"))
        actual = None if pts is None or reb is None else pts + reb
    if actual is None:
        return {"result": "pendiente", "note": f"Box score sin {col}", "actual": None, "units": None}
    result = grade_over_under(actual, pick.get("line"), pick.get("selection") or "")
    if result is None:
        return {"result": "pendiente", "note": "Línea o selección incompleta", "actual": actual, "units": None}
    odd = pick.get("odd") if pick.get("odd") is not None else pick.get("avg_odd")
    return {"result": result, "note": f"Valor final {actual}", "actual": actual, "units": units_for(result, odd)}


def _supabase_url_headers(write: bool):
    url = (os.getenv("SUPABASE_URL") or "").rstrip("/")
    if not url:
        return None, None, None
    service = os.getenv("SUPABASE_SERVICE_ROLE_KEY") or ""
    secret = os.getenv("VIP_HISTORY_SECRET") or ""
    anon = os.getenv("SUPABASE_ANON_KEY") or ""
    if write and service:
        key, mode = service, "service"
    elif write and secret and anon:
        key, mode = anon, "rpc"
    elif write and secret:
        # RPC can also be called with the secret in the body and the anon key
        # as apikey; if anon is missing, we cannot talk to PostgREST.
        return None, None, None
    elif anon:
        key, mode = anon, "anon"
    elif service:
        key, mode = service, "service"
    else:
        return None, None, None
    headers = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    return url, headers, mode


def _http(method: str, url: str, headers: dict, body=None, timeout: int = 20):
    data = None if body is None else json.dumps(body).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {"ok": True, "status": resp.status, "data": None}
            try:
                return {"ok": True, "status": resp.status, "data": json.loads(raw.decode())}
            except json.JSONDecodeError:
                return {"ok": True, "status": resp.status, "data": raw.decode()}
    except urllib.error.HTTPError as exc:
        err = exc.read().decode("utf-8", errors="replace")
        return {"ok": False, "status": exc.code, "data": err}
    except Exception as exc:
        return {"ok": False, "status": 0, "data": f"{type(exc).__name__}"}


def upsert_rows(rows: list[dict]) -> int:
    if not rows:
        return 0
    url, headers, mode = _supabase_url_headers(write=True)
    if not url:
        return 0
    payload = []
    for row in rows:
        item = dict(row)
        if item.get("raw") is not None and not isinstance(item["raw"], str):
            # leave jsonb as object for PostgREST
            pass
        payload.append(item)
    if mode == "service":
        headers = dict(headers)
        headers["Prefer"] = "resolution=merge-duplicates,return=minimal"
        # Do not clobber a grade that has already been set.
        # Use RPC-like two-step: insert new, skip updates of graded rows via
        # a filtered upsert is hard in one REST call, so we use the RPC when
        # possible. With service role we upsert only snapshot columns when
        # the existing result is pendiente, otherwise insert-or-ignore grades.
        res = _http("POST", f"{url}/rest/v1/rpc/vip_history_upsert_rows", headers, {"p_rows": payload})
        if res.get("ok"):
            try:
                return int(res.get("data") or 0)
            except (TypeError, ValueError):
                return len(payload)
        # Fall back to direct upsert of pending-only rows.
        res2 = _http(
            "POST",
            f"{url}/rest/v1/vip_pick_history?on_conflict=pick_key",
            headers,
            payload,
        )
        return len(payload) if res2.get("ok") else 0
    if mode == "rpc":
        secret = os.getenv("VIP_HISTORY_SECRET") or ""
        res = _http(
            "POST",
            f"{url}/rest/v1/rpc/vip_history_upsert",
            headers,
            {"p_secret": secret, "p_rows": payload},
        )
        if not res.get("ok"):
            return 0
        try:
            return int(res.get("data") or 0)
        except (TypeError, ValueError):
            return len(payload)
    return 0


def set_result(pick_key_val: str, result: str, units, note, actual) -> bool:
    url, headers, mode = _supabase_url_headers(write=True)
    if not url:
        return False
    body = {
        "p_pick_key": pick_key_val,
        "p_result": result,
        "p_units": units,
        "p_note": note,
        "p_actual": actual,
    }
    if mode == "rpc":
        body["p_secret"] = os.getenv("VIP_HISTORY_SECRET") or ""
        res = _http("POST", f"{url}/rest/v1/rpc/vip_history_set_result", headers, body)
        return bool(res.get("ok"))
    if mode == "service":
        # Prefer the same SQL function (no secret required for service_role).
        res = _http(
            "POST",
            f"{url}/rest/v1/rpc/vip_history_set_result_admin",
            headers,
            {
                "p_pick_key": pick_key_val,
                "p_result": result,
                "p_units": units,
                "p_note": note,
                "p_actual": actual,
            },
        )
        if res.get("ok"):
            return True
        patch = {
            "result": result,
            "units": units,
            "grade_note": note,
            "actual_value": actual,
            "graded_at": datetime.now(BOGOTA).isoformat(),
        }
        q = urllib.parse.quote(pick_key_val, safe="")
        headers = dict(headers)
        headers["Prefer"] = "return=minimal"
        res2 = _http("PATCH", f"{url}/rest/v1/vip_pick_history?pick_key=eq.{q}", headers, patch)
        return bool(res2.get("ok"))
    return False


def fetch_rows(limit: int = 500, only_pending: bool = False) -> list[dict]:
    url, headers, mode = _supabase_url_headers(write=False)
    if not url:
        return []
    params = "select=*&order=pick_date.desc,kickoff.desc,published_at.desc&limit=" + str(int(limit))
    if only_pending:
        params += "&result=eq.pendiente"
    res = _http("GET", f"{url}/rest/v1/vip_pick_history?{params}", headers)
    if res.get("ok") and isinstance(res.get("data"), list):
        return res["data"]
    return []


def summarize(rows: list[dict]) -> dict:
    total = len(rows)
    won = sum(1 for r in rows if (r.get("result") or "").lower() == "ganado")
    lost = sum(1 for r in rows if (r.get("result") or "").lower() == "perdido")
    void = sum(1 for r in rows if (r.get("result") or "").lower() == "nulo")
    pending = sum(1 for r in rows if (r.get("result") or "").lower() == "pendiente")
    settled = won + lost
    hit_rate = round(100.0 * won / settled, 1) if settled else None
    units = 0.0
    units_counted = 0
    for r in rows:
        result = (r.get("result") or "").lower()
        if result not in {"ganado", "perdido"}:
            continue
        u = r.get("units")
        if u is None:
            u = units_for(result, r.get("odd"))
        if u is None:
            continue
        try:
            units += float(u)
            units_counted += 1
        except (TypeError, ValueError):
            continue
    return {
        "total": total,
        "won": won,
        "lost": lost,
        "void": void,
        "pending": pending,
        "hit_rate": hit_rate,
        "units": round(units, 2),
        "units_n": units_counted,
    }


def list_history(limit_days: int = 60) -> dict:
    rows = fetch_rows(limit=1000)
    if limit_days and limit_days > 0:
        cutoff = (datetime.now(BOGOTA).date() - timedelta(days=int(limit_days))).isoformat()
        rows = [r for r in rows if str(r.get("pick_date") or "") >= cutoff]
    days: dict[str, list] = {}
    for row in rows:
        days.setdefault(str(row.get("pick_date") or ""), []).append(row)
    grouped = []
    for date_str in sorted(days.keys(), reverse=True):
        items = days[date_str]
        grouped.append({"date": date_str, "picks": items, "summary": summarize(items)})
    return {
        "ok": True,
        "timezone": "America/Bogota",
        "summary": summarize(rows),
        "days": grouped,
        "write_ready": bool(_supabase_url_headers(write=True)[0]),
    }


def publish_slate(data: dict) -> int:
    if not isinstance(data, dict):
        return 0
    date_str = data.get("date")
    picks = data.get("picks") or []
    if not date_str or not picks:
        return 0
    rows = [row_from_pick(date_str, p) for p in picks if isinstance(p, dict)]
    rows = [r for r in rows if r.get("match") and r.get("market_label") and r.get("selection")]
    try:
        return upsert_rows(rows)
    except Exception:
        return 0


def _fetch_football_final(fixture_id):
    fid = int(fixture_id)
    data = fm.api_get("/fixtures", {"id": fid}, ttl=15 * 60)
    item = (data.get("response") or [None])[0]
    status = (((item or {}).get("fixture") or {}).get("status") or {}).get("short") or ""
    if str(status).upper() not in FINISHED_FOOTBALL | VOID_FOOTBALL:
        data = fm.api_get("/fixtures", {"id": fid}, ttl=15 * 60, force=True)
        item = (data.get("response") or [None])[0]
        status = (((item or {}).get("fixture") or {}).get("status") or {}).get("short") or ""
    stats_resp = []
    if str(status).upper() in FINISHED_FOOTBALL:
        # Always refresh statistics for a finished match if the cache might
        # still hold an empty pre-game payload.
        stats = fm.api_get("/fixtures/statistics", {"fixture": fid}, ttl=15 * 60)
        stats_resp = stats.get("response") or []
        if not stats_resp:
            stats = fm.api_get("/fixtures/statistics", {"fixture": fid}, ttl=15 * 60, force=True)
            stats_resp = stats.get("response") or []
    return item, stats_resp


def _nba_row_for_pick(pick: dict) -> Optional[dict]:
    name = pick.get("entity") or ""
    # market_label is often "Name Prop"
    if not name and pick.get("market_label"):
        name = str(pick.get("market_label")).split(" ")[0:2]
        name = " ".join(name)
    player_id = pick.get("entity_id")
    try:
        df, full_name, _profile = nba.get_player_games(name, last_n=8, player_id=player_id)
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    date_str = str(pick.get("pick_date") or "")[:10]
    kick = str(pick.get("kickoff") or "")[:10]
    target_dates = {d for d in (date_str, kick) if d}
    # Late US tip-offs can land on the next (or previous) Bogota date.
    for base in list(target_dates):
        try:
            dt = datetime.strptime(base, "%Y-%m-%d").date()
            target_dates.add((dt - timedelta(days=1)).isoformat())
            target_dates.add((dt + timedelta(days=1)).isoformat())
        except ValueError:
            pass
    match_blob = (pick.get("match") or "").lower()
    best = None
    for rec in df.to_dict("records"):
        raw_date = rec.get("date") or rec.get("GAME_DATE") or ""
        game_date = str(raw_date)[:10]
        if hasattr(raw_date, "strftime"):
            game_date = raw_date.strftime("%Y-%m-%d")
        rec["_game_date"] = game_date
        opp = (rec.get("opponent") or "").lower()
        date_ok = (not target_dates) or (game_date in target_dates)
        opp_ok = True
        if opp and match_blob:
            nick = re.sub(r"[^a-z0-9]+", "", opp)
            opp_ok = nick and nick in re.sub(r"[^a-z0-9]+", "", match_blob)
        if date_ok and (opp_ok or not opp):
            best = rec
            break
    if best is None and target_dates:
        for rec in df.to_dict("records"):
            raw_date = rec.get("date") or rec.get("GAME_DATE") or ""
            game_date = str(raw_date)[:10]
            if hasattr(raw_date, "strftime"):
                game_date = raw_date.strftime("%Y-%m-%d")
            if game_date in target_dates:
                best = rec
                break
    return best


def grade_row(row: dict) -> dict:
    sport = (row.get("sport") or "").lower()
    pick = dict(row)
    if sport in {"football", "futbol", "fútbol"}:
        fid = row.get("fixture_id")
        if not fid:
            return {"result": "pendiente", "note": "Sin fixture_id", "actual": None, "units": None}
        item, stats = _fetch_football_final(fid)
        return evaluate_football(pick, item, stats)
    if sport == "nba":
        game = _nba_row_for_pick(pick)
        return evaluate_nba(pick, game)
    return {"result": "pendiente", "note": "Deporte no graduable", "actual": None, "units": None}


def grade_pending(limit: int = 200) -> dict:
    rows = fetch_rows(limit=limit, only_pending=True)
    report = {"checked": 0, "graded": 0, "errors": 0, "results": {}}
    for row in rows:
        report["checked"] += 1
        try:
            graded = grade_row(row)
        except Exception:
            report["errors"] += 1
            continue
        result = (graded or {}).get("result") or "pendiente"
        if result == "pendiente":
            continue
        ok = set_result(
            row.get("pick_key"),
            result,
            graded.get("units"),
            graded.get("note"),
            graded.get("actual"),
        )
        if ok:
            report["graded"] += 1
            report["results"][result] = report["results"].get(result, 0) + 1
        else:
            report["errors"] += 1
    return report
