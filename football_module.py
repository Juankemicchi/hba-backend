import os
import requests

API_KEY = os.getenv("FOOTBALL_DATA_KEY", "")
BASE_URL = "https://api.football-data.org/v4"
HEADERS = {"X-Auth-Token": API_KEY}

def get_team_id(team_name: str):
    try:
        # Buscar en competiciones principales
        competitions = ["PL", "PD", "SA", "BL1", "FL1", "CL"]
        for comp in competitions:
            response = requests.get(
                f"{BASE_URL}/competitions/{comp}/teams",
                headers=HEADERS
            )
            if response.status_code != 200:
                continue
            data = response.json()
            for team in data.get("teams", []):
                if team_name.lower() in team["name"].lower() or team_name.lower() in team.get("shortName", "").lower():
                    return team["id"]
    except Exception as e:
        print("Error buscando equipo:", e)
    return None

def get_team_stats(team_name: str):
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

def get_h2h(team1: str, team2: str, limit: int = 10):
    id1 = get_team_id(team1)
    id2 = get_team_id(team2)
    if not id1 or not id2:
        return None
    
    try:
        response = requests.get(
            f"{BASE_URL}/teams/{id1}/matches",
            headers=HEADERS,
            params={"status": "FINISHED", "limit": 100}
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
