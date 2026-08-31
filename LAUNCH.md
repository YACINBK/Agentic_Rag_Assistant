# LAUNCH.md — how to start and test this project

**Demo day: use `DEMO.md`** — the modular walkthrough (one module per feature,
fresh-state reset, 3-minute fallback). This file is the ops runbook: cold start,
topology, stack verification, troubleshooting.

**Demo target: Thursday 2026-09-03.** Rewritten 2026-08-30 — the login path, the LLM
provider and the admin surface all changed since the 2026-08-26 version:

- **Real Keycloak login is the primary path.** `DEV_MODE=false` in `.env.local`.
  The realm imports from `deploy/keycloak/whitecape-realm.json`; four demo
  accounts, all password `whitecape`. The `/dev/login` shortcuts still exist as a
  fallback (§3.4).
- **LLM inference runs on OpenRouter** (`openai/gpt-oss-120b`) after the Ollama
  cloud weekly limit hit on 2026-08-30. Key in gitignored `.env.local`. Ollama on
  the host still serves **BGE-M3 embeddings only**.
- **M8b + M8c shipped:** delete and re-ingest buttons on every document card,
  event-driven list refresh, polling bootstrap on re-ingest.

Everything below is copy-pasteable from `/mnt/data/rag_assistant`.

> The one thing to know up front: **the real setup is not `docker compose up`.**
> Redis and Ollama run natively on this host. Docker runs Postgres, Qdrant, the
> reranker **and Keycloak**. Uvicorn and Celery run from the project venv.
> Starting everything in Docker would collide on ports 6379 and 11434 and fail.

---

## 0. Cheat sheet — full cold start

Four terminals, in this order.

```bash
# ── Terminal 1 — infrastructure ─────────────────────────────────────────────
cd /mnt/data/rag_assistant
sudo systemctl start redis-server                 # host Redis  :6379
sudo snap start ollama                            # host Ollama :11434 (bge-m3)
docker compose up -d postgres qdrant keycloak reranker
#    :5432  :6333  :8080  :8082

# ── Terminal 2 — migrations (once per schema change) ────────────────────────
cd /mnt/data/rag_assistant
set -a; . .env.local; set +a
./.venv/bin/alembic upgrade head

# ── Terminal 3 — Celery worker (leave running) ──────────────────────────────
cd /mnt/data/rag_assistant
set -a; . .env.local; set +a
./.venv/bin/celery -A app.tasks.worker worker --loglevel=info --concurrency=1

# ── Terminal 4 — web app (leave running) ────────────────────────────────────
cd /mnt/data/rag_assistant
set -a; . .env.local; set +a
./.venv/bin/uvicorn app.main:app --reload --port 8000
```

Port 8000 is load-bearing — the Keycloak client's redirect URI is pinned to
`http://localhost:8000/auth/callback`.

---

## 1. Why the two-line preamble matters

```bash
cd /mnt/data/rag_assistant
set -a; . .env.local; set +a
```

- **`.env` uses Docker service names** (`postgres`, `qdranta`…) because that is
  what containers resolve. Host tooling cannot resolve them and dies with
  `socket.gaierror [Errno -3] Temporary failure in name resolution`.
- **`.env.local` overrides each endpoint with its `localhost` port** and holds the
  OpenRouter key, the four `*MODEL` overrides, `DEV_MODE=false` and
  `DEV_FAKE_PIPELINE=false`. `settings.py` reads `('.env', '.env.local')`
  last-wins.
- **Never use system `python`/`pytest`/`alembic`/`celery`.** Always `./.venv/bin/…`.

---

## 2. What runs where — the actual topology

| Service | Where it runs | Port | Start command |
|---|---|---|---|
| PostgreSQL 16 | Docker | 5432 | `docker compose up -d postgres` |
| Qdrant 1.12.1 | Docker | 6333 / 6334 | `docker compose up -d qdrant` |
| Keycloak 24 | Docker | 8080 | `docker compose up -d keycloak` |
| Reranker (TEI, GPU) | Docker | 8082 | `docker compose up -d reranker` |
| Redis 7 | **host, systemd** | 6379 | `sudo systemctl start redis-server` |
| Ollama (bge-m3 embeddings only) | **host, snap** | 11434 | `sudo snap start ollama` |
| FastAPI (UI + API + SSE) | **host venv**, uvicorn | 8000 | see §0 terminal 4 |
| Celery worker (ingestion) | **host venv** | — | see §0 terminal 3 |
| LLM inference (classifier/rewriter/generator/enricher) | **OpenRouter** via LiteLLM | — | key in `.env.local` |

