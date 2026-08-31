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

Corpus: `askgo` (477 chunks), `onboarding`, `remuneration` (**restricted**),
`qualite-tests` — all `done`. `deploiement` is **not ingested** on purpose: it
is the live upload in M3.

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

> **Quelles sont les étapes de la procédure de déploiement ?**

**EXPECT:** progress lines → streamed French answer with inline `【N】`
citations → source card. ~20–60 s (first query after boot is the slow one —
warm it up before the room if you can).

**Beat 2 — it refuses to invent:** ask *"Quelle est la politique de télétravail
chez Whitecape ?"* → **declined** ("insufficient information") — that's the
faithfulness checker, not a failure.

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

## Pre-screened queries (verified)

| Ask this | As | Result |
|---|---|---|
| Quelles sont les étapes de la procédure de déploiement ? | any | answered, 12 cites |
| Comment generer une demande de reapprovisionnement ? | any | answered, cited (Ask&Go corpus) |
| Combien de jours de congés par an et quel préavis pour une demande ? | any | answered, 3 cites |
| À quelles heures le badge d'accès fonctionne-t-il et pour quels étages ? | any | answered, 2 cites |
| Quel port utilise la base de données PostgreSQL ? | any | answered, 1 cite |
| Quand déclenche-t-on un retour arrière et en combien de temps ? | any | answered, 2 cites |
| Quels sont les niveaux de gravité des anomalies et les délais de correction ? | any | answered, 2 cites |
| Quelle couverture de test est attendue ? | any | answered, 3 cites |
| Quelle est la grille salariale pour la filière développement ? | **admin** | answered, 6 cites — M5 |
| Quelle est la grille salariale pour la filière développement ? | **plain** | **declined** — M5 |
| À combien s'élève la prime de cooptation ? | **admin / plain** | answered / declined |
| Quelle est la politique de télétravail chez Whitecape ? | any | **declined** — refuses to invent |

**Do not ask** *"Sur quels ports écoutent les services ?"* — that phrasing
scores under the relevance gate. Name a specific thing, don't ask generically.

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
