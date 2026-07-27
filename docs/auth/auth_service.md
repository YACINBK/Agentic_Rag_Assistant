# Auth Service — KeycloakAuthService

**File:** `app/services/auth.py` · **LLM:** none · **External services:** Keycloak (OIDC), Redis, PostgreSQL

---

## 1. Role

Handles the full Keycloak OIDC Authorization Code flow with PKCE.
Manages the lifecycle: authorization URL generation, callback token exchange,
user info retrieval, lazy database sync, session creation, session lookup,
and logout. This is the only class that talks to Keycloak.

## 2. Interface

```python
class KeycloakAuthService(BaseAuthService):
    def __init__(self, db: AsyncSession, redis: aioredis.Redis) -> None
    async def get_authorization_url(self, request: Request) -> str
    async def handle_callback(self, request: Request) -> UserSession
    async def get_current_user(self, request: Request) -> UserSession | None
    async def logout(self, request: Request) -> str
```

Implements `BaseAuthService` ABC from `app/core/security.py`.

## 3. Data flow

```
get_authorization_url(request)
    ├── Build AsyncOAuth2Client with PKCE (S256)
    ├── Create authorization URL pointing to Keycloak
    ├── Store OAuth state + code_verifier in Redis (300s TTL)
    └── Return authorization URL string

handle_callback(request)
    ├── Extract code + state from query params
    │     └── Missing? → raise AuthenticationError
    ├── Lookup OAuth state in Redis
    │     └── Not found? → raise AuthenticationError (expired)
    ├── Delete consumed OAuth state from Redis
    ├── Exchange code for tokens via Keycloak /token endpoint
    ├── Fetch userinfo via Keycloak /userinfo endpoint
    ├── Lazy sync user to PostgreSQL (find-or-create Role + User)
    ├── Create server-side session in Redis (SESSION_TTL_SECONDS)
    ├── Attach session_id to request.state
    └── Return UserSession

get_current_user(request)
    ├── Read session_id cookie
    │     └── No cookie? → return None
    ├── Lookup session:{id} in Redis
    │     └── Not found? → return None
    └── Deserialize JSON → return UserSession

logout(request)
    ├── Read session_id cookie
    ├── Delete session:{id} from Redis
    └── Return Keycloak logout URL (with post-logout redirect)
```

## 4. Behaviour

**Authorization URL:** Builds a Keycloak OIDC auth URL with PKCE challenge.
The OAuth state and PKCE code verifier are stored in Redis under
`oauth_state:{state}` with a 300-second TTL.

**Callback:** Validates code and state, exchanges the authorization code for
tokens, fetches user info from Keycloak, then performs lazy sync:
- Finds or creates the Role in PostgreSQL (by name from `realm_access.roles`)
- Finds or creates the User (by `keycloak_id`)
- Updates email and role on every login (lazy sync)
- Creates a server-side session in Redis with the configured TTL

**Current user:** Pure Redis lookup — no database or Keycloak call. This is
what `require_auth` in `app/api/dependencies.py` delegates to indirectly
(both read from Redis with the same key format).

**Logout:** Deletes the Redis session and returns the Keycloak RP-initiated
logout URL so the browser can clear the Keycloak SSO session too.

## 5. Keycloak endpoints used

| Endpoint | Method | When |
|---|---|---|
| `/realms/{realm}/protocol/openid-connect/auth` | GET (redirect) | `get_authorization_url` |
| `/realms/{realm}/protocol/openid-connect/token` | POST | `handle_callback` (code exchange) |
| `/realms/{realm}/protocol/openid-connect/userinfo` | GET | `handle_callback` (user info) |
| `/realms/{realm}/protocol/openid-connect/logout` | GET (redirect) | `logout` |

## 6. Lazy sync logic

```
realm_access.roles from Keycloak userinfo
    │
    ▼
Filter out Keycloak system roles (offline_access, uma_authorization, default-roles-*)
    │
    ▼
First remaining role → role_name (fallback: "user")
    │
    ▼
SELECT Role WHERE name = role_name
    ├── Found → use it
    └── Not found → INSERT new Role, flush
    │
    ▼
SELECT User WHERE keycloak_id = sub
    ├── Found → update email + role_id, flush
    └── Not found → INSERT new User, flush
    │
    ▼
Return UserSession(user_id, keycloak_id, email, role, is_admin, is_owner)
```

No webhooks — sync happens only on login (CLAUDE.md §12).

## 7. Design decisions

| Decision | Rationale |
|---|---|
| PKCE (S256) | Prevents authorization code interception, even for confidential clients. |
| OAuth state in Redis (300s TTL) | Short-lived, auto-expires, no DB write for transient OIDC state. |
| Lazy sync, not webhook | CLAUDE.md §12 — final decision. No Keycloak webhook infrastructure. |
| Session as JSON in Redis | Fast per-request lookup. Avoids DB call on every request. |
| `request.state.session_id` | Attaches session ID to request so the route handler can set the cookie. Clean separation — the service creates the session, the route sets the cookie. |
| Filter system roles from realm_access | Keycloak always includes `offline_access`, `uma_authorization`, etc. The first non-system role is the user's primary role. |

## 8. Constraints honoured

- All external calls go through `httpx` / `authlib` — no direct Keycloak SDK dependency.
- Roles resolved from Keycloak claims, not hardcoded (CLAUDE.md §12).
- Session-based auth with HTTP-only cookie, not Bearer tokens (CLAUDE.md §3, §12).
- Lazy sync on login only (CLAUDE.md §12).
- `UserSession` dataclass returned, not ORM object.

## 9. Test coverage (`tests/unit/test_auth_service.py`)

| Test | Proves |
|---|---|
| `test_authorization_url_contains_keycloak_endpoint` | URL contains correct Keycloak auth path |
| `test_authorization_url_stores_state_in_redis` | `oauth_state:*` key created in Redis |
| `test_callback_creates_session` | Valid code+state → UserSession + `session:*` in Redis + OAuth state consumed |
| `test_callback_missing_code_raises` | No `code` param → `AuthenticationError` |
| `test_callback_expired_state_raises` | State not in Redis → `AuthenticationError` |
| `test_get_current_user_valid_session` | Cookie + Redis session → correct `UserSession` |
| `test_get_current_user_no_session` | Cookie but no Redis entry → `None` |

Assertion 8 (`logout` deletes session + returns logout URL) is defined in the contract
but excluded from the 7-test scope. Covered by integration tests in Module 4.
