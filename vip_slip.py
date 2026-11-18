"""Analizar mi apuesta: parse text or a slip screenshot, map to HBA entities, compute verdicts."""

from __future__ import annotations

import json
import os
import re
from typing import Optional

import requests
from pydantic import BaseModel

import football_stats as fs
import nba_module as nba
from vip_picks import markets_from_analysis


class SlipRequest(BaseModel):
    text: Optional[str] = None
    image_base64: Optional[str] = None
    image_mime: Optional[str] = "image/jpeg"


EXTRACT_PROMPT = """Extrae las selecciones de esta apuesta (boleta, ticket o texto).
Devuelve SOLO un JSON array. Cada objeto:
{
  "raw": "texto original de la selección",
  "sport": "football" o "nba" o null,
  "query": "nombre del equipo o jugador",
  "opponent": "rival si aparece",
  "market": "clave corta: moneyline, 1x2, btts, totals, corners, corners_total, corners_for, goals, goals_for, shots, sot, cards, yellow, pts, reb, ast, pra, fg3m, stl, blk, min, player_prop",
  "line": número o null,
  "side": "over" | "under" | "yes" | "no" | "home" | "away" | "draw" | "win" | null,
  "selection": "texto de la pick (Over 2.5, Sí, Millonarios, etc.)"
}
Si no estás seguro, incluye raw y query igual. No inventes mercados que no estén en el ticket.
"""


def _xai_key() -> Optional[str]:
    return os.getenv("XAI_API_KEY") or None


def _data_url(image_base64: str, mime: str) -> str:
    raw = (image_base64 or "").strip()
    if raw.startswith("data:"):
        return raw
    mime = mime or "image/jpeg"
    return f"data:{mime};base64,{raw}"


def _parse_json_array(text: str) -> list:
    if not text:
        return []
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].strip()
    try:
        data = json.loads(text)
        if isinstance(data, dict) and "selections" in data:
            data = data["selections"]
        return data if isinstance(data, list) else []
    except Exception:
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            try:
                data = json.loads(text[start:end + 1])
                return data if isinstance(data, list) else []
            except Exception:
                return []
    return []


def vision_extract(image_base64: str, mime: str = "image/jpeg") -> list[dict]:
    key = _xai_key()
    if not key:
        raise RuntimeError("La lectura de capturas no está disponible en este momento.")
    url = "https://api.x.ai/v1/responses"
    payload = {
        "model": "grok-4.7",
        "input": [
            {
                "role": "user",
                "content": [
                    {"type": "input_image", "image_url": _data_url(image_base64, mime), "detail": "high"},
                    {"type": "input_text", "text": EXTRACT_PROMPT},
                ],
            }
        ],
    }
    resp = requests.post(
        url,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json=payload,
        timeout=90,
    )
    if resp.status_code >= 400:
        raise RuntimeError("No se pudo leer la captura. Inténtalo de nuevo.")
    body = resp.json()
    text = ""
    if isinstance(body.get("output_text"), str):
        text = body["output_text"]
    else:
        for item in body.get("output") or []:
            for content in item.get("content") or []:
                if content.get("type") in {"output_text", "text"} and content.get("text"):
                    text += content["text"]
            if item.get("type") == "message":
                pass
    return _parse_json_array(text)


def grok_extract_text(text: str) -> list[dict]:
    key = _xai_key()
    if not key:
        return []
    try:
        resp = requests.post(
            "https://api.x.ai/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
            json={
                "model": "grok-4",
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": EXTRACT_PROMPT},
                    {"role": "user", "content": text},
                ],
            },
            timeout=60,
        )
        if resp.status_code >= 400:
            return []
        content = (((resp.json().get("choices") or [{}])[0].get("message") or {}).get("content")) or ""
        return _parse_json_array(content)
    except Exception:
        return []


_MARKET_HINTS = [
    (r"ambos\s*anotan|btts|both teams? to score", "btts"),
    (r"corners?|c[oó]rneres|tiros de esquina", "corners_total"),
    (r"tarjetas|cards|amarillas", "cards"),
    (r"remates a puerta|shots on target|sot", "sot"),
    (r"remates|tiros|shots", "shots"),
    (r"goles|goals|total goles", "goals_for"),
    (r"puntos|points|\bpts\b", "PTS"),
    (r"rebotes|rebounds|\breb\b", "REB"),
    (r"asistencias|assists|\bast\b", "AST"),
    (r"\bpra\b", "PRA"),
    (r"triples|threes|3pt|fg3m", "FG3M"),
    (r"1x2|ganador|moneyline|ml\b|ganará|gana\b", "1x2"),
    (r"over|under|m[aá]s de|menos de|total", "totals"),
]