### Do NOT start these compose services

- **`redis`** — the container will never start while host Redis holds 6379.
- **`ollama`** — the snap owns 11434 and holds `bge-m3`. A container would start empty.
- **`backend` / `celery-worker` / `celery-beat`** — host venv with `--reload` is
  what you want in dev.

Beat (hourly cache TTL purge) is optional — the cache TTL is 24h and nothing
writes the cache yet (see §7).

---

## 3. Accounts and login — the real Keycloak path

| Account | Password | Becomes |
|---|---|---|
| `owner.demo` | `whitecape` | Owner + Admin **after seeding** (§4, Phase 2) |
| `admin.demo` | `whitecape` | Admin **after the owner grants it** (Phase 3) |
| `user.one` | `whitecape` | plain developer |
| `user.two` | `whitecape` | plain user — picks a role at first login |

Login URL: **`http://localhost:8000`** — the app's public **landing** page with a
**Login** button. Click it → Keycloak form → back to the app. First login for
any account lands on the **role picker** (M9c) — pick a primary role once; it
sticks (`role_source` flips to `self_selected` and the picker never appears
again for that account).

Three facts that save debugging time:

1. **Seed the owner AFTER first login, never before.** The seed script promotes an
   existing row by email; a pre-login insert would carry a random `keycloak_id`
   and the first real login would collide on the unique email. Order:
   login → `scripts/seed_owner.py --email owner@whitecape.fr` → **log out and back
   in** (the live session captured `is_admin=false` at login; only a fresh session
   sees the flag).
2. **Parallel sessions now work.** Real Keycloak gives every account its own
   session key — the old dev-mode clobbering (§3.4) does not apply here. Owner in
   one browser, `user.one` in another/incognito: both stay logged in. This is what
   makes the privilege demo (§4, Phase 6) parallel instead of sequential.
3. **PostgreSQL decides everything except identity.** Keycloak carries no roles;
   `is_admin`/`is_owner` live only in PostgreSQL (owner seed + the Users page).

### 3.4 Fallback: dev-mode login (only if Keycloak is broken mid-demo)

`.env.local` must say `DEV_MODE=true` (and uvicorn restarted) for these to exist:

| URL | Session |
|---|---|
| `/dev/login/admin` | developer + is_admin |
| `/dev/login?owner=true` | developer + admin + is_owner |
| `/dev/login?role=qa_engineer` | qa_engineer, no admin |
| `/dev/status` | shows the active session |

⚠ All dev logins write **one** Redis key (`session:dev-session-fixed`) — admin and
non-admin cannot coexist; comparisons must be strictly sequential in one tab.
Real Keycloak (§3) does not have this limitation.

---

## 4. Stack verification

Phase 0 is the gate — run it before anything. The feature-by-feature
walkthrough is **DEMO.md** (see the pointer at the end of this section).

### Phase 0 — stack health (2 minutes)

```bash
cd /mnt/data/rag_assistant && set -a; . .env.local; set +a

# Containers Up (not Exited)
docker compose ps postgres qdrant keycloak reranker

# All six ports listening
ss -ltn | grep -E ':(5432|6333|8080|8082|6379|11434)'

# App answers
./.venv/bin/python -c "import httpx;print(httpx.get('http://localhost:8000/health').json())"
#   expect {"status":"ok"}

# Keycloak imported the realm (200 = yes, 404/000 = see §7 troubleshooting)
./.venv/bin/python -c "import httpx;print(httpx.get('http://localhost:8080/realms/whitecape').status_code)"
#   expect 200

# Reranker loaded (first start downloads the model — can take minutes)
./.venv/bin/python -c "import httpx;print(httpx.get('http://localhost:8082/info').status_code)"
#   expect 200

# Embedding model present (the only Ollama model the pipeline needs)
ollama list | grep bge-m3

# Corpus state: rows + Qdrant point counts per document
./.venv/bin/python scratchpad/inspect_state.py
```

Expected corpus (as of 2026-08-30 evening):

| Document | Status | Chunks | Restricted |
|---|---|---|---|
| `askgo-621x-fr.html` | done | 477 | no |
| `whitecape-onboarding-fr.html` | done | 4 | no |
| `whitecape-remuneration-fr.html` | done | 4 | **yes** |

`whitecape-qualite-tests-fr.html` and `whitecape-deploiement-fr.html` are **not
ingested** — they are the live upload test in Phase 4.

### The feature walkthrough — see DEMO.md

