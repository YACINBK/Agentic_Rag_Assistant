# Auth Dependencies

**File:** `app/api/dependencies.py` · **LLM:** none · **External services:** Redis only

---

## 1. Role

Three FastAPI dependencies that form the authentication and authorization gate
for every protected route. They read the session cookie, validate it against
Redis, and return a `UserSession` — or raise if the user is unauthenticated
or lacks the required privilege level.

## 2. Interface

```python
async def require_auth(request: Request) -> UserSession
async def require_admin(user: UserSession = Depends(require_auth)) -> UserSession
async def require_owner(user: UserSession = Depends(require_auth)) -> UserSession
```

Dependency chain: `require_owner` and `require_admin` both depend on `require_auth`.
Used as `Depends()` in route signatures.

## 3. Data flow

```
Browser cookie (session_id=abc123)
    │
    ▼
require_auth
    ├── no cookie?           → raise AuthenticationError
    ├── Redis GET session:abc123
    │     └── None?          → raise AuthenticationError (expired)
    └── deserialize JSON     → return UserSession

require_admin(user)
    └── user.is_admin?       → True: return user / False: raise AuthorizationError

require_owner(user)
    └── user.is_owner?       → True: return user / False: raise AuthorizationError
```

## 4. Behaviour

- `require_auth` reads the `session_id` cookie from the request, looks up
  `session:{id}` in Redis, deserializes the JSON, and returns a `UserSession`.
- `require_admin` checks the `is_admin` flag on the already-authenticated user.
- `require_owner` checks the `is_owner` flag.
- All three raise on failure — they never return `None`. Error handlers
  (registered separately) convert exceptions to HTTP responses.

## 5. Routing implications

These dependencies are not pipeline nodes — they gate access to FastAPI routes.
Any route decorated with `Depends(require_auth)` will reject unauthenticated
requests before the route handler body executes.

```python
@app.get("/search")
async def search(request: Request, user: UserSession = Depends(require_auth)):
    ...  # user is guaranteed valid here

@app.post("/admin/upload")
async def upload(request: Request, user: UserSession = Depends(require_admin)):
    ...  # user is guaranteed admin here
```

## 6. Error handling

| Failure | Exception | Downstream effect |
|---|---|---|
| No session cookie | `AuthenticationError` | Error handler redirects to login or returns 401 |
| Expired/invalid session | `AuthenticationError` | Same |
| Not admin | `AuthorizationError` | Error handler returns 403 page or JSON |
| Not owner | `AuthorizationError` | Same |

Exceptions propagate to the error handlers registered by `register_error_handlers()`
in `app/api/error_handlers.py`. The dependencies never catch their own exceptions.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Redis-only, no DB call | Session validation happens on every request. Redis is sub-millisecond; a DB query per request would add latency and connection pressure. |
| Separate from `KeycloakAuthService` | The auth service handles OIDC flow (login/callback/logout). Dependencies handle per-request session validation. Different concerns, different lifetimes. |
| Raise, never return `None` | FastAPI's `Depends()` mechanism naturally propagates exceptions to error handlers. Returning `None` would require every route to check for it. |
| `is_admin` / `is_owner` flags, not role names | Authorization checks are flag-based, not string-based. No hardcoded role names (CLAUDE.md §12). |
| Constants match `auth.py` | `SESSION_COOKIE` and `SESSION_PREFIX` use the same values as `app/services/auth.py` to ensure consistency. |

## 8. Constraints honoured

- No database calls (CLAUDE.md §12 — session validation is Redis-only).
- No hardcoded role names (CLAUDE.md §12 — checks `is_admin`/`is_owner` flags).
- No `KeycloakAuthService` import — separation of concerns.
- `UserSession` dataclass returned, not ORM object — no SQLAlchemy dependency.

## 9. Test coverage (`tests/unit/test_auth_dependencies.py`)

| Test | Proves |
|---|---|
| `test_require_auth_valid_session` | Cookie + Redis entry → returns correct `UserSession` |
| `test_require_auth_no_cookie` | No cookie → `AuthenticationError` |
| `test_require_auth_expired_session` | Cookie present, Redis key gone → `AuthenticationError` |
| `test_require_admin_allows_admin_user` | `is_admin=True` → returns user |
| `test_require_admin_rejects_non_admin` | `is_admin=False` → `AuthorizationError` |
| `test_require_owner_allows_owner` | `is_owner=True` → returns user |
| `test_require_owner_rejects_admin_without_owner` | `is_admin=True, is_owner=False` → `AuthorizationError` |