def _heuristic_parse(text: str) -> list[dict]:
    selections = []
    for raw_line in re.split(r"[\n;]+", text or ""):
        line = raw_line.strip()
        if len(line) < 3:
            continue
        market = None
        for pat, key in _MARKET_HINTS:
            if re.search(pat, line, re.I):
                market = key
                break
        side = None
        low = line.lower()
        if re.search(r"\bno\b|under|menos de", low):
            side = "no" if market == "btts" else "under"
        elif re.search(r"\bs[ií]\b|over|m[aá]s de", low):
            side = "yes" if market == "btts" else "over"
        num = re.search(r"(\d+(?:[.,]\d+)?)", line)
        line_val = None
        if num and market not in {"1x2", "btts"}:
            try:
                line_val = float(num.group(1).replace(",", "."))
            except ValueError:
                line_val = None
        query = re.split(r"\s+(?:vs|v\.|over|under|más de|menos de|ambos|btts|-)\s+", line, maxsplit=1, flags=re.I)[0]
        query = re.sub(r"\d+(?:[.,]\d+)?", "", query)
        query = re.sub(r"\b(over|under|s[ií]|no|ganará|gana|moneyline|ml|1x2)\b", "", query, flags=re.I)
        query = re.sub(r"\s+", " ", query).strip(" -:")
        if not query:
            continue
        sport = "nba" if market in {"PTS", "REB", "AST", "PRA", "FG3M"} or re.search(r"nba|lakers|celtics|doncic|tatum", line, re.I) else "football"
        selections.append({
            "raw": line,
            "sport": sport,
            "query": query,
            "market": market,
            "line": line_val,
            "side": side,
            "selection": line,
        })
    return selections


def _search_football(query: str) -> Optional[dict]:
    try:
        result = fs.search_entities(query)
    except Exception:
        return None
    teams = result.get("teams") or []
    players = result.get("players") or []
    if teams:
        team = teams[0]
        return {"kind": "team", "sport": "football", "id": team.get("id"), "name": team.get("name"), "logo": team.get("logo"), "league_id": team.get("league_id") or team.get("league")}
    if players:
        player = players[0]
        return {"kind": "player", "sport": "football", "id": player.get("id"), "name": player.get("name"), "photo": player.get("photo"), "team": player.get("team")}
    return None


def _search_nba(query: str) -> Optional[dict]:
    try:
        result = nba.search_nba(query)
    except Exception:
        return None
    players = result.get("players") or (result if isinstance(result, list) else [])
    if isinstance(result, dict):
        players = result.get("players") or result.get("data") or []
        teams = result.get("teams") or []
        if teams and not players:
            team = teams[0]
            return {"kind": "team", "sport": "nba", "id": team.get("id"), "name": team.get("name") or team.get("full_name")}
    if players:
        player = players[0]
        return {
            "kind": "nba",
            "sport": "nba",
            "id": player.get("id"),
            "name": player.get("name") or player.get("full_name"),
            "team": player.get("team_name") or player.get("team"),
        }
    return None


def _resolve_entity(sel: dict) -> Optional[dict]:
    query = (sel.get("query") or sel.get("selection") or sel.get("raw") or "").strip()
    if not query:
        return None
    sport = (sel.get("sport") or "").lower()
    if sport == "nba":
        return _search_nba(query) or _search_football(query)
    found = _search_football(query)
    if found:
        return found
    return _search_nba(query)


def _analyze_entity(entity: dict, market: Optional[str], line, side: Optional[str]) -> Optional[dict]:
    kind = entity.get("kind")
    if kind == "team":
        try:
            return fs.analyze_team_props(team_id=int(entity["id"]), last_n=10, include_ai=False) if False else fs.analyze_team_props(team_id=int(entity["id"]), last_n=10)
        except Exception:
            return None
    if kind == "player":
        try:
            return fs.analyze_player_props(player_id=int(entity["id"]), last_n=10)
        except Exception:
            return None
    if kind == "nba":
        line_val = line if isinstance(line, (int, float)) else None
        prop = (market or "PTS").upper()
        if prop in {"TOTALS", "MONEYLINE", "1X2"}:
            prop = "PTS"
        try:
            kwargs = {"player_id": entity["id"], "last_n": 10}
            # analyze_nba_player signatures vary; try common ones
            try:
                return nba.analyze_nba_player(player_id=entity["id"], last_n=10)
            except TypeError:
                return nba.analyze_nba_player(entity.get("name") or entity["id"])
        except Exception:
            return None
    return None


