"""VIP NBA player-prop picks from BigBalls game logs.

Lines are an HBA estimate near the player's recent median (sportsbook-style
.5 lines). They are never presented as real bookmaker odds. If Grok web
search returns a sourced sportsbook line, it is shown separately.
"""

from __future__ import annotations

import json
import os
import re
import statistics
from datetime import datetime
from typing import Optional
import urllib.request
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import nba_module as nba
from openai import OpenAI

BOGOTA = ZoneInfo("America/Bogota")

MIN_MINUTES = 24.0
MIN_PLAYED_RECENT = 8
LOOKBACK_RECENT = 10
LOOKBACK_SEASON = 25
DNP_MINUTES = 8.0
MIN_HIT_RECENT = 70.0
MIN_HIT_SEASON = 55.0
LINE_MAX_GAP = 1.0  # line must stay within 1.0 of the last-10 median
MAX_PLAYERS_PER_TEAM = 18
MAX_PROPS_PER_PLAYER = 1
MAX_PROPS_PER_GAME = 2

# Skip these official injury designations when present.
OUT_STATUSES = {
    "out", "doubtful", "inactive", "suspended", "injured",
    "unavailable", "ofs", "out for season", "out indefinitely",
}

PROP_MARKETS = [
    ("PTS", "Puntos", 8.5),
    ("REB", "Rebotes", 2.5),
    ("AST", "Asistencias", 1.5),
    ("FG3M", "Triples", 0.5),
    ("PRA", "PRA", 15.5),
    ("PR", "Puntos + rebotes", 10.5),
    ("PA", "Puntos + asistencias", 10.5),
    ("RA", "Rebotes + asistencias", 4.5),
]
PROP_LABELS = {k: label for k, label, _ in PROP_MARKETS}
PROP_MIN_LINE = {k: mn for k, _label, mn in PROP_MARKETS}

