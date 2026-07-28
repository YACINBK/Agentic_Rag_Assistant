# Search Frontend — Templates + Components

**File:** `app/templates/pages/search.html`, `partials/`, `components/`, `app/static/css/pages/search.css` · **LLM:** none · **External services:** none

---

## 1. Role

The visual layer for the search feature. Templates render the search page,
SSE connection container, answer bubbles, and source citations. All markup
is server-rendered — components are stateless Jinja2 includes, data comes
from template context set by the route handlers.

## 2. Template structure

### `pages/search.html`

Extends `base.html`. Loads `search.css` via `{% block head %}`. Includes
`partials/search_page_content.html` in the content block.

### `partials/search_page_content.html`

Contains the page heading and includes `components/search_bar.html`. Has
`<div id="search-results">` as the HTMX swap target for search results.

### `partials/search_results.html`

Returned by `POST /search`. Sets up the SSE connection:
- `hx-ext="sse"` on the container div
- `sse-connect="/search/stream?qid={{ qid }}"` opens the SSE endpoint
- User query displayed in `div.query-bubble.message-user`
- Four `sse-swap` divs: `progress`, `answer`, `error`, `done`

Does NOT render the answer inline — the answer arrives via the SSE `answer`
event as a pre-rendered `message_bubble.html` fragment.

### `components/search_bar.html`

Self-contained search form. Imports `form_field` and `btn` macros.

```html
<form hx-post="/search/" hx-target="#search-results" hx-swap="innerHTML">
    {{ form_field("query", "Ask a question", ...) }}
    {{ btn("Search", type="submit") }}
</form>
```

### `components/message_bubble.html`

Renders the assistant's answer. Receives `answer` (str) and `sources` (list[str]).

- `div.message-bubble.message-assistant` wrapper
- Answer text in `div.message-content` (white-space: pre-wrap for formatting)
- Sources section: iterates `sources`, includes `source_card.html` per item
- Sources hidden when list is empty

### `components/source_card.html`

Displays a single source filename as a styled inline span.
Receives `source` as a plain string (not a dict).

## 3. CSS (`static/css/pages/search.css`)

| Selector | Purpose |
|---|---|
| `.search-page h1` | Page heading spacing |
| `.search-bar` | Form max-width constraint |
| `.message-user` | User message: blue background, white text, right-aligned |
| `.message-assistant` | Assistant message: white background, grey border, left-aligned |
| `.message-content` | Answer text: line-height 1.6, pre-wrap whitespace |
| `.sources-label` | Uppercase "SOURCES" label |
| `.sources-list` | Flexbox wrap for source cards |
| `.source-card` | Grey pill with filename |
| `.progress` | Italic grey progress text |
| `.error-container` | Red-tinted error box |

## 4. Data flow

```
POST /search → search_results.html (qid, query)
    → Browser opens SSE to /search/stream?qid={qid}
        → Server sends: event: progress → <div class="progress">...</div>
        → Server runs pipeline
        → Server renders message_bubble.html(answer, sources) → string
        → Server sends: event: answer → rendered HTML string
        → Server sends: event: done → ""
    → HTMX swaps each event's data into its sse-swap target div
```

## 5. Test coverage

6 tests in `tests/unit/test_search_frontend.py`:
- Search page has form with `hx-post`, has `#search-results` target, loads search.css
- Search results partial has SSE connection attributes
- Search results shows user query with `message-user` class
- Message bubble renders answer with `message-assistant` class + source filenames
