"""Chat VIP: Grok with backend tool steps that fetch real HBA stats."""

from __future__ import annotations

import json
import os
from typing import Any, List, Optional

from pydantic import BaseModel
from openai import OpenAI

import football_module as fm
import football_stats as fs
import nba_module as nba
from vip_picks import markets_from_analysis

MAX_MESSAGES = 12
MAX_TOOL_ROUNDS = 3

SYSTEM = (
    "Eres el asistente VIP de HBA Sports Intelligence. Responde en español (Colombia). "
    "Antes de afirmar estadísticas, usa las herramientas para obtener datos reales de HBA. "
    "Nunca inventes hit rates, promedios, alineaciones, cuotas ni resultados. "
    "Si una herramienta falla o no hay muestra suficiente, dilo con claridad. "
    "Cuando hables de un mercado, incluye hit rate, hits/juegos y la línea usada. "
    "Esto es análisis estadístico, no un consejo de apuesta. Recuerda el juego responsable (18+)."
)

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "search_entities",
            "description": "Busca equipos o jugadores de fútbol y NBA por nombre.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "sport": {"type": "string", "enum": ["football", "nba", "all"]},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_team",
            "description": "Estadísticas y hit rates de un equipo de fútbol (mismos mercados del dashboard HBA).",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "integer"},
                    "team": {"type": "string"},
                    "league": {"type": "integer"},
                    "last_n": {"type": "integer"},
                    "venue": {"type": "string", "enum": ["all", "home", "away"]},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_player",
            "description": "Props de un jugador de fútbol (remates, goles, asistencias, atajadas, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "integer"},
                    "player": {"type": "string"},
                    "last_n": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "analyze_nba_player",
            "description": "Props NBA de un jugador (puntos, rebotes, asistencias, etc.).",
            "parameters": {
                "type": "object",
                "properties": {
                    "player_id": {"type": "string"},
                    "player": {"type": "string"},
                    "last_n": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_standings",
            "description": "Tabla de posiciones de una liga de fútbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "league": {"type": "integer"},
                    "season": {"type": "integer"},
                },
                "required": ["league"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fixtures",
            "description": "Partidos recientes o próximos de un equipo de fútbol.",
            "parameters": {
                "type": "object",
                "properties": {
                    "team_id": {"type": "integer"},
                    "last": {"type": "integer"},
                },
                "required": ["team_id"],
            },
        },
    },
]


class ChatMessage(BaseModel):
    role: str
    content: Optional[str] = None


class ChatRequest(BaseModel):
    messages: List[ChatMessage]


def _client() -> OpenAI:
    key = os.getenv("XAI_API_KEY")
    if not key:
        raise RuntimeError("XAI_API_KEY no está configurada en este entorno.")
    return OpenAI(api_key=key, base_url="https://api.x.ai/v1")


def _compact_markets(analysis: dict) -> list[dict]:
    out = []
    for row in markets_from_analysis(analysis):
        rec = row.get("recommend") or {}
        out.append({
            "key": row.get("key"),
            "label": row.get("label"),
            "line": row.get("line"),
            "avg": row.get("avg"),
            "hit_rate": row.get("hit_rate"),
            "yes_rate": row.get("yes_rate"),
            "no_rate": row.get("no_rate"),
            "hits": row.get("hits"),
            "games": row.get("games"),
            "lean": rec.get("lean") or rec.get("side"),
        })
    return out