BOOK_DOMAINS = [
    "bettingpros.com",
    "actionnetwork.com",
    "covers.com",
    "oddsshark.com",
    "fantasypros.com",
]


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _median(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(statistics.median(values))


def _mean(values: list[float]) -> Optional[float]:
    if not values:
        return None
    return float(sum(values) / len(values))


def _hit(values: list[float], line: float, side: str) -> tuple[int, int, float]:
    games = len(values)
    if not games:
        return 0, 0, 0.0
    if side == "Over":
        hits = sum(1 for v in values if v > line)
    else:
        hits = sum(1 for v in values if v <= line)
    return hits, games, round(100.0 * hits / games, 1)


def estimated_line(recent: list[float], season: list[float], min_line: float) -> Optional[float]:
    """Sportsbook-style .5 line near the player's median, never far below it."""
    lines = candidate_lines(recent, season, min_line)
    return lines[0] if lines else None


def candidate_lines(recent: list[float], season: list[float], min_line: float) -> list[float]:
    """Primary line plus .5 neighbors still within 1.0 of the recent median.

    Closest to the median first, so we never shop a far easier number.
    """
    if len(recent) < MIN_PLAYED_RECENT:
        return []
    med_r = _median(recent)
    med_s = _median(season) if season else med_r
    mean_r = _mean(recent)
    raw = 0.50 * med_r + 0.30 * (med_s if med_s is not None else med_r) + 0.20 * mean_r
    primary = max(float(min_line), float(nba.snap_half(raw, min_line)))
    options = {primary}
    for delta in (-1.0, -0.5, 0.0, 0.5, 1.0):
        ln = max(float(min_line), float(nba.snap_half(med_r + delta, min_line)))
        if abs(ln - med_r) <= LINE_MAX_GAP + 1e-9:
            options.add(ln)
    # Drop anything that drifted away from the median after snapping.
    close = [ln for ln in options if abs(ln - med_r) <= LINE_MAX_GAP + 1e-9]
    return sorted(set(close), key=lambda ln: (abs(ln - med_r), ln))


def _played_rows(rows: list[dict]) -> list[dict]:
    out = []
    for row in rows or []:
        mins = _num(row.get("MIN"), 0) or 0
        if mins >= DNP_MINUTES:
            out.append(row)
    return out


def _is_home(row: dict) -> Optional[bool]:
    for key in ("is_home", "at_home", "home"):
        val = row.get(key)
        if isinstance(val, bool):
            return val
        if isinstance(val, str) and val.strip().lower() in {"home", "h", "true", "1"}:
            return True
        if isinstance(val, str) and val.strip().lower() in {"away", "a", "false", "0"}:
            return False
    loc = str(row.get("location") or row.get("ha") or row.get("home_away") or "").strip().lower()
    if loc in {"home", "h"}:
        return True
    if loc in {"away", "a", "road"}:
        return False
    return None


def _opp_name(row: dict) -> str:
    return str(row.get("opponent") or row.get("abbr") or "").strip().lower()


def _values(rows: list[dict], key: str) -> list[float]:
    vals = []
    for row in rows:
        val = _num(row.get(key))
        if val is None:
            continue
        vals.append(val)
    return vals


def _status_is_out(status: Optional[str]) -> bool:
    if not status:
        return False
    low = str(status).strip().lower()
    if low in OUT_STATUSES:
        return True
    return any(tag in low for tag in ("out", "inactive", "suspended", "indefinitely"))


def _norm_player_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", " ", (name or "").lower()).strip()
    text = re.sub(r"\b(jr|sr|iii|ii|iv)\b", "", text)
    return re.sub(r"\s+", " ", text).strip()


def fetch_espn_injuries() -> dict[str, dict]:
    """Public ESPN NBA injury report. Best-effort; never fatal."""
    index: dict[str, dict] = {}
    try:
        req = urllib.request.Request(
            "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/injuries",
            headers={"User-Agent": "HBA-VIP-picks/1.0"},
        )
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read().decode())
    except Exception:
        return index
    for team in data.get("injuries") or []:
        if not isinstance(team, dict):
            continue
        team_name = team.get("displayName")
        for row in team.get("injuries") or []:
            if not isinstance(row, dict):
                continue
            ath = row.get("athlete") or {}
            name = (ath.get("displayName") if isinstance(ath, dict) else None) or row.get("displayName") or ""
            name = str(name).strip()
            if not name:
                continue
            rec = {
                "status": row.get("status"),
                "comment": row.get("shortComment") or row.get("longComment"),
                "name": name,
                "team": team_name,
                "source": "espn",
            }
            index[f"name:{name.lower()}"] = rec
            norm = _norm_player_name(name)
            if norm:
                index[f"name:{norm}"] = rec
    return index


def fetch_injuries() -> dict[str, dict]:
    """Map player id (and name) to the latest injury row. Best-effort; never fatal.

    BigBalls is the first source; the public ESPN report fills gaps so a
    confirmed Out player is not recommended.
    """
    index: dict[str, dict] = {}
    try:
        data = nba.api_get(
            "/injuries",
            {"sport": "basketball", "league": "nba", "limit": 200},
            ttl=nba.CACHE_TTL_LIVE,
        )
        payload = data.get("data") if isinstance(data, dict) else data
        rows = []
        if isinstance(payload, dict):
            rows = payload.get("injuries") or payload.get("data") or payload.get("results") or []
        elif isinstance(payload, list):
            rows = payload
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            player = item.get("player") if isinstance(item.get("player"), dict) else {}
            pid = str(player.get("id") or item.get("player_id") or "")
            name = (player.get("name") or item.get("player_name") or item.get("name") or "").strip()
            rec = {
                "status": item.get("status") or item.get("designation") or item.get("injury_status"),
                "comment": item.get("comment") or item.get("note") or item.get("description"),
                "name": name,
            }
            if pid:
                index[pid] = rec
            if name:
                index[f"name:{name.lower()}"] = rec
                norm = _norm_player_name(name)
                if norm:
                    index[f"name:{norm}"] = rec
    except Exception:
        pass
    for key, rec in fetch_espn_injuries().items():
        prev = index.get(key)
        if not prev or (_status_is_out(rec.get("status")) and not _status_is_out(prev.get("status"))):
            index[key] = rec
    return index


