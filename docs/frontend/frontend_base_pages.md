# Frontend Base Pages — Login + Dashboard

**File:** `app/templates/pages/login.html`, `app/templates/pages/dashboard.html` · **LLM:** none · **External services:** none

---

## 1. Role

Entry points for authenticated and unauthenticated users. Login page is the
first thing users see. Dashboard is the landing page after successful Keycloak
login — shows user info and provides navigation to the search feature.

## 2. Login page (`pages/login.html`)

Extends `base.html`. Uses `btn` macro for the Keycloak sign-in link.

- Company branding area (Whitecape Technologies heading)
- Single "Sign in with Keycloak" button linking to `/auth/start`
- No nav bar (unauthenticated context — `user` is not in template context)

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
| `GET /auth/login` | `login_page` | `pages/login.html` | (none) |
| `GET /` | `index` | `pages/dashboard.html` | `{"user": UserSession}` |

Dashboard redirects to `/auth/login` if no valid session.

## 5. Test coverage

7 tests in `tests/unit/test_frontend_base_pages.py`:
- Login renders with sign-in button, no nav user display
- Dashboard shows email, role, admin badge, owner badge, search link
- Both pages extend base.html (contain nav brand)