def _pick_market_row(analysis: dict, market: Optional[str], line, side: Optional[str]) -> Optional[dict]:
    if not analysis:
        return None
    markets = markets_from_analysis(analysis)
    key_map = {
        "corners": "corners_total",
        "corners_total": "corners_total",
        "corners_for": "corners_for",
        "btts": "btts",
        "totals": "goals_for" if analysis.get("team") else "PTS",
        "goals": "goals_for",
        "goals_for": "goals_for",
        "shots": "shots",
        "sot": "shots_on_target",
        "shots_on_target": "shots_on_target",
        "cards": "cards",
        "yellow": "yellow",
        "pts": "PTS",
        "reb": "REB",
        "ast": "AST",
        "pra": "PRA",
        "fg3m": "FG3M",
        "stl": "STL",
        "blk": "BLK",
        "min": "MIN",
        "player_prop": None,
        "1x2": None,
        "moneyline": None,
    }
    want = key_map.get((market or "").lower()) if market else None
    if want:
        for row in markets:
            if row.get("key") == want or (row.get("key") or "").upper() == want:
                return row
    # fallback: recommended strongest market
    best = None
    best_score = -1
    for row in markets:
        rec = row.get("recommend") or {}
        hit = float(row.get("hit_rate") or row.get("yes_rate") or 0)
        games = int(row.get("games") or 0)
        score = hit if games >= 5 else 0
        if score > best_score:
            best, best_score = row, score
    return best


def _verdict_from_row(row: dict, side: Optional[str], line) -> dict:
    if not row:
        return {"verdict": "weak", "hit_rate": None, "games": 0, "hits": 0, "detail": "Sin estadísticas suficientes"}
    games = int(row.get("games") or 0)
    kind = row.get("kind") or "line"
    rec = row.get("recommend") or {}
    used_line = line if line is not None else row.get("line")
    if kind == "yesno":
        want = (side or rec.get("side") or "yes").lower()
        if want in {"sí", "si", "yes"}:
            want = "yes"
        if want in {"no"}:
            want = "no"
        hit = float(row.get("yes_rate") if want == "yes" else row.get("no_rate") or 0)
        selection = "Sí" if want == "yes" else "No"
        hits = int(round(hit / 100.0 * games)) if games else 0
        used_line = None
    else:
        over_hit = float(row.get("hit_rate") or 0)
        want = (side or rec.get("lean") or "over").lower()
        if want in {"under", "menos"}:
            hit = round(100.0 - over_hit, 1)
            selection = "Under"
            hits = max(games - int(row.get("hits") or 0), 0)
        else:
            hit = over_hit
            selection = "Over"
            hits = int(row.get("hits") or 0)
    if games < 5 or hit < 50:
        verdict = "weak"
    elif hit >= 65 and games >= 5:
        verdict = "strong"
    else:
        verdict = "mixed"
    return {
        "verdict": verdict,
        "hit_rate": round(hit, 1),
        "hits": hits,
        "games": games,
        "sample_size": games,
        "line": used_line,
        "selection": selection,
        "market": row.get("key"),
        "market_label": row.get("label") or row.get("key"),
        "avg": row.get("avg"),
        "lean": (row.get("recommend") or {}).get("lean"),
    }


def _form_moneyline(analysis: dict, entity_name: str) -> dict:
    rec = analysis.get("record") or {}
    form = analysis.get("form") or ""
    wins = rec.get("wins") if isinstance(rec, dict) else None
    games = rec.get("played") if isinstance(rec, dict) else None
    if wins is None:
        letters = [ch for ch in str(form).upper() if ch in "WDL"]
        wins = letters.count("W")
        games = len(letters)
    games = int(games or 0)
    wins = int(wins or 0)
    hit = round(100.0 * wins / games, 1) if games else 0
    verdict = "strong" if hit >= 65 and games >= 5 else ("weak" if hit < 50 or games < 5 else "mixed")
    return {
        "verdict": verdict,
        "hit_rate": hit,
        "hits": wins,
        "games": games,
        "sample_size": games,
        "line": None,
        "selection": entity_name,
        "market": "1x2",
        "market_label": "1X2 / ganador",
        "note": "Hit rate = % de victorias en la forma reciente de HBA.",
    }