def _roster_players(team_id) -> list[dict]:
    if not team_id:
        return []
    try:
        data = nba.get_nba_roster(str(team_id))
    except Exception:
        return []
    players = [p for p in (data.get("players") or []) if p and p.get("id") and p.get("name")]
    # Historical rosters can be huge; prefer numbered, current-looking players.
    def rank(p):
        same_team = 0 if str(p.get("team_id") or "") == str(team_id) else 1
        has_num = 0 if p.get("number") else 1
        has_pos = 0 if p.get("position") else 1
        return (same_team, has_num, has_pos, p.get("name") or "")
    players.sort(key=rank)
    return players[:MAX_PLAYERS_PER_TEAM]


def _player_logs(player_name, player_id, last_n=LOOKBACK_SEASON) -> tuple[list[dict], dict]:
    try:
        df, full_name, profile = nba.get_player_games(player_name or "", last_n, player_id=player_id)
    except Exception:
        return [], {}
    if df is None or getattr(df, "empty", True):
        return [], profile or {}
    rows = df.to_dict(orient="records")
    for row in rows:
        if hasattr(row.get("GAME_DATE"), "isoformat"):
            row["GAME_DATE"] = row["GAME_DATE"].isoformat()
        row["date"] = str(row.get("date") or row.get("GAME_DATE") or "")[:10]
        if row.get("postseason"):
            continue
    rows = [r for r in rows if not r.get("postseason")]
    if profile is None:
        profile = {}
    if full_name:
        profile["name"] = full_name
    return rows, profile


def _rotation_ok(played: list[dict]) -> tuple[bool, str]:
    if len(played) < MIN_PLAYED_RECENT:
        return False, "pocos partidos recientes con minutos"
    recent = played[:LOOKBACK_RECENT]
    if len(recent) < MIN_PLAYED_RECENT:
        return False, "no disputó la mayoría de los partidos recientes"
    mins = [_num(r.get("MIN"), 0) or 0 for r in recent]
    avg_min = sum(mins) / len(mins)
    if avg_min < MIN_MINUTES:
        return False, "minutos inestables o de banca"
    # Two straight DNPs already excluded from `played`. If the most recent
    # actually played games show a collapse in minutes, skip.
    last3 = mins[:3]
    if len(last3) == 3 and sum(last3) / 3.0 < 16:
        return False, "minutos en caída (posible carga o molestia)"
    return True, ""


def _supportive(values: list[float], line: float, side: str, min_games: int, min_hit: float) -> Optional[bool]:
    if len(values) < min_games:
        return None
    _h, _g, hit = _hit(values, line, side)
    return hit >= min_hit


