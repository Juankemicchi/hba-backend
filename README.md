# hba-backend

API de HBA Betting (FastAPI), desplegada en Render: https://hba-backend.onrender.com

## Arranque

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Variables de entorno (en el dashboard de Render)

- `API_FOOTBALL_KEY` — clave de API-Football Pro (api-sports.io v3). Obligatoria para fútbol.
- `XAI_API_KEY` — análisis con Grok (NBA y fútbol).
- `BIGBALLS_API_KEY` — datos de jugadores NBA.

No subas estas claves al repositorio.

## Fútbol (API-Football)

El backend hace de proxy para que la clave nunca quede en el navegador. La temporada se resuelve sola con la temporada actual de cada liga.

- `GET /api/football/leagues?country=&search=` — buscar ligas.
- `GET /api/football/teams?league=239` — equipos de una liga (temporada actual si no se indica `season`).
- `GET /api/football/standings?league=239`
- `GET /api/football/squad?team=1125` — plantilla.
- `GET /api/football/players?team=1125&season=2026&page=1` — jugadores con estadísticas (paginado).
- `POST /football/team` y `POST /football/h2h` — datos recientes (tabla, racha, lesiones, H2H) y análisis de Grok.

239 = Liga BetPlay (Primera A). Otras ligas se piden por su id (Premier League 39, La Liga 140, etc.).

Las plantillas y ligas se cachean 12 horas; fixtures, tabla y lesiones, 2 horas. En el plan free de Render el servicio se duerme y esa caché se pierde.
