from fastapi import FastAPI, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
try:
    from fastapi.responses import ORJSONResponse
except Exception:  # pragma: no cover
    ORJSONResponse = None
from nba_module import (
    analyze_prop,
    search_nba,
    list_nba_teams,
    get_nba_roster,
    analyze_nba_player,
    NBA_LABELS,
)
from football_module import (
    get_team_stats,
    get_h2h,
    get_teams,
    get_players,
    get_squad,
    get_leagues,
    get_standings,
    usage_snapshot,
    set_request_mode,
    quota_guard_snapshot,
    DEFAULT_LEAGUE,
)
from football_stats import (
    search_entities,
    analyze_team_props,
    analyze_player_props,
    MARKET_LABELS,
)
from vip_access import (
    require_vip,
    require_admin,
    public_config,
    set_user_vip,
    supabase_admin_ready,
    payment_secret,
    apply_vip_from_payment,
)
from vip_picks import list_vip_competitions, get_daily_picks, start_picks_background, get_warmup_status
from vip_history import list_history
from app_notifications import app_notifications_state
from vip_slip import SlipRequest, analyze_slip
from vip_chat import ChatRequest, vip_chat
from pydantic import BaseModel
from typing import Optional, Union, Any, List
import os
import time
import json
import hashlib
import threading
import requests
from openai import OpenAI

app = FastAPI(title="HBA Backend", default_response_class=ORJSONResponse) if ORJSONResponse else FastAPI(title="HBA Backend")

app.add_middleware(GZipMiddleware, minimum_size=400)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def _quota_request_mode(request, call_next):
    path = request.url.path or ""
    if path.startswith("/api/football") or path.startswith("/football") or path == "/analyze":
        set_request_mode("user_stats")
    elif path.startswith("/api/vip/picks"):
        set_request_mode("user_picks")
    else:
        set_request_mode("background")
    try:
        return await call_next(request)
    finally:
        set_request_mode("background")


# Placeholder key keeps import working if XAI_API_KEY is unset (local tests).
# Calls are still guarded with os.getenv("XAI_API_KEY").
client = OpenAI(
    api_key=os.getenv("XAI_API_KEY") or "missing-xai-key",
    base_url="https://api.x.ai/v1",
)

GROK_FAST_MODEL = os.getenv("GROK_FAST_MODEL", "grok-4.3")


def _grok_complete(messages, max_tokens=280, temperature=0.7):
    """Fast non-reasoning completion for insight/chat-style text. Falls back to grok-4."""
    try:
        return client.chat.completions.create(
            model=GROK_FAST_MODEL,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
            extra_body={"reasoning_effort": "none"},
        )
    except Exception:
        return client.chat.completions.create(
            model="grok-4",
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )



BIGBALLS_KEY = os.getenv("BIGBALLS_API_KEY", "")
HEADERS = {"Authorization": f"Bearer {BIGBALLS_KEY}"}
BASE_URL = "https://api.bigballsdata.com/v1"

class PropRequest(BaseModel):
    player: str
    prop: str
    line: float
    last_n: int = 12

class FootballTeamRequest(BaseModel):
    team: str
    league: int = None
    season: int = None

class FootballH2HRequest(BaseModel):
    team1: str
    team2: str
    last: int = 10
    league: int = None
    season: int = None

class FootballSearchNote:
    pass

class TeamPropsRequest(BaseModel):
    team: Optional[str] = None
    team_id: Optional[int] = None
    league: Optional[int] = None
    last_n: int = 10
    venue: str = "all"
    opponent: Optional[str] = None
    opponent_id: Optional[int] = None
    lines: Optional[dict] = None
    season: Optional[int] = None
    market: Optional[str] = None
    include_ai: bool = True

class PlayerPropsRequest(BaseModel):
    player: Optional[str] = None
    player_id: Optional[int] = None
    team: Optional[str] = None
    team_id: Optional[int] = None
    league: Optional[int] = None
    last_n: int = 10
    lines: Optional[dict] = None
    season: Optional[int] = None
    market: Optional[str] = None
    include_ai: bool = True

@app.get("/")
def root():
    return {"message": "HBA Backend activo", "ai": bool(os.getenv("XAI_API_KEY"))}


