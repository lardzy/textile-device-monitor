# AGENTS.md - Developer Guidelines

## Quick Commands

### Backend (Python/FastAPI)
```bash
# Install dependencies
cd backend && pip install -r requirements.txt

# Run development server
cd backend && python -m uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Run tests (when added)
cd backend && pytest -v

# Run single test
cd backend && pytest tests/test_api.py::test_endpoint -v

# Lint (when configured)
cd backend && ruff check app/
cd backend && mypy app/

# Format
cd backend && black app/
cd backend && isort app/
```

### Frontend (React/Vite)
```bash
# Install dependencies
cd frontend && npm install

# Run development server
cd frontend && npm run dev

# Build for production
cd frontend && npm run build

# Preview production build
cd frontend && npm run preview

# Lint (when configured)
cd frontend && npm run lint

# Type check (when configured)
cd frontend && npm run typecheck
```

### Docker
```bash
# Start all services
docker-compose up -d

# Rebuild and start
docker-compose up -d --build

# View logs
docker-compose logs -f backend
docker-compose logs -f frontend

# Stop all services
docker-compose down

# Stop and remove volumes
docker-compose down -v
```

## Code Style Guidelines

### Python (Backend)

**Imports:**
- Use absolute imports within the app: `from app.models import Device`
- Group imports in this order: stdlib, third-party, local
- One import per line for readability
- Use `isort` to organize: `isort app/`

**Formatting:**
- Use Black for code formatting: `black app/`
- Line length: 100 characters
- Use 4 spaces for indentation
- Use double quotes for strings

**Types:**
- Always use type hints for function parameters and return values
- Use Pydantic models for request/response validation
- Use SQLAlchemy types for database models
- Use `Optional[T]` instead of `None` or `T | None`
- Use `List[T]` instead of `list`

**Naming Conventions:**
- Variables and functions: `snake_case` (e.g., `device_list`, `get_device`)
- Classes: `PascalCase` (e.g., `DeviceService`, `DeviceModel`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `HEARTBEAT_TIMEOUT`)
- Private methods: `_private_method`
- Database tables: `snake_case` (e.g., `device_status_history`)

**Error Handling:**
- Use try/except for database operations
- Return proper HTTP status codes via FastAPI
- Use Pydantic `ValidationError` for input validation
- Log errors with context: `logger.error(f"Error getting device {id}: {e}")`
- Never expose sensitive data in error messages

**API Design:**
- Use async/await for all database I/O
- Return meaningful status messages in JSON responses
- Use HTTP status codes appropriately (200, 201, 404, 422, 500)
- Include pagination parameters in list endpoints
- Document all endpoints with OpenAPI docstrings

### JavaScript/React (Frontend)

**Imports:**
- Use ES6 imports: `import { useState } from 'react'`
- Group imports: React, third-party, local
- Named exports preferred over default

**Formatting:**
- Use Prettier when configured
- Use 2 spaces for indentation
- Use single quotes for strings
- Use semicolons consistently

**Types/Props:**
- Define PropTypes interfaces for components
- Use proper TypeScript-like JSDoc for complex objects
- Destructure props in function parameters

**Naming Conventions:**
- Components: `PascalCase` (e.g., `DeviceMonitor`, `QueueAssistant`)
- Functions/variables: `camelCase` (e.g., `fetchDevices`, `isLoading`)
- Constants: `UPPER_SNAKE_CASE` (e.g., `API_BASE_URL`)
- Files: `PascalCase` for components (e.g., `DeviceMonitor.jsx`)

**Error Handling:**
- Use try/catch for async operations
- Display user-friendly error messages with Ant Design message/error components
- Log errors to console with context
- Never expose stack traces to users
- Handle API errors gracefully with fallback UI states

**Component Guidelines:**
- Keep components small and focused (< 300 lines)
- Extract reusable logic to custom hooks
- Use functional components with hooks
- Use Ant Design components consistently
- Implement loading states for async operations
- Use memoization (useMemo, useCallback) for performance

## Architecture Notes

**Backend Structure:**
- `api/`: FastAPI route handlers
- `crud/`: Database operations (Business Logic Layer)
- `models.py`: SQLAlchemy database models
- `schemas.py`: Pydantic validation models
- `tasks/`: Background jobs and scheduled tasks
- `websocket/`: WebSocket connection management

**Frontend Structure:**
- `api/`: Axios API clients for each backend module
- `pages/`: Page-level components
- `components/`: Reusable UI components
- `websocket/`: WebSocket client wrapper
- `utils/`: Helper functions (localStorage, date formatting)

**Database:**
- Use SQLAlchemy ORM for all database operations
- Create queries through CRUD layer, never raw SQL
- Use database sessions with context managers
- Always commit/rollback explicitly

**WebSocket:**
- Use single connection manager instance
- Broadcast messages to all connected clients
- Handle disconnect gracefully
- Reconnect automatically with exponential backoff

## Testing (When Implemented)

- Write unit tests for all CRUD operations
- Write integration tests for API endpoints
- Mock database connections in tests
- Test WebSocket message handling
- Test error paths and edge cases

## Common Patterns

**Backend:**
```python
# Database operation pattern
def get_device(db: Session, device_id: int) -> Optional[Device]:
    return db.query(Device).filter(Device.id == device_id).first()

# API endpoint pattern
@router.get("/devices/{device_id}")
async def get_device(device_id: int, db: Session = Depends(get_db)):
    device = device_crud.get_device(db, device_id)
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return device
```

**Frontend:**
```javascript
// API call pattern
const fetchData = async () => {
  setLoading(true);
  try {
    const data = await api.getEndpoint();
    setData(data);
  } catch (error) {
    message.error('Failed to load data');
  } finally {
    setLoading(false);
  }
};
```

## Project-Specific Rules

1. **Device Heartbeat**: Devices must report every 5 seconds, 30-second timeout marks as offline
2. **Queue Management**: Position changes are logged, only today's logs are retained
3. **Data Retention**: Automatic cleanup of 30-day-old history at 2 AM daily
4. **No Authentication**: All endpoints are publicly accessible (LAN only)
5. **File Encoding**: UTF-8 for all Python/JS files
6. **API Version**: Current version is v1, maintain backward compatibility
7. **Locale**: Chinese language (zh-CN) for all UI elements

## Troubleshooting Common Issues

**PostgreSQL connection errors:** Ensure database service is running and accessible
**WebSocket connection failures:** Check firewall, verify correct WS_URL in frontend
**CORS errors:** Verify CORS_ORIGINS in backend .env includes frontend URL
**Import errors:** Ensure Python virtual environment is activated
**Docker build failures:** Check Dockerfile syntax and dependencies in requirements.txt

## Notes for AI Agents

- This is a LAN-only system, no authentication required
- WebSocket is critical for real-time updates
- External devices call status endpoint directly without auth
- Browser localStorage is used for inspector name persistence
- All timestamps should use proper timezone handling
- Docker Compose is the primary deployment method
- Frontend proxy configuration handles API routing to backend