def _build_player_candidates(player: dict, game: dict, venue: str, opponent: str, injury: Optional[dict]) -> list[dict]:
    if injury and _status_is_out(injury.get("status")):
        return []
    rows, profile = _player_logs(player.get("name"), player.get("id"))
    played = _played_rows(rows)
    ok, why = _rotation_ok(played)
    if not ok:
        return []
    recent_rows = played[:LOOKBACK_RECENT]
    season_rows = played[:LOOKBACK_SEASON]
    avg_min = round(_mean([_num(r.get("MIN"), 0) or 0 for r in recent_rows]) or 0, 1)
    photo = (profile or {}).get("photo") or player.get("photo")
    team_name = (
        (profile or {}).get("team_name")
        or player.get("team_name")
        or (game.get("home") if venue == "home" else game.get("away"))
    )
    candidates = []
    opp_l = (opponent or "").strip().lower()
    for key, label, min_line in PROP_MARKETS:
        recent_vals = _values(recent_rows, key)
        season_vals = _values(season_rows, key)
        if len(recent_vals) < MIN_PLAYED_RECENT:
            continue
        lines = candidate_lines(recent_vals, season_vals, min_line)
        if not lines:
            continue
        med_r = _median(recent_vals)
        avg_r = _mean(recent_vals)
        avg_s = _mean(season_vals)
        chosen = None
        for line in lines:  # closest to the median first
            if med_r is not None and abs(line - med_r) > LINE_MAX_GAP + 0.01:
                continue
            for side in ("Over", "Under"):
                hits, games, hit = _hit(recent_vals, line, side)
                if games < MIN_PLAYED_RECENT or hit < MIN_HIT_RECENT:
                    continue
                # 100% on a line a full point off the median is a trivial number, skip it.
                if hit >= 99.9 and med_r is not None and abs(line - med_r) >= 0.99:
                    continue
                if side == "Over" and med_r is not None and line < med_r - LINE_MAX_GAP:
                    continue
                if side == "Under" and med_r is not None and line > med_r + LINE_MAX_GAP:
                    continue
                season_ok = _supportive(season_vals, line, side, min_games=10, min_hit=MIN_HIT_SEASON)
                if season_ok is False:
                    continue
                venue_vals = []
                want_home = venue == "home"
                for row, val in zip(season_rows, season_vals):
                    flag = _is_home(row)
                    if flag is None:
                        continue
                    if flag == want_home:
                        venue_vals.append(val)
                venue_ok = _supportive(venue_vals, line, side, min_games=3, min_hit=55.0)
                h2h_vals = []
                if opp_l:
                    for row in season_rows:
                        if opp_l and opp_l in _opp_name(row):
                            val = _num(row.get(key))
                            if val is not None:
                                h2h_vals.append(val)
                h2h_ok = _supportive(h2h_vals, line, side, min_games=2, min_hit=50.0)
                primary_bonus = 8 if key in {"PTS", "PRA", "PR", "PA"} else 0
                closeness = max(0.0, LINE_MAX_GAP - abs((line if med_r is None else line - med_r)))
                score = (hit - 70.0) * 2.0 + (8 if season_ok else 0) + min(games, 10) + primary_bonus + closeness * 2
                if venue_ok:
                    score += 3
                if h2h_ok:
                    score += 3
                score += min(avg_min, 36) * 0.15
                note_bits = [
                    f"Línea estimada {line} (mediana últimos {games}: {med_r}).",
                    "No es una cuota de casa de apuestas.",
                ]
                if injury and injury.get("status"):
                    note_bits.append(f"Reporte de lesión: {injury.get('status')}.")
                cand = {
                "sport": "nba",
                "kind": "player_prop",
                "market": key,
                "market_label": f"{player.get('name')} {label}",
                "selection": side,
                "line": line,
                "line_estimated": line,
                "line_type": "estimated",
                "line_label": "Línea estimada",
                "hit_rate": hit,
                "hits": hits,
                "games": games,
                "sample_size": games,
                "hit_rate_season": (_hit(season_vals, line, side)[2] if season_vals else None),
                "games_season": len(season_vals),
                "avg": round(avg_r, 1) if avg_r is not None else None,
                "avg_season": round(avg_s, 1) if avg_s is not None else None,
                "median": med_r,
                "avg_minutes": avg_min,
                "confidence": 5 if hit >= 80 and games >= 8 else (4 if hit >= 70 else 3),
                "risk": "baja" if hit >= 80 and games >= 8 else ("media" if hit >= 70 else "alta"),
                "entity": player.get("name"),
                "entity_id": player.get("id"),
                "entity_side": venue,
                "logo": photo,
                "team": team_name,
                "match": f"{game.get('home')} vs {game.get('away')}",
                "kickoff": game.get("kickoff"),
                "league": "NBA",
                "fixture_id": game.get("game_id"),
                "opponent": opponent,
                "venue_split_ok": venue_ok,
                "h2h_ok": h2h_ok,
                "h2h_games": len(h2h_vals),
                "injury_status": (injury or {}).get("status") if injury and not _status_is_out((injury or {}).get("status")) else None,
                "score": score,
                "recent_values": list(recent_vals),
                "note": " ".join(note_bits),
                # Odds only if a real book source is attached later.
                "avg_odd": None,
                "odds_verified": False,
                }
                if chosen is None or cand["score"] > chosen["score"]:
                    chosen = cand
            if chosen:
                # Stick with the closest qualifying line; do not shop farther ones.
                break
        if chosen:
            candidates.append(chosen)
    # One market per player at this stage (best score).
    if not candidates:
        return []
    best = max(candidates, key=lambda c: (c.get("score") or 0, c.get("hit_rate") or 0))
    return [best]


