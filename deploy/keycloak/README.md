# Whitecape local demo realm — identity only

Imported by `start-dev --import-realm` from `whitecape-realm.json` in this directory.

**THE CLIENT SECRET IS COMMITTED IN PLAINTEXT.** That is acceptable for a realm
that only ever runs on a developer machine, and is NOT acceptable for anything
reachable from outside it. Rotating it means editing `whitecape-realm.json` and
`.env` in the same commit — `tests/unit/test_m13_keycloak_realm.py` asserts the
two are equal, so a mismatch fails the suite instead of failing silently at
login. The seeded passwords are demo credentials for the same reason: all four
accounts use `whitecape`.

**THIS REALM CARRIES NO AUTHORIZATION.** There is no `roles` key, no realmRoles
or clientRoles on any user, and no protocol mapper. PostgreSQL is authoritative
for a user's primary role and Keycloak proves identity and nothing else
(CLAUDE.md §2 MVP amendment, D40). Adding a role here would not grant anything —
nothing reads it — but it would invite the next person to start.

The four accounts are identities, not privilege levels. `is_admin` and
`is_owner` live only in PostgreSQL: `is_owner` comes from the deployment seed,
`is_admin` from the owner acting through the admin UI. Logging all four in
produces four ordinary users; seeding the flags afterwards is a separate step
this file does not perform.

    owner.demo    → intended to become is_owner = true (and is_admin with it)
    admin.demo    → intended to become is_admin = true
    user.one      → stays flagless
    user.two      → stays flagless

`sslRequired` is `none` because the demo is served over plain http on localhost.
Keycloak's default `external` would refuse a non-localhost http origin, which is
what an intranet deployment (CLAUDE.md §13) would hit. Do not carry `none` into
any deployment that terminates TLS elsewhere.

> **Why no `_comment` in the JSON:** Keycloak 24's `RealmRepresentation` (and
> nested beans such as `CredentialRepresentation`) fail hard on unknown
> properties — a top-level `_comment` makes `--import-realm` abort before the
> realm exists. The warning therefore lives here, and the M13 test asserts this
> file carries it.

**Every user carries a pinned `id`.** Without one, each `--import-realm` into a
fresh volume mints a NEW sub for every account — and since the app matches
PostgreSQL rows by `keycloak_id`, every existing row is orphaned at once and
login dies on the email unique constraint (observed live 2026-08-31 after two
re-imports). The pinned ids make re-imports idempotent:

    owner.demo  00000000-0000-4000-8000-0000000000a1
    admin.demo  00000000-0000-4000-8000-0000000000a2
    user.one    00000000-0000-4000-8000-0000000000a3
    user.two    00000000-0000-4000-8000-0000000000a4

The app keeps a second line of defence regardless: `_lazy_sync_user` falls back
to an email match and relinks the stored `keycloak_id`, so even an unpinned sub
heals on the user's next login instead of failing it.
