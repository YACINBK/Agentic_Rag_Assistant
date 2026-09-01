# DEMO.md — the modular walkthrough

**Demo day: Thursday 2026-09-03.** Each module shows ONE feature, in dependency
order, ~11 minutes total. LAUNCH.md stays the ops runbook (cold start, stack
verification, troubleshooting) — this file is the performance script.

## Before you start (5 minutes earlier)

```bash
cd /mnt/data/rag_assistant && set -a; . .env.local; set +a

# 1. Reset every feature to its first-time state (picker returns, grant is live)
./.venv/bin/python scripts/reset_demo_state.py --apply

# 2. Stack health — the six ports + realm + reranker + OpenRouter probe
#    (LAUNCH.md §4 Phase 0 — run it verbatim)
```

State after the reset (this is what each module expects):

| Account | role_source | is_admin | First login shows |
|---|---|---|---|
| `owner.demo` / `whitecape` | default | **true** (seeded — never reset) | role picker |
| `admin.demo` / `whitecape` | default | **false** — granted live in M2 | role picker |
| `user.one` / `whitecape` | default | false | role picker |
| `user.two` / `whitecape` | default | false | role picker |

Corpus before the room: `askgo` (477 chunks), `onboarding`, `qualite-tests` —
all `done` and **unrestricted**; `remuneration` is `done` and **restricted**
(the §5 demo's only restricted document). `deploiement` is **deleted before the
room** — it is the live upload in M3 (delete it the day before with the
Documents page's Delete button if it's present). Verify — a wrongly ticked
*restricted* box on upload silently turns plain-user queries into declines,
exactly like the §5 demo (found 2026-09-01: deploiement had been uploaded
restricted and every "any user" technical query died):

```bash
docker compose exec -T postgres psql -U whitecape -d whitecape -t -A -c \
  "SELECT original_filename, ingestion_status, restricted FROM document ORDER BY 1;"
#   expect before the room: askgo/onboarding/qualite | f, remuneration | t, no deploiement
#   expect after M3: deploiement | done | f as well
```

Browser setup: **Browser A** = owner.demo. **Browser B** (incognito or a second
browser) = user.one from M5 on.

---

## M0 — The landing page (30 s)

**SAY:** "This is the internal knowledge assistant — the entry point is the app
itself, public, with a single Login button. No separate login screen."

**DO:** Open `http://localhost:8000` in a fresh/incognito window.
**EXPECT:** Whitecape branding + one **Login** button. No redirect, the word
Keycloak appears nowhere.

---

## M1 — Login + the first-login role picker (1 min)

**SAY:** "Keycloak proves identity — that's all it does. Roles live in
PostgreSQL, and a first-time user picks their own once."

**DO (Browser A):** Click **Login** → `owner.demo` / `whitecape` → the **role
picker** appears → pick **developer**.
**EXPECT:** Picker → dashboard ("Welcome, owner@whitecape.fr"), nav shows
**Documents** and **Users**.

**Beat 2 — the picker is one-time:** log out, log back in → straight to the
dashboard, no picker.

---

## M2 — The owner's surfaces + a live admin grant (1.5 min)

**SAY:** "Admin is a flag on top of a role, not a role itself — and only the
owner can grant it."

**DO (Browser A):** **Users** page → find `admin.demo` → **grant admin**.
**EXPECT:** The row flips to admin. (The change also purges the target's live
sessions — nothing stale keeps old privileges.)

**DO (Browser B):** Log in as `admin.demo` / `whitecape` → pick a role at the
picker → dashboard.
**EXPECT:** Nav shows **Documents** but **not** Users — admin, not owner.
Visit `http://localhost:8000/admin/users` in B directly → **403**.

**If asked "how do accounts get created?"** — the honest answer, and it is the
designed one: an account is created in **Keycloak** (the realm), which holds
identity only. The person receives their credentials and logs in; the
PostgreSQL row is created at that first login with the default role, and the
role decision happens right there — either the person picks it (the picker you
just saw) or the owner pre-empts the picker by assigning through this Users
page (`role_source` becomes `admin_assigned`, the picker never shows for that
account). So account creation does NOT override the picker — it is the picker's
trigger. One ordering nuance to know: the owner cannot assign a role to an
account that has never logged in (there is no row to assign to yet) — lazy
sync, no webhooks, by design.

---

## M3 — Upload + the ingestion machine (2 min)

**SAY:** "Upload returns immediately — the work happens in a Celery worker
through five stages: extract, chunk, enrich, index, and cache invalidation."

**DO (Browser A or B):** **Documents** → upload `demo_docs/whitecape-deploiement-fr.html`,
category *technical*, **not** restricted → submit.
**EXPECT:** Row appears `pending` → the list refreshes itself and polls while
`running` → **`done`, 4 chunks** in ~15 s (first ingest of the day pays a ~6 s
tokenizer load — if M3 is the first ingest, say "the first one loads the
embedding model" while it runs).

**While it runs, SAY:** the status machine — `pending` means queued, `running`
means the worker holds it, `done` means Qdrant holds the points.

---

## M4 — The search pipeline (2–3 min)

**SAY:** "Every answer is grounded in indexed documents — streamed with the
citations that prove it."

**DO (Browser A):** Search page, ask:

> **Comment generer une demande de reapprovisionnement ?**

**EXPECT:** progress lines → streamed French answer with inline citation
pills → source card. ~10–30 s (first query after boot is the slow one — warm
it up before the room if you can). This is the flagship query: the Ask&Go
corpus, the richest citations, and it has answered on every verified run.

**Beat 2 — repeat it:** ask the exact same question again → **~1 second**,
same citations, same images — the semantic cache (M10). One question, two
features.

**Beat 3 — it refuses to invent:** ask *"Quelle est la politique de télétravail
chez Whitecape ?"* → **declined** ("insufficient information") — that's the
honesty boundary, not a failure.

More pre-screened queries (all verified) are in the table at the end.

---

## M5 — The security boundary (1.5 min) — the strongest module

**SAY:** "Restricted documents are filtered inside the vector search itself —
the model never sees them, so it cannot leak them. Two browsers, same question."

**DO (Browser A — owner):** ask *"Quelle est la grille salariale pour la filière
développement ?"* → full answer with citations.
**DO (Browser B — user.one, logged in at M2 or fresh):** same question →
**declined**.

**EXPECT:** A answered, B declined — same question, same corpus, different
privilege. If B is fresh: the picker appears once (pick **qa_engineer** — roles
are a lookup table, not hardcoded).

---

## M6 — Admin mutations, now with buttons (1.5 min)

**SAY:** "Everything an admin changes is announced to the UI — the list keeps
itself current."

**DO (Browser A):** on `whitecape-onboarding-fr.html` → **Re-ingest**.
**EXPECT:** No navigation; the card flips to `pending`/`running` **by itself**
(the 202 carries an `HX-Trigger` event); the list polls every 3 s; the
Re-ingest button **disappears** while in flight and **returns** at `done`
(~40 s). Say the 409 story: clicking re-ingest mid-run is refused server-side.

**DO:** on `whitecape-qualite-tests-fr.html` → **Delete** → confirm (the dialog
names the file) → the card vanishes without a refresh.
**EXPECT:** gone from the list; the database row, the stored file and the 4
Qdrant points all removed.

*(Re-upload qualite-tests afterwards if you want the corpus whole.)*

---

## M7 — Logout (30 s)

**SAY:** "Logout is a full round-trip: app session, cookie, and the Keycloak
SSO session."

**DO (Browser A):** **Logout** → Keycloak asks "Do you want to log out?" →
click **Logout**.
**EXPECT:** back on the **landing** page. Done.

---

## Pre-screened queries — re-verified 2026-09-01 under the live stack

The 2026-08-26 table died with the model switch: the rewriter is an LLM, so
boundary queries flip around the relevance gate. Everything below was re-run
**twice on the full path** (cache purged between rounds) plus the earlier
screening — only queries that answered every observed run are listed as safe.

**The demo set — ask these:**

| Ask this | As | Result (observed) | Module |
|---|---|---|---|
| Comment generer une demande de reapprovisionnement ? | any | answered, every run — flagship | M4 |
| *(same question again)* | any | **~1 s cache hit**, citations intact | M4 beat 2 |
| Quelle est la politique de télétravail chez Whitecape ? | any | **declined** — refuses to invent | M4 beat 3 |
| Quelle est la grille salariale pour la filière développement ? | **admin** | answered, every run | M5 |
| Quelle est la grille salariale pour la filière développement ? | **plain** | **declined** — the §5 demo | M5 |
| À combien s'élève la prime de cooptation ? | **admin** | answered, every run | M5 backup |
| À combien s'élève la prime de cooptation ? | **plain** | **declined** | M5 backup |
| Quelle couverture de test est attendue ? | any | answered, every run | extra |
| Quels sont les niveaux de gravité des anomalies et les délais de correction ? | any | answered, every run | extra |

**Backup tier** (answered 3 of 4 observed runs — one flip; usable only
AFTER M3's live upload re-ingests deploiement):
*Quelles sont les étapes de la procédure de déploiement ?*

**Do NOT ask** — verified dead under the current models (declined on every
observed run; all were "answered" in the old table): *congés par an*, *badge
d'accès*, *port de PostgreSQL*, *retour arrière*, *Sur quels ports écoutent les
services ?*. The rewrite nondeterminism sits these right on the relevance gate.
If you want one of them anyway, warm it up out of sight first — a success
caches it for 24 h.

**Cache hygiene for demo day:** the semantic cache TTL is 24 h. Entries written
today expire before Thursday — run each demo query once out of sight right
before the room (that also pre-warms the slow first-boot path), then the
in-room repeats are the 1-second hits.

## If everything is on fire — the 3-minute minimum

1. **M0** landing (30 s)
2. **M1** owner login + picker (1 min)
3. **M4** one pre-screened query with citations (1 min)

That trio alone shows: real Keycloak auth, PostgreSQL role authority, and a
grounded, cited RAG answer. Everything else is upside.

## Demo-doc contents (if asked)

onboarding = HR facts (25 jours de congés, badge 7h–20h, étages 1–3) ·
deploiement = port map, 5-step deploy, rollback thresholds · qualite = test
levels, severities, acceptance criteria · remuneration = salary grid — **the
restricted one**.