def _run_tool(name: str, arguments: dict) -> Any:
    args = arguments or {}
    last_n = int(args.get("last_n") or args.get("last") or 10)
    last_n = max(5, min(last_n, 15))
    if name == "search_entities":
        query = args.get("query") or ""
        sport = (args.get("sport") or "all").lower()
        out = {}
        if sport in {"all", "football", "futbol", "fútbol"}:
            try:
                out["football"] = fs.search_entities(query)
            except Exception as exc:
                out["football_error"] = str(exc)
        if sport in {"all", "nba"}:
            try:
                out["nba"] = nba.search_nba(query)
            except Exception as exc:
                out["nba_error"] = str(exc)
        return out
    if name == "analyze_team":
        data = fs.analyze_team_props(
            team=args.get("team"),
            team_id=args.get("team_id"),
            league=args.get("league"),
            last_n=last_n,
            venue=args.get("venue") or "all",
        )
        return {
            "team": data.get("team"),
            "form": data.get("form"),
            "record": data.get("record"),
            "markets": _compact_markets(data),
        }
    if name == "analyze_player":
        data = fs.analyze_player_props(
            player=args.get("player"),
            player_id=args.get("player_id"),
            last_n=last_n,
        )
        return {
            "player": data.get("player") or data.get("name"),
            "team": data.get("team"),
            "markets": _compact_markets(data),
        }
    if name == "analyze_nba_player":
        pid = args.get("player_id")
        player = args.get("player")
        try:
            data = nba.analyze_nba_player(player_id=pid, last_n=last_n) if pid else None
        except TypeError:
            data = None
        if data is None:
            try:
                data = nba.analyze_nba_player(player or pid, last_n=last_n)
            except TypeError:
                data = nba.analyze_nba_player(player or pid)
        return {
            "player": data.get("player") or data.get("name") or player,
            "team": data.get("team"),
            "markets": _compact_markets(data) or data.get("markets"),
            "form": data.get("form"),
        }
    if name == "get_standings":
        data = fm.get_standings(int(args["league"]), args.get("season"))
        # compact first 10 rows of first group
        groups = data if isinstance(data, list) else (data.get("groups") or data.get("standings") or [data])
        compact = []
        for group in groups[:2] if isinstance(groups, list) else []:
            rows = group.get("rows") or group.get("table") or group if isinstance(group, dict) else group
            if isinstance(rows, list):
                for row in rows[:12]:
                    compact.append({
                        "rank": row.get("rank"),
                        "team": row.get("team"),
                        "team_id": row.get("team_id"),
                        "played": (row.get("played") or (row.get("all") or {}).get("played")),
                        "points": row.get("points"),
                        "goal_diff": row.get("goal_diff") or row.get("goalsDiff"),
                    })
        return {"standings": compact, "raw_keys": list(data.keys()) if isinstance(data, dict) else None}
    if name == "get_fixtures":
        rows = fm.get_recent_fixtures(int(args["team_id"]), last=last_n)
        return {"fixtures": rows[:12]}
    return {"error": f"Herramienta desconocida: {name}"}


def _safe_tool(name: str, arguments: dict) -> str:
    try:
        result = _run_tool(name, arguments)
        text = json.dumps(result, ensure_ascii=False, default=str)
        if len(text) > 8000:
            text = text[:8000] + "…"
        return text
    except Exception as exc:
        return json.dumps({"error": str(exc)}, ensure_ascii=False)


def vip_chat(req: ChatRequest) -> dict:
    if not os.getenv("XAI_API_KEY"):
        return {
            "ok": False,
            "error": "El chat VIP necesita XAI_API_KEY en el servidor. Configúrala en Render para activar Grok.",
            "message": None,
        }
    history = []
    for msg in (req.messages or [])[-MAX_MESSAGES:]:
        role = (msg.role or "").lower()
        if role not in {"user", "assistant"}:
            continue
        content = (msg.content or "").strip()
        if not content:
            continue
        history.append({"role": role, "content": content})
    if not history or history[-1]["role"] != "user":
        return {"ok": False, "error": "Envía un mensaje de usuario.", "message": None}

    messages = [{"role": "system", "content": SYSTEM}] + history
    client = _client()
    used_tools = []
    try:
        for _round in range(MAX_TOOL_ROUNDS + 1):
            resp = client.chat.completions.create(
                model="grok-4",
                messages=messages,
                tools=TOOLS,
                tool_choice="auto",
                temperature=0.3,
            )
            choice = resp.choices[0]
            msg = choice.message
            tool_calls = getattr(msg, "tool_calls", None) or []
            if not tool_calls:
                text = (msg.content or "").strip()
                return {
                    "ok": True,
                    "message": text,
                    "used_tools": used_tools,
                    "disclaimer": "Análisis estadístico, no es un consejo de apuesta. 18+. Juega con responsabilidad.",
                }
            messages.append({
                "role": "assistant",
                "content": msg.content or "",
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function.name,
                            "arguments": tc.function.arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            })
            for tc in tool_calls:
                name = tc.function.name
                try:
                    args = json.loads(tc.function.arguments or "{}")
                except Exception:
                    args = {}
                used_tools.append(name)
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": _safe_tool(name, args),
                })
        return {
            "ok": False,
            "error": "El asistente encadenó demasiadas consultas. Intenta una pregunta más concreta.",
            "message": None,
            "used_tools": used_tools,
        }
    except Exception as exc:
        return {"ok": False, "error": f"No se pudo completar el chat: {exc}", "message": None, "used_tools": used_tools}
