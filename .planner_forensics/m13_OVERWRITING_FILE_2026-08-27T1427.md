<!--
PRESERVED BY THE PLANNER, 2026-08-28.

This is the verbatim content that was found at contracts/m13_keycloak_realm_import.md
on 2026-08-28. It is NOT the M13 contract that was planned or implemented, and it has
been moved here rather than deleted.

WHY IT IS NOT THE REAL CONTRACT — four independent records disagree with it:

  1. log.md:555 (append-only, written at plan time, BEFORE implementation) records
     "m13_keycloak_realm_import — 10 assertions, 7 test cases ... Identity-only realm
     file ... The assertions prove file shape and compose wiring ONLY; a working login
     is a MANUAL checklist and must be reported as such."
     The file below has 8 assertions, 6 test cases, and a Forbidden item reading
     "No Manual Console Steps: The process must be entirely programmatic" — the
     direct opposite.

  2. reviews/m13_keycloak_realm_import_summary.md:3 (the Generator, written 2026-08-27
     13:09 while the real contract was still on disk) states: "Contract:
     contracts/m13_keycloak_realm_import.md (10 assertions, 7 test cases, 9 Forbidden
     items)". The file below has 8 assertions, 6 test cases, 3 Forbidden items.

  3. feature_list.json:140 records the same declarative design: "Produces
     deploy/keycloak/whitecape-realm.json plus 'start-dev --import-realm' and a
     read-only bind mount under /opt/keycloak/data/import/".

  4. tests/unit/test_m13_keycloak_realm.py:3 declares "Contract:
     contracts/m13_keycloak_realm_import.md, test cases 1-7" and implements exactly
     the ten assertions the Generator's summary tabulates.

FILESYSTEM EVIDENCE:

  - mtime 2026-08-27 14:27, size 3037 bytes.
  - Every sibling contract written in the same planning session is 11-12 KB:
    m9b 12063 @ 2026-08-26 18:28, m9c 12201 @ 18:36, m9d 11472 @ 18:37.
  - The Generator's M13 summary is dated 2026-08-27 13:09 — this file postdates the
    completed implementation by 78 minutes.
  - contracts/ directory mtime is still 2026-08-26 18:39. A create/delete/rename
    updates a directory's mtime; an in-place overwrite does not. So this content
    replaced the real contract under the same filename.

WHO AND WHEN CANNOT BE ESTABLISHED. contracts/ is gitignored (.gitignore:46), so git
holds no history for it, and the sha256 baseline taken before briefing the Generator
was lost when /tmp was cleared. This is finding D38 realised, and the reason D38's fix
(a durable baseline, not one in /tmp) is now mandatory rather than advisory.

UNIMPLEMENTABLE AS WRITTEN, independently of the above: it requires
settings.KEYCLOAK_ADMIN_URL, settings.KEYCLOAK_ADMIN_USER and
settings.KEYCLOAK_ADMIN_PASSWORD. None of the three exists in app/core/settings.py,
which declares only KEYCLOAK_URL, KEYCLOAK_REALM, KEYCLOAK_CLIENT_ID and
KEYCLOAK_CLIENT_SECRET.

Kept because it may be a draft of a genuine post-MVP idea — a programmatic Admin-API
realm importer is a reasonable thing to want once the realm outgrows a committed JSON
file. It is not what M13 is, and it must not be handed to the Evaluator.
-->

# Contract — M13: Keycloak Realm Import
Status: DRAFT
Last verified: —

## Interface
`async def import_realm(realm_file_path: Path) -> RealmImportResult`
Programmatically imports a Keycloak realm configuration from a JSON file into the target Keycloak instance.

## Inputs
- `realm_file_path`: Path to the JSON export file. Must exist and be valid JSON. If the file is missing or corrupt, raise `RealmImportError`.

## Outputs
`RealmImportResult` (Dataclass):
- `status`: `"success"` or `"partial_success"`.
- `realm_created`: `bool` (True if the realm was created from scratch).
- `client_updated`: `bool` (True if the OIDC client was updated/verified).
- `errors`: `list[str]` (Any non-fatal warnings or skipped fields).

## Environment
- **External Service:** Keycloak Admin API (via `settings.KEYCLOAK_ADMIN_URL`).
- **Authentication:** Uses `settings.KEYCLOAK_ADMIN_USER` and `settings.KEYCLOAK_ADMIN_PASSWORD` to obtain an admin token.
- **Verification:** Reads `settings.KEYCLOAK_REALM`, `settings.KEYCLOAK_CLIENT_ID`, and `settings.KEYCLOAK_CLIENT_SECRET` to verify the imported realm matches the application config.
- **Tests:** Use a `MockKeycloakAdmin` double that records API calls.

## Assertions
1. **File Integrity:** The function reads the JSON file and validates it contains at least the `realm` and `clients` keys.
2. **Admin Auth:** The service successfully authenticates with the Keycloak Admin API before attempting the import.
3. **Idempotent Import:** If the realm already exists, the import updates the configuration rather than failing or creating a duplicate.
4. **Client Alignment:** The imported client's `clientId` must exactly match `settings.KEYCLOAK_CLIENT_ID`.
5. **Secret Alignment:** The imported client's `secret` must exactly match `settings.KEYCLOAK_CLIENT_SECRET`.
6. **Identity-Only:** The import does not overwrite existing users in the realm if they are already present (handled by Keycloak's native `OVERWRITE` vs `MERGE` strategy).
7. **Error Propagation:** Any HTTP 4xx/5xx from the Keycloak Admin API is wrapped in a `RealmImportError`.
8. **CLI Integration:** The `scripts/import_realm.py` tool correctly loads `.env.local`, calls the service, and prints a human-readable summary.

## Forbidden
- **No Hardcoded Secrets:** Admin credentials must come from `settings`.
- **No Manual Console Steps:** The process must be entirely programmatic.
- **No Direct Shell Calls:** Do not use `subprocess` to call `curl` or `wget`; use `httpx`.

## Test cases
1. **Happy Path:** Valid JSON $\rightarrow$ New Realm $\rightarrow$ `status="success"`.
2. **Existing Realm:** Valid JSON $\rightarrow$ Existing Realm $\rightarrow$ `status="success"` (Updated).
3. **Config Mismatch:** Valid JSON $\rightarrow$ Client ID in file differs from `.env` $\rightarrow$ `RealmImportError`.
4. **Corrupt File:** Non-JSON file $\rightarrow$ `RealmImportError`.
5. **Auth Failure:** Wrong Admin Password $\rightarrow$ `RealmImportError`.
6. **Missing File:** Path does not exist $\rightarrow$ `FileNotFoundError`.
