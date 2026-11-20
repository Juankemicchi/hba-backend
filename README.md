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

## VIP AI Assistant

Endpoints bajo `/api/vip/*` protegidos por `require_vip` (header `X-VIP-Token` o `Authorization: Bearer`).

Variables de entorno (opcionales; si faltan, el sitio público sigue funcionando y el panel VIP muestra la pantalla de acceso):

- `VIP_ACCESS_CODES` — códigos temporales separados por coma, para pruebas.
- `SUPABASE_URL`, `SUPABASE_ANON_KEY`, `SUPABASE_JWT_SECRET`, `SUPABASE_SERVICE_ROLE_KEY` — cuentas y `profiles.is_vip` (ver `supabase_schema.sql`).
- `ADMIN_TOKEN` — protege `POST /api/admin/vip` para marcar usuarios VIP (solo si Supabase está configurado).
- Hooks de pago (aún no hay pasarela): `MERCADOPAGO_WEBHOOK_SECRET` o `MP_WEBHOOK_SECRET`, `WOMPI_EVENTS_SECRET` o `WOMPI_WEBHOOK_SECRET`.

`XAI_API_KEY` ya se usa para explicar picks, leer capturas (Grok visión, modelo grok-4.7) y el Chat VIP con function calling. Sin esta clave, los picks y el análisis de texto siguen funcionando con las estadísticas de HBA.

- `GET /api/vip/config` — público; indica si hay Supabase o códigos.
- `GET /api/vip/picks?sport=all|nba|football&league=` — top 5 del día (caché 45 min).
- `POST /api/vip/slip` — analiza picks o una captura.
- `POST /api/vip/chat` — chat con herramientas de estadísticas reales.

## VIP picks engine v8

- Team markets use the team's last 15 games at the same venue (home/away). If that split has fewer than 8 games, all games are used and confidence is capped at 4/5 (Alta).
- The opponent's mirror record must back every team pick (e.g. Team Under 1.5 goals needs the opponent to concede under 2 in at least 60% of its games; 70% for Máxima confianza).
- Match markets need each team to back the line on its own (55%+).
- 1X2 crosses the venue win rate with the opponent's venue loss rate and requires a better goal difference per game.
- Value: model probability = 50% empirical + 50% Bayesian (prior = no-vig market probability, k=8). Published only if edge >= 4 p.p. (max 25).
- Goal unders priced above 1.55 are capped at Media (3/5) and dropped if the model is under 65%.
- Daily card: up to 5 football + 2 NBA picks, ranked by edge x confidence, max 2 per match. History keeps only each day's own matches and freezes the card once published.