@app.get("/health")
def health():
    warm = get_warmup_status()
    quota = None
    try:
        quota = usage_snapshot()
    except Exception:
        quota = None
    guard = None
    try:
        guard = quota_guard_snapshot()
    except Exception:
        guard = None
    return {
        "ok": True,
        "warmup_seconds": warm.get("seconds"),
        "warmup_at": warm.get("finished_at"),
        "warmup_started_at": warm.get("started_at"),
        "warmup_running": warm.get("running"),
        "football_quota": quota,
        "quota_guard": guard,
    }


@app.on_event("startup")
def _start_background_jobs():
    start_picks_background()

def get_upcoming_matchup(player_name: str):
    try:
        response = requests.get(
            f"{BASE_URL}/matches",
            headers=HEADERS,
            params={"sport": "basketball", "league": "nba", "status": "scheduled", "limit": 15}
        )
        if response.status_code != 200:
            return None
        matches = response.json().get("data", [])
        return matches[0] if matches else None
    except:
        return None

def generate_ai_analysis(player: str, prop: str, line: float, avg: float, hit_rate: float, games: int, matchup=None):
    hits = round((hit_rate / 100) * games)
    rival_info = ""
    if matchup:
        home = matchup.get("home", {}).get("name", "Local")
        away = matchup.get("away", {}).get("name", "Visitante")
        rival_info = f"\nPróximo partido: {away} vs {home}."

    prompt = f"""Eres un analista de apuestas deportivas profesional.
Habla de forma natural.

Datos del jugador:
- Jugador: {player}
- Prop: {prop}
- Línea: {line}
- Promedio reciente: {avg}
- Hit rate: {hit_rate}% ({hits} de {games} partidos)
{rival_info}

Genera un análisis de 4 a 6 líneas que incluya promedio vs línea, consistencia, riesgo y conclusión. Sé directo y profesional."""

    try:
        response = _grok_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=240,
            temperature=0.75,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error al generar análisis: {str(e)}"

def _fmt_matches(matches, limit=6):
    lines = []
    for m in (matches or [])[:limit]:
        date = (m.get("date") or "")[:10]
        score = f"{m.get('goals_home')}-{m.get('goals_away')}"
        if m.get("goals_home") is None:
            score = m.get("status") or "vs"
        lines.append(f"- {date} {m.get('home')} {score} {m.get('away')} ({m.get('league') or ''})")
    return "\n".join(lines) if lines else "- Sin partidos recientes"

def _fmt_standing(standing):
    if not standing:
        return "Sin datos de tabla"
    group = f" ({standing.get('group')})" if standing.get("group") else ""
    form = standing.get("form") or "-"
    return (
        f"Puesto {standing.get('rank')}{group}, {standing.get('points')} pts, "
        f"{standing.get('played')} PJ, {standing.get('win')}W-{standing.get('draw')}E-{standing.get('lose')}D, "
        f"goles {standing.get('goals_for')}-{standing.get('goals_against')}, racha {form}"
    )

def _fmt_injuries(injuries):
    if not injuries:
        return "Sin lesionados reportados"
    parts = []
    for inj in injuries[:8]:
        reason = inj.get("reason") or inj.get("type") or ""
        parts.append(f"{inj.get('name')}{f' ({reason})' if reason else ''}")
    extra = f" y {len(injuries) - 8} más" if len(injuries) > 8 else ""
    return ", ".join(parts) + extra

def _fmt_form(stats):
    if not stats:
        return "Sin estadísticas"
    form = stats.get("form") or "-"
    return (
        f"Forma {form}; {stats.get('played')} PJ, {stats.get('wins')}W-{stats.get('draws')}E-{stats.get('loses')}D, "
        f"goles {stats.get('goals_for')}-{stats.get('goals_against')}"
    )

def generate_football_team_analysis(ctx: dict):
    team = (ctx.get("team") or {}).get("name") or "Equipo"
    league = ctx.get("league") or {}
    prompt = f"""Eres un analista de apuestas deportivas profesional.
Habla de forma natural, en español, como un tipster que analiza fútbol.

Equipo: {team}
Liga: {league.get('name') or '-'} ({league.get('country') or ''})
Temporada: {ctx.get('season')}
Tabla: {_fmt_standing(ctx.get('standing'))}
Estadísticas: {_fmt_form(ctx.get('statistics'))}
Lesionados: {_fmt_injuries(ctx.get('injuries'))}
Últimos partidos:
{_fmt_matches(ctx.get('recent_matches'))}

Genera un análisis de 4 a 6 líneas sobre el momento del equipo, consistencia, bajas importantes, nivel de riesgo para apostar y una conclusión clara. Sé directo y profesional. No uses listas numeradas."""

    try:
        response = _grok_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=280,
            temperature=0.75,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error al generar análisis: {str(e)}"

