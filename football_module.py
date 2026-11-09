import os
import requests
from datetime import datetime

API_KEY = os.getenv("FOOTBALL_API_KEY", "")
BASE_URL = "https://v3.football.api-sports.io"
HEADERS = {"x-apisports-key": API_KEY}

def get_team_id(team_name: str):
    try:
        response = requests.get(
            f"{BASE_URL}/teams",
            headers=HEADERS,
            params={"search": team_name}
        )
        data = response.json()
        if data.get("response"):
            return data["response"][0]["team"]["id"]
    except:
        pass
    return None

def get_player_id(player_name: str):
    try:
        response = requests.get(
            f"{BASE_URL}/players",
            headers=HEADERS,
            params={"search": player_name}
        )
        data = response.json()
        if data.get("response"):
            return data["response"][0]["player"]["id"]
    except:
        pass
    return None

def get_team_stats(team_name: str, league_id: int = 39, season: int = 2025):
    team_id = get_team_id(team_name)
    if not team_id:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/teams/statistics",
            headers=HEADERS,
            params={"team": team_id, "league": league_id, "season": season}
        )
        return response.json()
    except:
        return None

def get_h2h(team1: str, team2: str, last: int = 10):
    id1 = get_team_id(team1)
    id2 = get_team_id(team2)
    if not id1 or not id2:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/fixtures/headtohead",
            headers=HEADERS,
            params={"h2h": f"{id1}-{id2}", "last": last}
        )
        return response.json()
    except:
        return None
