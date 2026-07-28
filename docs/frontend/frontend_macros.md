# Frontend Macros + Base Template

**File:** `app/templates/macros/forms.html`, `app/templates/macros/buttons.html`, `app/templates/base.html` · **LLM:** none · **External services:** none

---

## 1. Role

Template infrastructure for the entire frontend. Jinja2 macros provide
parameterized, reusable UI primitives (equivalent to React prop-based
components). Base template enhancements add SSE support and extensibility
blocks for page-specific assets.

## 2. Macros

### `form_field(name, label, type, error, required, placeholder)`

```jinja2
{% from "macros/forms.html" import form_field %}
{{ form_field("query", "Ask a question", placeholder="e.g. What is the leave policy?", required=True) }}
```

Renders a `div.form-group` containing:
- `<label for="{name}">{label}</label>`
- `<input type="{type}" id="{name}" name="{name}" ...>`
- Optional `<span class="error-text">{error}</span>` when `error` is set
- Parent div gets `has-error` class when error is present

### `btn(text, variant, type, href, **kwargs)`

```jinja2
{% from "macros/buttons.html" import btn %}
{{ btn("Search", type="submit") }}
{{ btn("Dashboard", href="/", variant="secondary") }}
{{ btn("Delete", variant="danger", hx_post="/delete", hx_confirm="Are you sure?") }}
```

Renders `<button>` (default) or `<a>` (when `href` is set). Variants: `primary` (default), `secondary`, `danger`.

Extra keyword arguments are converted to HTML attributes with underscores replaced by hyphens — `hx_post` becomes `hx-post`. This is the HTMX integration pattern.

**Important:** Uses Jinja2's implicit `kwargs` variable, not `**attrs` in the signature (which is invalid Jinja2 syntax).

## 3. Base template blocks

| Block | Purpose | Where |
|---|---|---|
| `{% block title %}` | Page title | `<title>` |
| `{% block head %}` | Page-specific CSS/meta | `<head>`, after base.css |
| `{% block content %}` | Page body | `<main class="content">` |
| `{% block footer %}` | Page-specific scripts | Before `</body>` |

## 4. HTMX setup

- HTMX 2.0.4: `<script src="https://unpkg.com/htmx.org@2.0.4">`
- SSE extension: `<script src="https://unpkg.com/htmx.org/dist/ext/sse.js">`
- `hx-boost="true"` on `<body>` — intercepts navigation links for SPA-like experience

## 5. Test coverage

8 tests in `tests/unit/test_frontend_macros.py`:
- `form_field` renders label, input, error state, required attribute, placeholder
- `btn` renders primary/secondary/danger variants, HTMX attribute passthrough
- `base.html` loads SSE extension, has `{% block head %}`

## 6. CSS additions (base.css)

- `.form-group`, `.has-error`, `.error-text` — form field styling
- `.btn-secondary`, `.btn-danger` — button variants
- `.badge`, `.badge-admin` (blue), `.badge-owner` (amber) — role badges