def collect_nba_prop_candidates(games: list[dict]) -> list[dict]:
    if not games:
        return []
    injuries = fetch_injuries()
    candidates: list[dict] = []
    seen_players = set()
    for game in games:
        sides = (
            ("home", game.get("home"), game.get("home_id"), game.get("away")),
            ("away", game.get("away"), game.get("away_id"), game.get("home")),
        )
        for venue, team_name, team_id, opponent in sides:
            for player in _roster_players(team_id):
                pid = str(player.get("id"))
                if pid in seen_players:
                    continue
                seen_players.add(pid)
                pname = player.get("name") or ""
                injury = (
                    injuries.get(pid)
                    or injuries.get(f"name:{pname.lower()}")
                    or injuries.get(f"name:{_norm_player_name(pname)}")
                )
                try:
                    candidates.extend(_build_player_candidates(player, game, venue, opponent or "", injury))
                except Exception:
                    continue
    return candidates


def _mix_all_picks(football_picks: list[dict], nba_picks: list[dict], limit: int = 5) -> list[dict]:
    """Keep football value picks first, then add clearly labeled NBA props."""
    fb = [p for p in football_picks or [] if p]
    nba = [p for p in nba_picks or [] if p]
    if not fb:
        return nba[:limit]
    if not nba:
        return fb[:limit]
    mixed = fb[:3] + nba[:2]
    if len(mixed) < limit:
        for p in fb[3:] + nba[2:]:
            mixed.append(p)
            if len(mixed) >= limit:
                break
    return mixed[:limit]


def _safe_url(url: Optional[str]) -> Optional[str]:
    if not url or not isinstance(url, str):
        return None
    text = url.strip()
    if not re.match(r"^https?://", text, re.I):
        return None
    try:
        host = (urlparse(text).hostname or "").lower()
    except Exception:
        return None
    if not host or "." not in host:
        return None
    if not any(host == d or host.endswith("." + d) for d in BOOK_DOMAINS):
        return None
    return text


def _parse_book_odd(raw) -> Optional[float]:
    if raw is None or raw == "":
        return None
    if isinstance(raw, str):
        text = raw.strip().replace(",", ".")
        if re.fullmatch(r"[+-]\d+", text):
            try:
                am = int(text)
            except ValueError:
                return None
            if am == 0:
                return None
            # American → decimal.
            dec = 1 + (100 / abs(am) if am < 0 else am / 100.0)
            return round(dec, 2) if 1.01 <= dec <= 50 else None
        raw = text
    try:
        val = float(raw)
    except (TypeError, ValueError):
        return None
    if 1.01 <= val <= 50:
        return round(val, 2)
    return None


