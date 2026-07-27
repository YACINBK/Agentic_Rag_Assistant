# Auth Integration Tests

**File:** `tests/integration/test_auth_flow.py` · **LLM:** none · **External services:** none (all mocked)

---

## 1. Role

End-to-end verification of the complete auth chain through a real FastAPI
application. Tests that routes, dependencies, error handlers, and session
management work correctly together — not in isolation.

This is the only test module that exercises the full stack:
`route → require_auth → KeycloakAuthService → MockRedis → error handler → HTTP response`

## 2. What is tested

| Layer | Real or mocked |
|---|---|
| FastAPI routes | Real (mirrored from main.py) |
| `require_auth` dependency | Real |
| `register_error_handlers` | Real |
| `KeycloakAuthService` | Real |
| Redis session storage | MockRedis (in-memory) |
| Keycloak HTTP (token + userinfo) | Mocked via `unittest.mock.patch` |
| PostgreSQL / SQLAlchemy | Mocked AsyncSession |
| Jinja2 templates | Real (reads from `app/templates/`) |

## 3. Test app setup

Each test builds a minimal FastAPI app via `_make_app(redis, db)` that mirrors
the real app's auth wiring:

```python
app = FastAPI()
app.state.redis = redis          # MockRedis injected here
register_error_handlers(app)     # real error handlers
# 4 auth routes + protected root /
```

This approach tests the wiring without the full lifespan/startup overhead.

## 4. Assertions covered

| # | Assertion | Test |
|---|---|---|
| 1 | Unauthenticated GET `/` → 303 redirect to `/auth/login` | `test_unauthenticated_root_redirects` |
| 2 | GET `/auth/login` → 200 with "Sign in" | `test_login_page_renders` |
| 3 | GET `/auth/start` → redirect containing `keycloak` + `openid-connect/auth` | `test_auth_start_redirects_to_keycloak` |
| 4 | Valid callback → `session_id` cookie + redirect to `/` + `session:*` in Redis | `test_full_callback_flow_sets_cookie` |
| 5 | Authenticated GET `/` → 200 dashboard | `test_authenticated_root_shows_dashboard` |
| 6 | Logout → session deleted + cookie cleared + redirect to Keycloak | `test_logout_clears_session` |
| 7 | Expired session → redirect to `/auth/login` | Covered by test 1 (empty MockRedis = expired) |

## 5. Key design notes

**`Accept: text/html` header on browser tests** — TestClient's default
`Accept: */*` routes `AuthenticationError` to the JSON handler (401) instead
of the browser handler (303). Explicit header mirrors a real browser request.

**`client.cookies.set(...)` on the client instance** — per-request `cookies=`
is deprecated in TestClient. Setting on the client instance gives consistent
cookie persistence across the request.

**Redirect status codes** — `RedirectResponse` in FastAPI defaults to 307 for
`/auth/start` and `/auth/logout`. Tests assert `status in (302, 303, 307)` and
verify the `Location` URL strictly. This is correct behaviour, not a bug.

**Assertion 7 coverage** — an empty `MockRedis` with a cookie present is
behaviourally identical to an expired session (Redis miss). Test 1 covers this
path without a separate test case.

## 6. Security properties verified end-to-end

| Property | Verified by |
|---|---|
| Unauthenticated requests are rejected before route handler runs | Test 1 — `require_auth` raises before index body executes |
| Full OIDC callback creates a valid server-side session | Test 4 — `session:*` key present in MockRedis after callback |
| Session cookie is set with `httponly=True, samesite=lax` | Test 4 — `Set-Cookie` header inspected |
| Logout deletes the Redis session (not just the cookie) | Test 6 — `session:abc123` absent from MockRedis after logout |
| Logout redirects to Keycloak RP-initiated logout endpoint | Test 6 — Location contains `keycloak` + `logout` |

## 7. Constraints honoured

- No real Keycloak calls — all HTTP mocked.
- No real Redis — MockRedis throughout.
- No real PostgreSQL — AsyncSession mocked.
- Each test is independent — no shared state between tests.
- No pipeline logic tested here — auth-only scope.

## 8. Test coverage (`tests/integration/test_auth_flow.py`)

| Test | Proves |
|---|---|
| `test_unauthenticated_root_redirects` | Full dependency + error handler chain produces 303 |
| `test_login_page_renders` | Login route + Jinja2 template renders correctly |
| `test_auth_start_redirects_to_keycloak` | Auth start route builds correct Keycloak URL |
| `test_full_callback_flow_sets_cookie` | Complete OIDC callback: state validation → token exchange → user sync → session creation → cookie |
| `test_authenticated_root_shows_dashboard` | Valid session cookie → dashboard rendered with user data |
| `test_logout_clears_session` | Session deleted from Redis + cookie cleared + Keycloak redirect |