def _suggest_swaps(analysis: dict, current: dict) -> list[dict]:
    if not analysis:
        return []
    swaps = []
    current_key = current.get("market")
    # If the requested side is weak, offer the opposite side of the same market first.
    for row in markets_from_analysis(analysis):
        if row.get("key") != current_key:
            continue
        games = int(row.get("games") or 0)
        if games < 5:
            break
        if row.get("kind") == "yesno":
            sel = current.get("selection") or ""
            if sel == "Sí":
                hit = float(row.get("no_rate") or 0)
                opp = "No"
            else:
                hit = float(row.get("yes_rate") or 0)
                opp = "Sí"
        else:
            over_hit = float(row.get("hit_rate") or 0)
            if (current.get("selection") or "").lower().startswith("over"):
                hit = round(100.0 - over_hit, 1)
                opp = f"Under {row.get('line')}"
            else:
                hit = over_hit
                opp = f"Over {row.get('line')}"
        if hit >= 65:
            swaps.append({
                "market": row.get("key"),
                "market_label": row.get("label") or row.get("key"),
                "selection": opp,
                "line": row.get("line"),
                "hit_rate": round(hit, 1),
                "games": games,
                "reason": f"El lado contrario del mismo mercado acertó el {hit}% ({games} PJ).",
            })
        break
    for row in markets_from_analysis(analysis):
        if row.get("key") == current_key:
            continue
        rec = row.get("recommend") or {}
        lean = rec.get("lean")
        if lean in (None, "NEUTRAL"):
            continue
        games = int(row.get("games") or 0)
        if row.get("kind") == "yesno":
            hit = float(row.get("yes_rate") if rec.get("side") == "yes" else row.get("no_rate") or 0)
            sel = "Sí" if rec.get("side") == "yes" else "No"
        else:
            over_hit = float(row.get("hit_rate") or 0)
            if lean == "UNDER":
                hit = round(100.0 - over_hit, 1)
                sel = f"Under {row.get('line')}"
            else:
                hit = over_hit
                sel = f"Over {row.get('line')}"
        if hit >= 65 and games >= 5:
            swaps.append({
                "market": row.get("key"),
                "market_label": row.get("label") or row.get("key"),
                "selection": sel,
                "line": row.get("line"),
                "hit_rate": round(hit, 1),
                "games": games,
                "reason": f"Mejor lectura estadística en la misma entidad: {hit}% ({games} PJ).",
            })
        if len(swaps) >= 2:
            break
    return swaps


def analyze_slip(req: SlipRequest) -> dict:
    selections = []
    source = "empty"
    errors = []
    if req.image_base64:
        try:
            selections = vision_extract(req.image_base64, req.image_mime or "image/jpeg")
            source = "image"
        except Exception as exc:
            errors.append(str(exc))
    text = (req.text or "").strip()
    if text and not selections:
        selections = grok_extract_text(text)
        source = "text_ai" if selections else source
    if text and not selections:
        selections = _heuristic_parse(text)
        source = "text"
    if not selections:
        return {
            "ok": False,
            "source": source,
            "error": errors[0] if errors else "No se pudieron leer selecciones. Escribe tus picks o sube una captura.",
            "picks": [],
            "parlay": None,
        }

    picks = []
    rates = []
    for sel in selections:
        entity = _resolve_entity(sel)
        item = {
            "raw": sel.get("raw") or sel.get("selection"),
            "query": sel.get("query"),
            "requested_market": sel.get("market"),
            "requested_line": sel.get("line"),
            "requested_side": sel.get("side"),
            "entity": entity,
            "mapped": bool(entity),
        }
        if not entity:
            item["verdict"] = "weak"
            item["detail"] = "No se encontró el equipo o jugador en HBA."
            item["swaps"] = []
            picks.append(item)
            continue
        analysis = _analyze_entity(entity, sel.get("market"), sel.get("line"), sel.get("side"))
        market_key = (sel.get("market") or "").lower()
        if analysis and market_key in {"1x2", "moneyline", "ml"} and entity.get("kind") == "team":
            verdict = _form_moneyline(analysis, entity.get("name"))
        else:
            row = _pick_market_row(analysis, sel.get("market"), sel.get("line"), sel.get("side"))
            verdict = _verdict_from_row(row, sel.get("side"), sel.get("line"))
        item.update(verdict)
        item["swaps"] = _suggest_swaps(analysis, verdict) if (verdict.get("verdict") != "strong") else []
        if verdict.get("hit_rate") is not None and verdict.get("games"):
            rates.append(float(verdict["hit_rate"]) / 100.0)
        picks.append(item)

    parlay = None
    usable = [p for p in picks if p.get("hit_rate") is not None and p.get("games")]
    if usable:
        product = 1.0
        for p in usable:
            product *= float(p["hit_rate"]) / 100.0
        parlay = {
            "legs": len(usable),
            "combined_hit_rate": round(product * 100.0, 2),
            "label": "Estimación aproximada: producto de los hit rates individuales. No es una probabilidad real del parlay (los partidos no son independientes y las cuotas no entran en el cálculo).",
            "missing_legs": len(picks) - len(usable),
        }

    return {
        "ok": True,
        "source": source,
        "errors": errors,
        "picks": picks,
        "parlay": parlay,
    }
