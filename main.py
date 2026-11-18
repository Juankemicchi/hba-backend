from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from nba_module import analyze_prop
from football_module import (
    get_team_stats,
    get_h2h,
    get_teams,
    get_players,
    get_squad,
    get_leagues,
    get_standings,
    DEFAULT_LEAGUE,
)
from football_stats import (
    search_entities,
    analyze_team_props,
    analyze_player_props,
    MARKET_LABELS,
)
from pydantic import BaseModel
import os
import requests
from openai import OpenAI

app = FastAPI(title="HBA Backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

client = OpenAI(
    api_key=os.getenv("XAI_API_KEY"),
    base_url="https://api.x.ai/v1",
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
    team: str = None
    team_id: int = None
    league: int = None
    last_n: int = 10
    venue: str = "all"
    opponent: str = None
    opponent_id: int = None
    lines: dict = None
    season: int = None
    market: str = None
    include_ai: bool = True

class PlayerPropsRequest(BaseModel):
    player: str = None
    player_id: int = None
    team: str = None
    team_id: int = None
    league: int = None
    last_n: int = 10
    lines: dict = None
    season: int = None
    market: str = None
    include_ai: bool = True

@app.get("/")
def root():
    return {"message": "HBA Backend activo", "ai": bool(os.getenv("XAI_API_KEY"))}

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
        response = client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=300,
            temperature=0.75
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
        response = client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
            temperature=0.75
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
        response = client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=400,
            temperature=0.75
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

Detalle del mercado principal ({MARKET_LABELS.get(primary, primary)}):
{recent or 'sin serie'}

{rec_txt}

Cierra con estas dos líneas exactas al final:
RECOMENDACIÓN: OVER|UNDER|NEUTRAL <línea> <mercado>
CONFIANZA: <1-5>/5
Sé directo. Menciona consistencia, riesgo y si el promedio sostiene la línea."""

    try:
        response = client.chat.completions.create(
            model="grok-4",
            messages=[{"role": "user", "content": prompt}],
            max_tokens=350,
            temperature=0.6
        )
        return response.choices[0].message.content.strip()
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
