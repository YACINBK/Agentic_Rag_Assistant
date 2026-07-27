# Auth Error Handling

**File:** `app/api/error_handlers.py` · **LLM:** none · **External services:** none

---

## 1. Role

Converts `AuthenticationError` and `AuthorizationError` exceptions into
client-appropriate HTTP responses. Detects whether the request came from
a browser, an HTMX partial, or a JSON API client and responds accordingly.

Registered once at app startup via `register_error_handlers(app)` in `main.py`.

## 2. Interface

```python
def register_error_handlers(app: FastAPI) -> None
```

Registers two `@app.exception_handler` closures on the FastAPI instance:
- `authentication_error_handler` for `AuthenticationError`
- `authorization_error_handler` for `AuthorizationError`

## 3. Data flow

```
Route raises AuthenticationError or AuthorizationError
    │
    ▼
FastAPI exception_handler dispatch
    │
    ▼
Detect client type:
    ├── HX-Request header?     → HTMX client
    ├── Accept: text/html?     → Browser
    └── else                   → JSON API
    │
    ▼
Return appropriate response
```

### Response matrix

| Exception | Client | Status | Body |
|---|---|---|---|
| `AuthenticationError` | Browser | 303 | Redirect to `/auth/login` |
| `AuthenticationError` | HTMX | 401 | JSON + `HX-Redirect: /auth/login` header |
| `AuthenticationError` | JSON | 401 | `{"detail": "Authentication required"}` |
| `AuthorizationError` | Browser | 403 | Full page `pages/403.html` |
| `AuthorizationError` | HTMX | 403 | Partial `partials/403.html` |
| `AuthorizationError` | JSON | 403 | `{"detail": "Insufficient permissions"}` |

## 4. Behaviour

**Client detection order matters.** HTMX is checked first because HTMX
requests typically also send `Accept: text/html`. If HTML were checked first,
HTMX partials would incorrectly receive full-page responses.

- `_is_htmx(request)` → checks `HX-Request` header (case-insensitive)
- `_accepts_html(request)` → checks if `text/html` appears in `Accept` header

**Authentication errors** redirect the user to login. The HTMX variant uses
`HX-Redirect` so that HTMX replaces the entire page rather than swapping
a partial into an existing layout.

**Authorization errors** render a 403 page. Browser clients get the full page
(extends `base.html`); HTMX clients get a standalone `<div>` fragment
suitable for swapping into the current layout.

## 5. Templates

### `pages/403.html`

Full page extending `base.html`. Contains heading, explanation text, and
a link back to the dashboard. Used for direct browser navigation to
a forbidden resource.

### `partials/403.html`

Standalone `<div>` fragment with no `<html>` wrapper. Used when an HTMX
action (button click, form submit) triggers an authorization failure —
the partial replaces the swap target without breaking the page layout.

## 6. Integration with dependencies

These handlers catch exceptions raised by the auth dependencies:

```
require_auth  ──raises──▶  AuthenticationError  ──handled by──▶  authentication_error_handler
require_admin ──raises──▶  AuthorizationError   ──handled by──▶  authorization_error_handler
require_owner ──raises──▶  AuthorizationError   ──handled by──▶  authorization_error_handler
```

The dependencies raise; the handlers respond. Neither layer knows the
other's internals — they communicate through exception types only.

## 7. Design decisions

| Decision | Rationale |
|---|---|
| Detection order: HTMX → HTML → JSON | HTMX requests also carry `Accept: text/html`. Checking HTMX first prevents misrouting. |
| HTMX auth error returns JSON body + `HX-Redirect` header | HTMX needs the header to perform a full-page redirect. JSON body provides a fallback if the header is missed. |
| Separate full page vs partial for 403 | HTMX swaps target a `<div>`, not the whole page. A full `<html>` document inside a swap target breaks layout. |
| Module-level `Jinja2Templates` instance | Shared across both handlers. Directory set to `app/templates/` — standard project layout. |
| No logging in handlers | Error handlers are response formatters. Logging happens upstream (structlog in middleware or route). |

## 8. Constraints honoured

- No `KeycloakAuthService` import — handlers are pure response formatters.
- No sensitive data in responses — no session IDs, tokens, or stack traces.
- No exception swallowing — handlers convert exceptions to responses, they don't suppress them.
- Frontend convention: partials have no `<html>` wrapper (CLAUDE.md §17 rule 2).

## 9. Test coverage (`tests/unit/test_auth_error_handling.py`)

| Test | Proves |
|---|---|
| `test_auth_error_redirects_browser` | `Accept: text/html` → 303 redirect to `/auth/login` |
| `test_auth_error_htmx_redirect` | `HX-Request: true` → 401 + `HX-Redirect` header |
| `test_auth_error_json_response` | `Accept: application/json` → 401 JSON |
| `test_authz_error_returns_403_page` | `Accept: text/html` → 403 with "forbidden" in body |
| `test_authz_error_htmx_returns_partial` | `HX-Request: true` → 403 without `<html>` wrapper |
| `test_authz_error_json_response` | `Accept: application/json` → 403 JSON `{"detail": "Insufficient permissions"}` |