Phases 1–9 (login → role picker → owner seed → admin grant → upload → search →
privilege demo → re-ingest/delete → logout) moved to **`DEMO.md`** — the
performance script. One module per feature, exact clicks, expected results,
timings, and a 3-minute fallback path. Before performing it:

```bash
./.venv/bin/python scripts/reset_demo_state.py --apply
```

restores first-login state for every account (role picker returns, the M9d
grant is performable again, all sessions flushed). Owner flags are never
touched — the seed survives by design.

---

## 5. Demo queries and ingestability

Pre-screened queries and the demo-doc content map live in **DEMO.md** — ask
those, not improvisations: generic phrasings can score under the relevance
gate and decline.

What will NOT ingest (operational, kept here):

- **PDFs** — `extract.py` is HTML-only; a PDF goes to `failed` with 0 chunks.
- **HTML without BookStack markers** — the chunker reads `id="chapter-*"` /
  `id="page-*"` / `id="bkmrk-*"` and ignores heading levels. Ordinary HTML
  chunks to `[]` and the row is marked `done` with `chunk_count = 0` — a
  silent success.

Check a new HTML file before uploading (offline, safe):

```bash
./.venv/bin/python scratchpad/verify_demo_chunking.py
```


## 6. Shutting down

```bash
# Ctrl-C the uvicorn and celery terminals first, then:
docker compose stop postgres qdrant keycloak reranker
```

Use `stop`, **not** `down`. `down -v` would delete the `pg_data` and `qdrant_data`
volumes — the corpus and the whole database.

---

## 7. Troubleshooting — failures actually hit on this machine

### Keycloak answers `000` / never comes up

Check the log first — it separates two very different failures:

```bash
docker compose logs keycloak | tail -20
```

- **`ERROR: Unrecognized field "_comment"`** → the realm file was damaged. This
  exact failure happened 2026-08-30 (D44): Keycloak 24 rejects unknown
  properties at every level of the realm JSON, so the file carries **no** comment
  key at all — the plaintext-secret warning lives in `deploy/keycloak/README.md`.
  If someone re-adds a `_comment`, delete it.
- **Started fine but `/realms/whitecape` is 404** → stale `keycloak_data` volume:
  `--import-realm` skips realms that already exist. Force a re-import — surgically,
  never `down -v` (that wipes postgres + qdrant):
  ```bash
  docker compose rm -sf keycloak
  docker volume rm rag_assistant_keycloak_data
  docker compose up -d keycloak
  ```
  Re-imports are safe: every realm user carries a pinned `id` (see
  `deploy/keycloak/README.md`), so subs never change between imports. They used
  to change — and a fresh sub orphaned every existing PostgreSQL user row,
  breaking login with a 500 on the email unique constraint. The app-side
  `_lazy_sync_user` email fallback relinks a stale row on next login regardless.

Give it ~30–60 s to boot before judging; then
`curl http://localhost:8080/realms/whitecape` must return 200.

### Login 401s right after the Keycloak form ("Authentication required")

The 2026-08-30 fixes (D45) are already committed — if this returns, the app is
running old code. Restart uvicorn. Two historical causes, both fixed in
`5da4b5c`: a PKCE verifier read from a non-existent authlib attribute, and a
missing `scope=openid` (Keycloak issues the token without `openid` and userinfo
returns 403 — the login dies *after* a successful token exchange).

### Ingestion `failed` at Stage C / any LLM call fails

**Check the OpenRouter quota first.** This is the replacement for the Ollama
cloud weekly limit that killed all `-cloud` models on 2026-08-30:

```bash
set -a; . .env.local; set +a
./.venv/bin/python -c "
import asyncio
from app.services.llm import LiteLLMService
from app.core.settings import settings
async def p():
    try:
        print('OK:', (await LiteLLMService().complete(
            settings.GENERATOR_MODEL,
            [{'role':'user','content':'Dis: bonjour'}]))[:60])
    except Exception as e:
        print('FAIL:', str(e)[-200:])
asyncio.run(p())"
```

If it fails with a quota/credit error, that is the whole story — no code defect.
There is no local fallback model pulled; the committed `.env` defaults to
`ollama/qwen2.5:*` which are **not** on the host.

### Search fails with "Reranker failed: ConnectError"

The TEI container is down or still loading (it downloads the model on first
start; GPU image warms in ~15 s after the first ever start):

```bash
docker compose up -d reranker && docker compose logs -f reranker
```

### Everything was up, now nothing responds

The Docker daemon restarts itself on package updates and no compose service has a
`restart:` policy — the stack stays down silently. Check with `docker ps -a`, not
`docker ps` (stale cache claims "Up 5 hours" for exited containers). Container
logs are UTC; this host is CET (+2).

