import requests
import pandas as pd
import os

API_KEY = os.getenv("BIGBALLS_API_KEY", "")
BASE_URL = "https://api.bigballsdata.com/v1"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def get_player_id(player_name: str):
    response = requests.get(
        f"{BASE_URL}/players",
        headers=HEADERS,
        params={"sport": "basketball", "league": "nba", "name": player_name}
    )
    
    if response.status_code != 200:
        raise ValueError(f"Error en la API: {response.status_code}")
    
    data = response.json().get("data", [])
    
    if not data:
        return None, None
    
    player = data[0]
    return player["id"], player.get("name", player_name)

def get_player_games(player_name: str, last_n: int = 15):
    player_id, full_name = get_player_id(player_name)
    
    if not player_id:
        raise ValueError(f"Jugador '{player_name}' no encontrado")
    
    response = requests.get(
        f"{BASE_URL}/players/{player_id}/gamelog",
        headers=HEADERS,
        params={"limit": last_n}
    )
    
    if response.status_code != 200:
        raise ValueError(f"Error al obtener stats: {response.status_code}")
    
    data = response.json().get("data", [])
    
    print("=== RESPUESTA DE LA API ===")
    print(data)
    print("===========================")
    
    if not data:
        return pd.DataFrame(), full_name
    
    rows = []
    for game in data:
        rows.append({
            "GAME_DATE": game.get("date") or game.get("game_date"),
            "PTS": game.get("points") or game.get("pts") or 0,
            "REB": game.get("rebounds") or game.get("reb") or 0,
            "AST": game.get("assists") or game.get("ast") or 0,
            "FG3M": game.get("three_pointers_made") or game.get("fg3m") or 0,
            "MIN": game.get("minutes") or game.get("min") or 0,
            "STL": game.get("steals") or game.get("stl") or 0,
            "BLK": game.get("blocks") or game.get("blk") or 0,
        })
    
    df = pd.DataFrame(rows)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"], errors="coerce")
    df = df.sort_values("GAME_DATE", ascending=False)
    
    return df.head(last_n), full_name

def analyze_prop(player_name: str, prop: str = "PTS", line: float = 20.5, last_n: int = 15):
    df, full_name = get_player_games(player_name, last_n)
    
    if df.empty:
        raise ValueError(f"No se encontraron partidos para {player_name}")
    
    df = df.copy()
    
    if prop == "PRA":
        df["PRA"] = df["PTS"] + df["REB"] + df["AST"]
        value_col = "PRA"
    elif prop == "PR":
        df["PR"] = df["PTS"] + df["REB"]
        value_col = "PR"
    elif prop == "PA":
        df["PA"] = df["PTS"] + df["AST"]
        value_col = "PA"
    elif prop == "RA":
        df["RA"] = df["REB"] + df["AST"]
        value_col = "RA"
    elif prop == "DD":
        df["DD"] = ((df["PTS"] >= 10).astype(int) + (df["REB"] >= 10).astype(int) + (df["AST"] >= 10).astype(int) >= 2).astype(int)
        value_col = "DD"
    elif prop == "TD":
        df["TD"] = ((df["PTS"] >= 10).astype(int) + (df["REB"] >= 10).astype(int) + (df["AST"] >= 10).astype(int) >= 3).astype(int)
        value_col = "TD"
    else:
        if prop not in df.columns:
            raise ValueError(f"Prop '{prop}' no disponible")
        value_col = prop
    
    df["Over"] = df[value_col] > line
    
    stats = {
        "player": full_name,
        "avg": round(df[value_col].mean(), 1),
        "hit_rate": round(df["Over"].mean() * 100, 1),
        "games": len(df),
        "prop": prop,
        "line": line,
        "value_col": value_col
    }
    
    return df, stats
