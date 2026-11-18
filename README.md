# hba-backend

API de HBA Sports Intelligence (FastAPI), desplegada en Render: https://hba-backend.onrender.com

## Arranque

```bash
pip install -r requirements.txt
uvicorn main:app --host 0.0.0.0 --port $PORT
```

## Variables de entorno (dashboard de Render)

- `API_FOOTBALL_KEY` — clave de API-Football Pro.
- `XAI_API_KEY` — análisis con Grok (NBA y fútbol).
- `BIGBALLS_API_KEY` — datos de jugadores NBA.

No subas estas claves al repositorio.

## Fútbol

La temporada actual se resuelve sola. Las estadísticas de partidos terminados se cachean 30 días.

- `GET /api/football/search?q=` — autocompletado de equipos y jugadores.
- `GET /api/football/teams?league=239` — equipos de una liga.
- `GET /api/football/squad?team=` — plantilla.
- `POST /api/football/analyze/team` — promedios y hit rate de remates, goles, córners, tarjetas, BTTS, atajadas y offsides (últimos 5/10/15, local/visitante, H2H). `include_ai` opcional.
- `POST /api/football/analyze/player` — remates, goles, asistencias, faltas y atajadas (porteros) con hit rate.
- Se mantienen `/football/team`, `/football/h2h` y `/analyze` (NBA).