def _extract_json_blob(text: str):
    if not text:
        return None
    raw = text.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()
    try:
        return json.loads(raw)
    except Exception:
        pass
    m = re.search(r"(\{.*\}|\[.*\])", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _response_text(resp) -> str:
    if resp is None:
        return ""
    if getattr(resp, "output_text", None):
        return str(resp.output_text)
    chunks = []
    for item in getattr(resp, "output", None) or []:
        content = getattr(item, "content", None) or []
        for part in content:
            text = getattr(part, "text", None)
            if text:
                chunks.append(str(text))
    return "\n".join(chunks).strip()


def _response_citations(resp) -> list[str]:
    urls = []
    if resp is None:
        return urls
    for item in getattr(resp, "citations", None) or []:
        url = item if isinstance(item, str) else (getattr(item, "url", None) or (item.get("url") if isinstance(item, dict) else None))
        safe = _safe_url(url)
        if safe:
            urls.append(safe)
    output = getattr(resp, "output", None) or []
    for item in output:
        content = getattr(item, "content", None) or []
        for part in content:
            for ann in getattr(part, "annotations", None) or []:
                url = getattr(ann, "url", None) or (ann.get("url") if isinstance(ann, dict) else None)
                safe = _safe_url(url)
                if safe:
                    urls.append(safe)
    # unique, preserve order
    seen = set()
    out = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out



MARKET_SYNONYMS = {
    "PTS": {"PTS", "POINTS", "PUNTOS", "POINT", "PTS O/U"},
    "REB": {"REB", "REBOUNDS", "REBOTES", "REBOUND"},
    "AST": {"AST", "ASSISTS", "ASISTENCIAS", "ASSIST"},
    "FG3M": {
        "FG3M", "THREES", "THREES_MADE", "3PM", "3P", "3-PT", "3 PT",
        "TRIPLES", "TRES", "THREE POINTERS", "THREE-POINTERS", "MADE THREES",
        "3-POINTERS", "3 POINTERS", "TRES ANOTADOS",
    },
    "PRA": {"PRA", "PTS+REB+AST", "PUNTOS+REBOTES+ASISTENCIAS", "PTS + REB + AST"},
    "PR": {"PR", "PTS+REB", "PUNTOS+REBOTES", "PTS + REB"},
    "PA": {"PA", "PTS+AST", "PUNTOS+ASISTENCIAS", "PTS + AST"},
    "RA": {"RA", "REB+AST", "REBOTES+ASISTENCIAS", "REB + AST"},
}

_BAD_URL_TOKENS = (
    "first-basket", "first_basket", "first-scorer", "first_scorer",
    "first-field-goal", "anytime-td", "anytime_touchdown", "anytime-touchdown",
    "point-spread", "moneyline", "alt-spread",
)

_NBA_NICKS = {
    "hawks", "celtics", "nets", "hornets", "bulls", "cavaliers", "cavs",
    "mavericks", "mavs", "nuggets", "pistons", "warriors", "rockets",
    "pacers", "clippers", "lakers", "grizzlies", "heat", "bucks",
    "timberwolves", "wolves", "pelicans", "knicks", "thunder", "magic",
    "76ers", "sixers", "suns", "blazers", "trailblazers", "kings", "spurs",
    "raptors", "jazz", "wizards",
}

MAX_BOOK_LINE_GAP = 1.0


def _norm_market_token(text: str) -> str:
    return re.sub(r"[^A-Z0-9+]+", " ", (text or "").upper()).strip()


def _markets_match(claimed: str, pick_market: str, pick_label: str) -> bool:
    claimed_n = _norm_market_token(claimed)
    if not claimed_n:
        return False
    key = (pick_market or "").upper()
    aliases = set(MARKET_SYNONYMS.get(key, {key}))
    aliases.add(key)
    aliases.add(_norm_market_token(pick_label))
    if claimed_n in aliases:
        return True
    # Allow "Tyrese Maxey Points" style labels that contain the alias.
    return any(alias and alias in claimed_n for alias in aliases if len(alias) >= 3)


def _url_path_ok(url: str, pick: dict) -> bool:
    path = (urlparse(url).path or "").lower()
    query = (urlparse(url).query or "").lower()
    blob = path + " " + query
    if any(tok in blob for tok in _BAD_URL_TOKENS):
        return False
    match = str(pick.get("match") or "")
    parts = re.split(r"\s+vs\.?\s+", match, flags=re.I)
    allowed = set()
    for part in parts:
        s = re.sub(r"[^a-z0-9 ]+", " ", (part or "").lower()).strip()
        if "76ers" in s or "sixers" in s:
            allowed.update({"76ers", "sixers"})
        if "trail blazers" in s or "blazers" in s:
            allowed.add("blazers")
        if "timberwolves" in s:
            allowed.update({"timberwolves", "wolves"})
        last = s.split()[-1] if s.split() else ""
        if last:
            allowed.add(last)
    norm = re.sub(r"[^a-z0-9]+", " ", blob)
    found = {nick for nick in _NBA_NICKS if re.search(r"\b" + re.escape(nick) + r"\b", norm)}
    # If the URL names an NBA team that is not in this game, it is the wrong matchup.
    if found and allowed and (found - allowed):
        return False
    return True


def _source_name_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host


def enrich_props_with_grok_lines(picks: list[dict]) -> tuple[list[dict], dict]:
    """Optional one-shot Grok web search for real sportsbook lines.

    Returns (picks, meta). Never invents a line or an odd. If search fails
    or a row has no source URL, the pick keeps only the estimated line.
    """
    meta = {"attempted": False, "ok": False, "reason": "no_picks", "sources": []}
    if not picks:
        return picks, meta
    key = os.getenv("XAI_API_KEY")
    if not key:
        meta["reason"] = "missing_xai_key"
        return picks, meta
    compact = []
    for i, p in enumerate(picks):
        compact.append({
            "idx": i,
            "player": p.get("entity"),
            "match": p.get("match"),
            "market": p.get("market"),
            "market_label": p.get("market_label"),
            "selection": p.get("selection"),
            "estimated_line": p.get("line_estimated"),
            "kickoff": p.get("kickoff"),
        })
    prompt = (
        "Busca en la web las líneas actuales de props NBA (puntos, rebotes, "
        "asistencias, triples, PRA, PR, PA, RA) para estos jugadores y partidos. "
        "Hoy es " + datetime.now(BOGOTA).strftime("%Y-%m-%d") + " (hora Colombia). "
        "Devuelve SOLO un JSON array, sin markdown, con objetos: "
        "idx, player, market, line (número .5), odd_over, odd_under, source "
        "(nombre de la casa o agregador), source_url (https). "
        "Incluye una fila solo si encontraste una línea publicada con URL de fuente. "
        "No inventes líneas ni cuotas. Si no hay dato, omite esa fila.\n\n"
        + json.dumps(compact, ensure_ascii=False)
    )
    meta["attempted"] = True
    try:
        client = OpenAI(api_key=key, base_url="https://api.x.ai/v1")
        resp = client.responses.create(
            model="grok-4.7",
            input=[{"role": "user", "content": prompt}],
            tools=[{"type": "web_search", "filters": {"allowed_domains": BOOK_DOMAINS}}],
            temperature=0.1,
        )
    except Exception as exc:
        meta["reason"] = f"search_error:{type(exc).__name__}"
        return picks, meta

    text = _response_text(resp)
    citations = _response_citations(resp)
    meta["sources"] = citations
    blob = _extract_json_blob(text)
    rows = blob if isinstance(blob, list) else (blob.get("picks") or blob.get("lines") or []) if isinstance(blob, dict) else []
    if not rows:
        meta["reason"] = "no_structured_lines"
        return picks, meta

    applied = 0
    rejected = 0
    for row in rows:
        if not isinstance(row, dict):
            continue
        try:
            idx = int(row.get("idx"))
        except (TypeError, ValueError):
            idx = None
        if idx is None or idx < 0 or idx >= len(picks):
            continue
        pick = picks[idx]
        player = str(row.get("player") or "").strip().lower()
        entity = str(pick.get("entity") or "").lower()
        if player and player not in entity and entity not in player:
            continue
        claimed_market = str(row.get("market") or row.get("market_label") or "").strip()
        if not _markets_match(claimed_market or pick.get("market"), pick.get("market"), pick.get("market_label") or ""):
            rejected += 1
            continue
        try:
            book_line = float(str(row.get("line")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        book_line = nba.snap_half(book_line, pick.get("line_estimated") or 0.5)
        if book_line < 0.5 or book_line > 80:
            continue
        source_url = _safe_url(row.get("source_url") or row.get("url"))
        if not source_url:
            for cit in citations:
                safe = _safe_url(cit)
                if safe and _url_path_ok(safe, pick):
                    source_url = safe
                    break
        if not source_url or not _url_path_ok(source_url, pick):
            rejected += 1
            continue
        estimated = pick.get("line_estimated") if pick.get("line_estimated") is not None else pick.get("line")
        try:
            gap = abs(float(book_line) - float(estimated))
        except (TypeError, ValueError):
            gap = 99
        # A verified book line more than 1.0 away is a different bet (or a bad scrape).
        if gap > MAX_BOOK_LINE_GAP + 0.01:
            pick["book_conflict"] = True
            pick["book_conflict_reason"] = "line_gap"
            rejected += 1
            continue
        # Recompute the hit at the published book line. If it is no longer
        # a 70%+ lean, do not feature the estimated (softer) line as a pick.
        recent = pick.get("recent_values") or []
        if recent and gap > 0.01:
            hits_b, games_b, hit_b = _hit(recent, book_line, pick.get("selection") or "Under")
            pick["hit_rate_at_book"] = hit_b
            pick["hits_at_book"] = hits_b
            pick["games_at_book"] = games_b
            if games_b < 8 or hit_b < 70:
                pick["book_conflict"] = True
                pick["book_conflict_reason"] = "hit_at_book"
                rejected += 1
                continue
        odd_over = _parse_book_odd(row.get("odd_over") or row.get("over_odd"))
        odd_under = _parse_book_odd(row.get("odd_under") or row.get("under_odd"))
        side = pick.get("selection")
        real_odd = odd_over if side == "Over" else odd_under if side == "Under" else None
        pick["book_line"] = book_line
        pick["book_source"] = _source_name_from_url(source_url)
        pick["book_source_url"] = source_url
        pick["book_line_label"] = "Línea en casas (vía Grok)"
        # A single scraped odd is not a bookmaker consensus.
        if real_odd and real_odd >= 1.40:
            pick["avg_odd"] = real_odd
            pick["odd_min"] = real_odd
            pick["odd_max"] = real_odd
            pick["bookmakers"] = 1
            pick["odds_verified"] = False
            pick["odds_source"] = "grok_web"
        applied += 1
    meta["ok"] = applied > 0
    meta["reason"] = "ok" if applied else "no_verified_source"
    meta["applied"] = applied
    meta["rejected"] = rejected
    return picks, meta


def attach_nba_reasons(picks: list[dict]) -> None:
    """Fill a Spanish statistical reason when Grok prose is not used yet."""
    for pick in picks:
        if pick.get("reason"):
            continue
        pick["reason"] = statistical_reason(pick)


def statistical_reason(pick: dict) -> str:
    line = pick.get("line")
    side = pick.get("selection") or ""
    text = (
        f"{pick.get('entity')}: en los últimos {pick.get('games')} partidos, "
        f"{pick.get('market_label')} {side} {line} acertó el {pick.get('hit_rate')}% "
        f"({pick.get('hits')}/{pick.get('games')})."
    )
    bits = []
    if pick.get("avg") is not None:
        bits.append(f"promedio reciente {pick['avg']}")
    if pick.get("median") is not None:
        bits.append(f"mediana {pick['median']}")
    if pick.get("avg_season") is not None:
        bits.append(f"promedio de temporada {pick['avg_season']}")
    if pick.get("avg_minutes") is not None:
        bits.append(f"{pick['avg_minutes']} min de promedio")
    if bits:
        text += " " + ", ".join(bits).capitalize() + "."
    text += f" Línea estimada {pick.get('line_estimated', line)} (no es una cuota de casa)."
    if pick.get("hit_rate_season") is not None:
        text += f" En la muestra de temporada el acierto fue {pick['hit_rate_season']}%."
    if pick.get("book_line") is not None:
        src = pick.get("book_source") or "fuente web"
        text += f" Línea en casas (vía Grok): {pick['book_line']} ({src})."
        if pick.get("avg_odd") is not None:
            text += f" Cuota reportada {pick['avg_odd']}."
    return text
