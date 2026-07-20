# Progress — Sprint 1

**Sprint:** Week 1 — Pipeline Foundation
**Dates:** 2026-07-20 → 2026-07-25
**Goal:** conftest verified + Node 01 (Classifier) verified + Node 02 (Rewriter) verified

---

## Current state

| Item | Status | Notes |
|---|---|---|
| `contracts/conftest.md` | WRITTEN | Contract defines stage factories + mock patterns |
| `tests/conftest.py` | UPDATED | Stage factories added (post_classifier, post_retrieval, post_rerank, post_generation) |
| `contracts/node_01_classifier.md` | WRITTEN | 8 assertions, 5 test cases, binary DIRECT/SIMPLE_RAG |
| Node 01 implementation | NEXT | Run `./run_loop.sh node_01_classifier` |
| Node 02 contract | PENDING | Write after Node 01 is VERIFIED |

---

## Workflow reminder

```bash
# Create feature branch
git checkout develop && git pull
git checkout -b feature/node-01-classifier

# Run the loop
./run_loop.sh node_01_classifier

# Check result
tail -1 log.md

# If PASS: squash-merge
git checkout develop
git merge --squash feature/node-01-classifier
git commit -m "Node 01: Classifier — binary DIRECT/SIMPLE_RAG classification"
git push origin develop
git branch -d feature/node-01-classifier
```

---

## Blockers

None.
