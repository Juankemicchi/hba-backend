import os
import requests

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

def get_team_id(team_name: str):
    try:
        response = requests.get(
            f"{BASE_URL}/teams",
            headers=HEADERS,
            params={"name": team_name}
        )
        data = response.json()
        if data.get("teams"):
            return data["teams"][0]["id"]
    except Exception as e:
        print("Error buscando equipo:", e)
    return None

def get_team_stats(team_name: str, competition: str = "PL"):
    team_id = get_team_id(team_name)
    if not team_id:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/teams/{team_id}",
            headers=HEADERS
        )
        return response.json()
    except Exception as e:
        print("Error stats equipo:", e)
        return None

def get_team_matches(team_name: str, limit: int = 10):
    team_id = get_team_id(team_name)
    if not team_id:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/teams/{team_id}/matches",
            headers=HEADERS,
            params={"status": "FINISHED", "limit": limit}
        )
        return response.json()
    except Exception as e:
        print("Error partidos equipo:", e)
        return None

def get_h2h(team1: str, team2: str, limit: int = 10):
    id1 = get_team_id(team1)
    id2 = get_team_id(team2)
    if not id1 or not id2:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/teams/{id1}/matches",
            headers=HEADERS,
            params={"status": "FINISHED", "limit": 50}
        )
        data = response.json()
        matches = data.get("matches", [])
        
        h2h_matches = [
            m for m in matches 
            if m["homeTeam"]["id"] == id2 or m["awayTeam"]["id"] == id2
        ]
        return {"matches": h2h_matches[:limit]}
    except Exception as e:
        print("Error H2H:", e)
        return None
