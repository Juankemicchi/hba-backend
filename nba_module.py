import requests
import pandas as pd
import os

API_KEY = os.getenv("BALLDONTLIE_API_KEY", "")
BASE_URL = "https://api.balldontlie.io/v1"
HEADERS = {"Authorization": f"Bearer {API_KEY}"}

def get_player_id(player_name: str):
    response = requests.get(
        f"{BASE_URL}/players",
        headers=HEADERS,
        params={"search": player_name, "per_page": 10}
    )
    
    if response.status_code != 200:
        raise ValueError(f"Error en la API: {response.status_code}")
    
    data = response.json().get("data", [])
    
    if not data:
        return None, None
    
    search = player_name.lower()
    for player in data:
        full_name = f"{player['first_name']} {player['last_name']}".lower()
        if search in full_name or full_name in search:
            return player["id"], f"{player['first_name']} {player['last_name']}"
    
    player = data[0]
    return player["id"], f"{player['first_name']} {player['last_name']}"

def get_player_games(player_name: str, last_n: int = 15):
    player_id, full_name = get_player_id(player_name)
    
    if not player_id:
        raise ValueError(f"Jugador '{player_name}' no encontrado")
    
    response = requests.get(
        f"{BASE_URL}/stats",
        headers=HEADERS,
        params={
            "player_ids[]": player_id,
            "seasons[]": 2025,
            "per_page": last_n
        }
    )
    
    if response.status_code != 200:
        raise ValueError(f"Error al obtener stats: {response.status_code}")
    
    data = response.json().get("data", [])
    
    if not data:
        return pd.DataFrame(), full_name
    
    rows = []
    for stat in data:
        rows.append({
            "GAME_DATE": stat["game"]["date"],
            "MATCHUP": f"{stat['team']['abbreviation']} vs OPP",
            "PTS": stat.get("pts", 0),
            "REB": stat.get("reb", 0),
            "AST": stat.get("ast", 0),
            "FG3M": stat.get("fg3m", 0),
            "STL": stat.get("stl", 0),
            "BLK": stat.get("blk", 0),
            "MIN": stat.get("min", "0"),
            "TOV": stat.get("turnover", 0),
            "FGM": stat.get("fgm", 0),
            "FGA": stat.get("fga", 0),
        })
    
    df = pd.DataFrame(rows)
    df["GAME_DATE"] = pd.to_datetime(df["GAME_DATE"])
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
    elif prop in ["DD", "TD"]:
        value_col = "PTS"
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