def generate_football_h2h_analysis(h2h: dict, team1: str, team2: str):
    ctx1 = h2h.get("team1") or {}
    ctx2 = h2h.get("team2") or {}
    prompt = f"""Eres un analista de apuestas deportivas profesional.
Habla de forma natural, en español, como un tipster que analiza fútbol.

Cara a cara: {team1} vs {team2}

{(ctx1.get('team') or {}).get('name') or team1}:
- Liga: {((ctx1.get('league') or {}).get('name')) or '-'}
- Tabla: {_fmt_standing(ctx1.get('standing'))}
- Forma: {_fmt_form(ctx1.get('statistics'))}
- Lesionados: {_fmt_injuries(ctx1.get('injuries'))}
- Recientes:
{_fmt_matches(ctx1.get('recent_matches'))}

{(ctx2.get('team') or {}).get('name') or team2}:
- Liga: {((ctx2.get('league') or {}).get('name')) or '-'}
- Tabla: {_fmt_standing(ctx2.get('standing'))}
- Forma: {_fmt_form(ctx2.get('statistics'))}
- Lesionados: {_fmt_injuries(ctx2.get('injuries'))}
- Recientes:
{_fmt_matches(ctx2.get('recent_matches'))}

Historial H2H (más reciente primero):
{_fmt_matches(h2h.get('matches'), 8)}

Genera un análisis de 4 a 6 líneas que compare momento de ambos equipos, el historial, bajas y una conclusión clara para el mercado 1X2 o goles. Sé directo y profesional. No uses listas numeradas."""

    try:
        response = _grok_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.75,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error al generar análisis: {str(e)}"

@app.post("/analyze")
def analyze(request: PropRequest):
    try:
        df, stats = analyze_prop(
            player_name=request.player,
            prop=request.prop,
            line=request.line,
            last_n=request.last_n
        )
        
        matchup = get_upcoming_matchup(request.player)
        
        ai_analysis = generate_ai_analysis(
            player=stats["player"],
            prop=request.prop,
            line=request.line,
            avg=stats["avg"],
            hit_rate=stats["hit_rate"],
            games=stats["games"],
            matchup=matchup
        )
        
        games = df.to_dict(orient='records')
        
        for game in games:
            if 'opponent' not in game or not game['opponent']:
                matchup_str = game.get('MATCHUP', '')
                if isinstance(matchup_str, str) and matchup_str:
                    game['opponent'] = matchup_str.split()[-1]
                else:
                    game['opponent'] = "Rival"
        
        return {
            "stats": stats,
            "games": games,
            "ai_analysis": ai_analysis
        }
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/football/team")
def analyze_football_team(request: FootballTeamRequest):
    try:
        stats = get_team_stats(request.team, request.league, request.season)
        if not stats:
            raise HTTPException(status_code=404, detail="No se encontraron datos del equipo")
        ai_analysis = generate_football_team_analysis(stats)
        return {"team": request.team, "stats": stats, "ai_analysis": ai_analysis}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/football/h2h")
def football_h2h(request: FootballH2HRequest):
    try:
        h2h = get_h2h(request.team1, request.team2, request.last, request.league, request.season)
        if not h2h:
            raise HTTPException(status_code=404, detail="No se encontró historial")
        ai_analysis = generate_football_h2h_analysis(h2h, request.team1, request.team2)
        return {
            "team1": request.team1,
            "team2": request.team2,
            "matches": h2h.get("matches", []),
            "team1_data": h2h.get("team1"),
            "team2_data": h2h.get("team2"),
            "ai_analysis": ai_analysis
        }
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/football/leagues")
def football_leagues(country: str = None, search: str = None):
    try:
        return get_leagues(country=country, search=search)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/football/teams")
def football_teams(league: int = DEFAULT_LEAGUE, season: int = None):
    try:
        return get_teams(league, season)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/football/standings")
def football_standings(league: int = DEFAULT_LEAGUE, season: int = None):
    try:
        return get_standings(league, season)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/football/players")
def football_players(team: int, season: int = None, page: int = 1, league: int = None):
    try:
        return get_players(team, season, page, league)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/football/squad")
def football_squad(team: int):
    try:
        data = get_squad(team)
        if not data.get("players"):
            raise HTTPException(status_code=404, detail="No se encontró la plantilla")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


