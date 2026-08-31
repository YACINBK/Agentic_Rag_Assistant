# Frontend Base Pages — Landing + Dashboard

**File:** `app/templates/pages/landing.html`, `app/templates/pages/dashboard.html` · **LLM:** none · **External services:** none

> **Amended 2026-08-31.** The standalone login page was removed: `/` is now a
> public landing for anonymous visitors (the Login button is the OIDC entry
> point), and `/auth/login` is a redirect to `/`. It must keep existing under
> that name — Keycloak's registered `post_logout_redirect_uri` and the auth
> error handlers point at it, and both converge on the landing.

---

## 1. Role

Entry points for authenticated and unauthenticated users. The **landing** is
the first thing anyone sees — the app itself, publicly, with a Login button.
The **dashboard** is what `/` becomes once a session exists. There is no
dedicated login page and no redirect to one: auth is one click inside the app.

## 2. Landing page (`pages/landing.html`)

Extends `base.html`. Uses `btn` macro for the login link.

- Company branding area (Whitecape Technologies heading)
- Single "Login" button linking to `/auth/start` — no Keycloak wording; the
  mechanism is not the user's concern
- No nav bar (unauthenticated context — `user` is not in template context)
- A future signup button would slot in beside Login (registration is disabled
  in the realm for now)

## 3. Dashboard page (`pages/dashboard.html`)

Extends `base.html`. Uses `btn` macro for navigation.

- Displays user email and primary role from `UserSession`
- Conditional admin badge: `{% if user.is_admin %}` → `<span class="badge badge-admin">Admin</span>`
- Conditional owner badge: `{% if user.is_owner %}` → `<span class="badge badge-owner">Owner</span>`
- "Search Knowledge Base" link to `/search`
- Nav bar visible (user context available via `base.html`)

## 4. Route handlers

Both pages are served from `app/main.py`:

| Route | Handler | Template | Context |
|---|---|---|---|
| `GET /` (no session) | `index` | `pages/landing.html` | (none) |
| `GET /` (session) | `index` | `pages/dashboard.html` | `{"user": UserSession}` |
| `GET /auth/login` | `login_page` | redirect → `/` | (none) |

Anonymous `/` renders the landing (no redirect). Expired sessions and
Keycloak's post-logout bounce hit `/auth/login` and redirect to the landing.

## 5. Test coverage

7 tests in `tests/unit/test_frontend_base_pages.py`:
- Login renders with sign-in button, no nav user display
- Dashboard shows email, role, admin badge, owner badge, search link
- Both pages extend base.html (contain nav brand)