### Uploaded document stays `pending`

The Celery worker is not running, or is running without `.env.local` sourced (it
resolves `redis`/`postgres` as Docker hostnames and fails). Restart terminal 3.
There is no error and no timeout — this is the single most likely thing to go
wrong.

### Every answer is about "25 days of paid annual leave"

`DEV_FAKE_PIPELINE` is on. `echo $DEV_FAKE_PIPELINE` must be `false`, and
`.env.local:DEV_FAKE_PIPELINE=false` must be in effect (source it, restart
uvicorn).

### `socket.gaierror` / `ModuleNotFoundError`

You skipped the preamble, or used system Python. `set -a; . .env.local; set +a`,
then `./.venv/bin/…`.

### Answers arrive with no citations / "insufficient information"

The relevance gate (0.5) or faithfulness checker (0.5) rejected the result — the
system declines rather than guessing. Ask a pre-screened phrasing (§5).

### Do not demo the semantic cache

Nothing writes `semantic_cache` yet (M10 is the writer) — the collection holds 0
points. Every query takes the full pipeline path; there is no cache-hit path to
show. Do not promise one.

### Disk

Docker images and TEI/HF caches live on `/`. Check before the demo:

```bash
df -h / /mnt/data
```

---

## 8. Port map

| Port | Service | Notes |
|---|---|---|
| 8000 | FastAPI — UI, API, SSE | host venv uvicorn; realm redirectUri pinned to it |
| 5432 | PostgreSQL | Docker |
| 6333 / 6334 | Qdrant HTTP / gRPC | Docker |
| 8080 | Keycloak | Docker, realm `whitecape` |
| 8082 | Reranker (TEI) | Docker, GPU image `89-latest` |
| 6379 | Redis | **host** systemd |
| 11434 | Ollama | **host** snap — bge-m3 embeddings only |

---

## 9. Useful one-liners

```bash
cd /mnt/data/rag_assistant && set -a; . .env.local; set +a

# Corpus state: document rows + Qdrant point counts per document
./.venv/bin/python scratchpad/inspect_state.py

# Will this HTML file chunk? (offline, safe)
./.venv/bin/python scratchpad/verify_demo_chunking.py

# SQL shell
docker compose exec -T postgres psql -U whitecape -d whitecape

# Qdrant collections
./.venv/bin/python -c "import httpx;print(httpx.get('http://localhost:6333/collections').json())"

# Points belonging to one document
./.venv/bin/python -c "import httpx;print(httpx.post('http://localhost:6333/collections/documents/points/count',json={'filter':{'must':[{'key':'document_id','match':{'value':'<DOC_UUID>'}}]},'exact':True}).json())"

# Remove test documents from every store (dry-run by default)
./.venv/bin/python scripts/cleanup_documents.py            # shows the plan
./.venv/bin/python scripts/cleanup_documents.py --apply    # executes

# Full test suite (integration needs postgres up; 348+ passing as of 2026-08-30)
./.venv/bin/pytest tests/ -q

# Ruff
./.venv/bin/ruff check app tests scripts
```

---

## 10. Pre-demo checklist

- [ ] `df -h /` shows more than ~5 GB free
- [ ] `sudo systemctl start redis-server` · `sudo snap start ollama` · `ollama list` shows `bge-m3`
- [ ] `docker compose up -d postgres qdrant keycloak reranker`
- [ ] `ss -ltn` shows 5432, 6333, 8080, 8082, 6379, 11434
- [ ] `http://localhost:8080/realms/whitecape` → 200
- [ ] `http://localhost:8082/info` → 200 (reranker loaded)
- [ ] **OpenRouter probe passes** (§7 one-liner) — quota is the single external dependency
- [ ] `./.venv/bin/alembic upgrade head` clean
- [ ] Celery worker running, `.env.local` sourced
- [ ] Uvicorn running on **8000**, `.env.local` sourced, `echo $DEV_FAKE_PIPELINE` is `false`
- [ ] Owner seeded (`is_owner=t is_admin=t`) and re-login shows Documents + Users
- [ ] Demo docs all `done` with **non-zero** `chunk_count`; `whitecape-remuneration-fr.html` present and **restricted**
- [ ] `./.venv/bin/python scripts/reset_demo_state.py --apply` run — every account back to first-login state
- [ ] One pre-screened query answered **with citations**
- [ ] Restricted-doc check: admin answered, `user.one` declined (two browsers, parallel)
- [ ] One Re-ingest click watched through pending → running → done (list self-refreshing)