def _fmt_market(m):
    if not m or not m.get("games"):
        return "sin datos"
    if m.get("kind") == "yesno":
        side = "Sí" if (m.get("side") or m.get("line")) == "yes" else "No"
        return (
            f"{m.get('label')}: Sí en {m.get('yes_rate')}% ({m.get('hits') if (m.get('side') or m.get('line'))=='yes' else (m.get('games') or 0) - (m.get('hits') or 0)}/{m.get('games')}), "
            f"No en {m.get('no_rate')}%. Mercado elegido: {side}, hit rate {m.get('hit_rate')}% ({m.get('hits')}/{m.get('games')})"
        )
    return f"{m.get('label')}: promedio {m.get('avg')} vs línea {m.get('line')}, hit rate {m.get('hit_rate')}% ({m.get('hits')}/{m.get('games')})"


def _fmt_match_line(row, key):
    date = (row.get("date") or "")[:10]
    val = row.get(key)
    score = row.get("score") or ""
    return f"- {date} vs {row.get('opponent')} ({row.get('venue')}, {score}): {val}"


def generate_props_analysis(kind: str, name: str, ctx: dict, market_key: str = None):
    stats = ctx.get("stats") or {}
    primary = market_key or ctx.get("primary")
    rec = ctx.get("recommendation") or {}
    markets_txt = "\n".join(_fmt_market(m) for m in stats.values() if m and m.get("games"))
    series = (stats.get(primary) or {}).get("values") or []
    recent = "\n".join(_fmt_match_line(v, "value") for v in series[:8])
    rec_txt = f"Inclinación calculada: {rec.get('lean')} {rec.get('line')} {rec.get('label')} (confianza {rec.get('confidence')}/5)."
    extra = ""
    if kind == "team":
        record = ctx.get("record") or {}
        extra = f"Forma {ctx.get('form') or '-'} · {record.get('wins')}W-{record.get('draws')}E-{record.get('losses')}D en {record.get('played')} partidos."
        if ctx.get("h2h"):
            h2h_lines = []
            for m in (ctx.get("h2h") or [])[:5]:
                h2h_lines.append(f"- {(m.get('date') or '')[:10]} {m.get('home')} {m.get('goals_home')}-{m.get('goals_away')} {m.get('away')}")
            extra += "\nH2H reciente:\n" + "\n".join(h2h_lines)
    elif kind == "nba":
        team = (ctx.get("team") or {}).get("name") or ""
        extra = f"Equipo NBA: {team}. Partidos analizados: {ctx.get('played')}."
    else:
        team = (ctx.get("team") or {}).get("name") or ""
        extra = f"Equipo: {team}. Partidos con minutos: {ctx.get('played')}. Portero: {ctx.get('is_goalkeeper')}."

    prompt = f"""Eres un analista de apuestas deportivas profesional.
Habla en español, breve y basado solo en los datos. 4 a 6 líneas, sin listas numeradas.

{kind.upper()}: {name}
Temporada: {ctx.get('season')}  Últimos partidos: {ctx.get('last_n')}
{extra}

Mercados:
{markets_txt}

Detalle del mercado principal ({(stats.get(primary) or {}).get('label') or MARKET_LABELS.get(primary) or NBA_LABELS.get(primary, primary)}):
{recent or 'sin serie'}

{rec_txt}

Cierra con estas dos líneas exactas al final:
RECOMENDACIÓN: OVER|UNDER|SÍ|NO|NEUTRAL <línea> <mercado>
CONFIANZA: <1-5>/5
Sé directo. Menciona consistencia, riesgo y si el promedio sostiene la línea. Si el mercado es Ambos anotan, usa SÍ o NO (no over/under)."""

    try:
        response = _grok_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=280,
            temperature=0.6,
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        return f"Error al generar análisis: {str(e)}"


INSIGHT_TTL = 6 * 60 * 60
_insight_cache = {}
_insight_inflight = {}
_insight_mutex = threading.Lock()


