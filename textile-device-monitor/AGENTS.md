# AGENTS.md - textile-device-monitor

## Purpose
- This directory contains the monitor stack: FastAPI backend + React/Vite frontend.
- Commands below assume you are in `textile-device-monitor/` unless noted.
- Keep changes scoped to the monitor stack when working here.

## Commands

### Docker
```bash
docker-compose up -d
docker-compose up -d --build
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f postgres
docker-compose down
docker-compose down -v
```

### Backend (FastAPI)
```bash
cd backend
pip install -r requirements.txt
python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Frontend (React/Vite)
```bash
cd frontend
npm install
npm run dev
npm run build
npm run preview
```

### Tests / Lint
- No backend/frontend test runner configured.
- No eslint/prettier/ruff/black config in this repo; keep formatting consistent with existing files.

## Environment and Config
- Backend env file: `backend/.env` (template in `backend/.env.example`).
- Docker Compose sets DATABASE_URL, SECRET_KEY, HEARTBEAT_TIMEOUT, DATA_RETENTION_DAYS, CORS_ORIGINS.
- Frontend production routing: `frontend/nginx.conf`.

## Code Style Guidelines

### Python (backend)
- Indentation: 4 spaces; double quotes are common.
- Imports: stdlib, third-party, then local; prefer absolute `app.*` imports.
- Types: add type hints for public functions where practical; use `Optional` for nullable values.
- Naming: `snake_case` for functions/vars, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Error handling: raise `HTTPException` with proper status codes; log unexpected exceptions.
- Concurrency: long-running loops use background tasks (`asyncio.create_task`).
- DB access: use SQLAlchemy ORM via `app/crud/*`; avoid raw SQL.

### JavaScript/React (frontend)
- Formatting: 2-space indentation, single quotes, semicolons.
- Imports: React hooks first, then third-party, then local modules.
- Components: functional components with hooks; keep state updates immutable.
- Async: use `async/await` with `try/catch`; show errors via `message.error` or AntD modals.
- UI: use Ant Design components consistently; keep UI labels in Chinese (zh-CN).
- Data access: keep API calls in `src/api/*`; WebSocket usage goes through `src/websocket/client.js`.
- File layout: pages in `src/pages/*`, reusable pieces in `src/components/*`, helpers in `src/utils/*`.

## Architecture Notes
- Backend routes: `backend/app/api/*` and registered in `app.main`.
- CRUD layer: `backend/app/crud/*` for DB operations.
- Models/schemas: `backend/app/models.py`, `backend/app/schemas.py`.
- Tasks: `backend/app/tasks/*` (heartbeat, cleanup, queue timeouts).
- WebSocket manager: `backend/app/websocket/manager.py`.
- Frontend WebSocket client: `frontend/src/websocket/client.js` (fixed-delay reconnect, limited retries).

## Domain Rules
- Heartbeat interval is 5 seconds; backend marks devices offline after 30 seconds.
- Queue operations must update logs and broadcast WebSocket events.
- Data retention is 30 days; cleanup runs daily at 2 AM.
- No authentication (LAN-only); avoid adding auth without explicit approval.

## Generated Artifacts
- Avoid editing files under `frontend/dist/`.