class InsightRequest(BaseModel):
    kind: str
    name: Optional[str] = None
    entity_id: Optional[Union[str, int]] = None
    market: Optional[str] = None
    label: Optional[str] = None
    line: Optional[Union[str, float, int]] = None
    side: Optional[str] = None
    market_kind: Optional[str] = None
    avg: Optional[float] = None
    hit_rate: Optional[float] = None
    hits: Optional[int] = None
    games: Optional[int] = None
    yes_rate: Optional[float] = None
    no_rate: Optional[float] = None
    lean: Optional[str] = None
    confidence: Optional[int] = None
    values: Optional[List[Any]] = None
    last_n: Optional[int] = None
    venue: Optional[str] = None
    opponent: Optional[str] = None
    form: Optional[str] = None
    record: Optional[dict] = None
    team: Optional[str] = None
    season: Optional[Union[int, str]] = None
    is_goalkeeper: Optional[bool] = None


def _insight_cache_key(req: InsightRequest) -> str:
    side = req.side or (req.line if req.market_kind == "yesno" else None)
    payload = {
        "kind": (req.kind or "").lower(),
        "id": str(req.entity_id or ""),
        "name": (req.name or "").strip().lower(),
        "market": req.market,
        "line": req.line if req.market_kind != "yesno" else None,
        "side": side,
        "last_n": req.last_n,
        "venue": req.venue or "all",
        "opponent": (req.opponent or "").strip().lower(),
        "hit_rate": req.hit_rate,
        "avg": req.avg,
        "games": req.games,
        "hits": req.hits,
    }
    raw = json.dumps(payload, sort_keys=True, ensure_ascii=True, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()


def _insight_get(key: str):
    item = _insight_cache.get(key)
    if item and time.time() - item["ts"] < INSIGHT_TTL:
        return item["text"]
    return None


def _insight_set(key: str, text: str):
    _insight_cache[key] = {"ts": time.time(), "text": text}
    # prune expired / cap size
    now = time.time()
    if len(_insight_cache) > 200:
        expired = [k for k, v in _insight_cache.items() if now - v["ts"] >= INSIGHT_TTL]
        for k in expired:
            _insight_cache.pop(k, None)
        if len(_insight_cache) > 200:
            oldest = sorted(_insight_cache.items(), key=lambda kv: kv[1]["ts"])[:50]
            for k, _ in oldest:
                _insight_cache.pop(k, None)


def generate_market_insight(req: InsightRequest) -> str:
    is_yesno = (req.market_kind == "yesno") or (str(req.market or "").lower() == "btts")
    if is_yesno:
        side_raw = str(req.side or req.line or "yes").lower()
        side = "No" if side_raw in ("no", "n", "0") else "Sí"
        line_txt = side
        market_label = req.label or "Ambos anotan"
        stats_txt = (
            f"Mercado: {market_label} (Sí/No). Lado elegido: {side}. "
            f"Sí en {req.yes_rate}% · No en {req.no_rate}%. "
            f"Hit rate del lado {side}: {req.hit_rate}% ({req.hits}/{req.games})."
        )
        rec_line = f"{req.lean or 'NEUTRAL'} {side} {market_label}"
    else:
        line_txt = req.line
        market_label = req.label or req.market or "el mercado"
        stats_txt = (
            f"Mercado: {market_label}. Línea: {line_txt}. "
            f"Promedio: {req.avg}. Hit rate over: {req.hit_rate}% ({req.hits}/{req.games})."
        )
        rec_line = f"{req.lean or 'NEUTRAL'} {line_txt} {market_label}"

    recent_bits = []
    for row in (req.values or [])[:8]:
        if not isinstance(row, dict):
            continue
        date = str(row.get("date") or "")[:10]
        opp = row.get("opponent") or "Rival"
        if is_yesno:
            val = row.get("display") or ("Sí" if row.get("value") in (1, "1", True) else "No")
        else:
            val = row.get("value")
        mark = "HIT" if row.get("over") else "MISS"
        score = f" {row.get('score')}" if row.get("score") else ""
        recent_bits.append(f"- {date} vs {opp}{score}: {val} ({mark})")
    recent = "\n".join(recent_bits) if recent_bits else "- Sin serie reciente"

    extra = []
    if req.team:
        extra.append(f"Equipo: {req.team}.")
    if req.season:
        extra.append(f"Temporada: {req.season}.")
    if req.last_n:
        extra.append(f"Últimos {req.last_n} partidos.")
    if req.venue and req.venue != "all":
        extra.append(f"Filtro: {req.venue}.")
    if req.opponent:
        extra.append(f"Rival H2H: {req.opponent}.")
    if req.form:
        extra.append(f"Forma: {req.form}.")
    if req.record:
        rec = req.record
        extra.append(f"Récord: {rec.get('wins')}W-{rec.get('draws')}E-{rec.get('losses')}D.")
    kind_name = {"team": "EQUIPO", "player": "JUGADOR", "nba": "JUGADOR NBA"}.get((req.kind or "").lower(), (req.kind or "").upper())
    lean_pretty = {"SI": "SÍ", "YES": "SÍ"}.get((req.lean or "").upper(), req.lean or "NEUTRAL")

    prompt = f"""Eres un analista de apuestas deportivas profesional.
Habla en español, breve y basado solo en estos datos. 4 a 6 líneas, sin listas numeradas.

{kind_name}: {req.name or 'N/A'}
{' '.join(extra)}

{stats_txt}
Inclinación calculada: {lean_pretty} (confianza {req.confidence or '-'}/5).

Serie reciente del mercado {market_label}:
{recent}

IMPORTANTE: Analiza EXCLUSIVAMENTE el mercado "{market_label}" y la línea {line_txt}.
Menciona explícitamente "{market_label}" y la línea {line_txt} en el texto.
No cambies de mercado ni hables de otra estadística como si fuera la principal.

Cierra con estas dos líneas exactas al final:
RECOMENDACIÓN: {rec_line}
CONFIANZA: {req.confidence or 3}/5
Sé directo. Menciona consistencia, riesgo y si los datos sostienen esta línea."""

    if not os.getenv("XAI_API_KEY"):
        return f"No hay clave de IA configurada. Mercado {market_label}, línea {line_txt}."
    try:
        response = _grok_complete(
            [{"role": "user", "content": prompt}],
            max_tokens=280,
            temperature=0.6,
        )
        return (response.choices[0].message.content or "").strip()
    except Exception as e:
        return f"Error al generar análisis: {str(e)}"


@app.get("/api/football/search")
def football_search(q: str = "", query: str = ""):
    try:
        term = (q or query or "").strip()
        return search_entities(term)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/football/analyze/team")
def football_analyze_team(request: TeamPropsRequest):
    try:
        if not request.team and not request.team_id:
            raise HTTPException(status_code=422, detail="Indica un equipo")
        data = analyze_team_props(
            team=request.team,
            team_id=request.team_id,
            league=request.league,
            last_n=request.last_n,
            venue=request.venue,
            opponent=request.opponent,
            opponent_id=request.opponent_id,
            lines=request.lines,
            season=request.season,
            market=request.market,
        )
        if not data:
            raise HTTPException(status_code=404, detail="No se encontraron datos del equipo")
        name = (data.get("team") or {}).get("name") or request.team
        if request.include_ai:
            data["ai_analysis"] = generate_props_analysis("team", name, data, request.market)
        else:
            data["ai_analysis"] = None
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/football/analyze/player")
def football_analyze_player(request: PlayerPropsRequest):
    try:
        if not request.player and not request.player_id:
            raise HTTPException(status_code=422, detail="Indica un jugador")
        data = analyze_player_props(
            player=request.player,
            player_id=request.player_id,
            team=request.team,
            team_id=request.team_id,
            league=request.league,
            last_n=request.last_n,
            lines=request.lines,
            season=request.season,
            market=request.market,
        )
        if not data:
            raise HTTPException(status_code=404, detail="No se encontraron datos del jugador")
        name = (data.get("player") or {}).get("fullname") or (data.get("player") or {}).get("name") or request.player
        if request.include_ai and data.get("matches"):
            data["ai_analysis"] = generate_props_analysis("player", name, data, request.market)
        elif not data.get("matches"):
            data["ai_analysis"] = data.get("error") or "No hay partidos recientes con minutos para este jugador."
        else:
            data["ai_analysis"] = None
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

class NbaPlayerPropsRequest(BaseModel):
    player: Optional[str] = None
    player_id: Optional[str] = None
    last_n: int = 10
    lines: Optional[dict] = None
    market: Optional[str] = None
    include_ai: bool = True


@app.get("/api/nba/search")
def nba_search(q: str = "", query: str = ""):
    try:
        return search_nba((q or query or "").strip())
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/nba/teams")
def nba_teams():
    try:
        return {"teams": list_nba_teams()}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/nba/roster")
def nba_roster(team_id: str = "", team: str = ""):
    try:
        tid = (team_id or team or "").strip()
        if not tid:
            raise HTTPException(status_code=422, detail="Indica un equipo")
        data = get_nba_roster(tid)
        if not data.get("players"):
            raise HTTPException(status_code=404, detail="No se encontró la plantilla")
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/nba/analyze/player")
def nba_analyze_player(request: NbaPlayerPropsRequest):
    try:
        if not request.player and not request.player_id:
            raise HTTPException(status_code=422, detail="Indica un jugador")
        data = analyze_nba_player(
            player=request.player,
            player_id=request.player_id,
            last_n=request.last_n,
            lines=request.lines,
            market=request.market,
        )
        name = (data.get("player") or {}).get("name") or request.player
        if request.include_ai and data.get("matches"):
            data["ai_analysis"] = generate_props_analysis("nba", name, data, request.market)
        else:
            data["ai_analysis"] = None
        return data
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.post("/api/insight")
def market_insight(request: InsightRequest):
    kind = (request.kind or "").lower()
    if kind not in ("team", "player", "nba"):
        raise HTTPException(status_code=422, detail="kind debe ser team, player o nba")
    if not request.market and not request.label:
        raise HTTPException(status_code=422, detail="Indica el mercado a analizar")
    key = _insight_cache_key(request)
    cached = _insight_get(key)
    if cached:
        return {"ai_analysis": cached, "cached": True}

    with _insight_mutex:
        ev = _insight_inflight.get(key)
        if ev is None:
            ev = threading.Event()
            _insight_inflight[key] = ev
            owner = True
        else:
            owner = False

    if not owner:
        ev.wait(timeout=45)
        cached = _insight_get(key)
        if cached:
            return {"ai_analysis": cached, "cached": True}

    try:
        text = generate_market_insight(request)
        _insight_set(key, text)
        return {"ai_analysis": text, "cached": False}
    finally:
        if owner:
            ev.set()
            with _insight_mutex:
                _insight_inflight.pop(key, None)


class AdminVipRequest(BaseModel):
    email: Optional[str] = None
    user_id: Optional[str] = None
    is_vip: bool = True


@app.get("/api/app/notifications")
def app_notifications():
    """Public, quota-free state for the Android app's notification poller."""
    try:
        return app_notifications_state()
    except Exception:
        return {"ok": False, "date": None, "slate_id": None, "picks_count": 0,
                "max_confidence_count": 0, "history_latest_date": None,
                "history_graded_count": 0, "history_won": 0, "history_lost": 0}


@app.get("/api/vip/config")
def vip_config():
    return public_config()


@app.get("/api/vip/me")
def vip_me(user=Depends(require_vip)):
    return {"ok": True, "source": user.source, "is_vip": True, "email": user.email}


@app.get("/api/vip/competitions")
def vip_competitions(_user=Depends(require_vip)):
    return list_vip_competitions()


@app.get("/api/vip/picks")
def vip_picks(sport: str = "all", league: Optional[int] = None, _user=Depends(require_vip)):
    try:
        return get_daily_picks(sport=sport, league_id=league)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/vip/history")
def vip_pick_history(_user=Depends(require_vip)):
    try:
        return list_history()
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/vip/slip")
def vip_analyze_slip(request: SlipRequest, _user=Depends(require_vip)):
    try:
        return analyze_slip(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/vip/chat")
def vip_chat_route(request: ChatRequest, _user=Depends(require_vip)):
    try:
        return vip_chat(request)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/vip")
def admin_set_vip(request: AdminVipRequest, _admin=Depends(require_admin)):
    if not supabase_admin_ready():
        raise HTTPException(status_code=503, detail="Supabase no está configurado")
    return set_user_vip(email=request.email, user_id=request.user_id, is_vip=request.is_vip)


@app.post("/api/vip/webhooks/mercadopago")
async def vip_webhook_mercadopago(request: Request):
    if not payment_secret("mercadopago"):
        return {"ok": False, "detail": "Pasarela Mercado Pago no configurada"}
    # Hook: verify signature with MERCADOPAGO_WEBHOOK_SECRET, map payer to profiles, set is_vip.
    _ = await request.body()
    return {"ok": False, "detail": "Webhook recibido; el procesamiento se activará cuando la pasarela esté conectada"}


@app.post("/api/vip/webhooks/wompi")
async def vip_webhook_wompi(request: Request):
    if not payment_secret("wompi"):
        return {"ok": False, "detail": "Pasarela Wompi no configurada"}
    _ = await request.body()
    return {"ok": False, "detail": "Webhook recibido; el procesamiento se activará cuando la pasarela esté conectada"}
