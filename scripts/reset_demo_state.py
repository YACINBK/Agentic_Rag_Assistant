"""Reset the demo state so every feature can be shown from its first-login moment.

Run this before a rehearsal or the demo itself. It restores exactly the state
the feature modules expect, without touching documents or the corpus:

  1. role_source -> 'default' for the four realm accounts — the first-login
     role picker (M9c) appears again on each account's next login.
  2. admin.demo loses is_admin — the owner can grant it live through the Users
     page (M9d) during the walkthrough.
  3. Every Redis session is flushed — no stale sessions, no half-consumed
     first-login state, every login starts clean.

It NEVER touches the owner account's flags (is_owner / is_admin): §2/§12 — the
owner is deployment-seeded, not manageable by any user or script action, and the
seed's whole point is that it survives.

Usage (from the repo root, .env.local sourced):
    set -a; . .env.local; set +a
    ./.venv/bin/python scripts/reset_demo_state.py           # show the plan
    ./.venv/bin/python scripts/reset_demo_state.py --apply   # execute it
"""

import asyncio
import argparse
import sys

from sqlalchemy import select, update

from app.core.models.base import async_session
from app.core.models.user import ROLE_SOURCE_DEFAULT, User
from app.core.settings import settings

# The four realm identities (deploy/keycloak/whitecape-realm.json). Anything
# else in the table is dev-era seed data and is left alone.
REALM_ACCOUNTS = (
    "owner@whitecape.fr",
    "admin@whitecape.fr",
    "user.one@whitecape.fr",
    "user.two@whitecape.fr",
)

# The account whose admin flag is reset so the grant can be performed live.
GRANT_DEMO_TARGET = "admin@whitecape.fr"


async def plan() -> list[tuple[str, str, str, str]]:
    async with async_session() as session:
        users = (
            (
                await session.execute(
                    select(User.email, User.role_source, User.is_admin, User.is_owner)
                    .where(User.email.in_(REALM_ACCOUNTS))
                    .order_by(User.email)
                )
            )
            .all()
        )
        return [(u.email, u.role_source, u.is_admin, u.is_owner) for u in users]


async def apply(reset_admin: bool) -> None:
    from app.core.models.role import Role

    async with async_session() as session:
        # The born-default role every account gets at first login.
        role = (
            (
                await session.execute(
                    select(Role).where(Role.name == settings.DEFAULT_ROLE)
                )
            )
            .scalar_one_or_none()
        )
        if role is None:
            print(f"Error: default role '{settings.DEFAULT_ROLE}' not found.")
            sys.exit(1)

        # 1. Full first-login state: role_source back to 'default' AND the role
        #    itself back to the born default. Resetting only role_source left a
        #    previously picked role visible in the Users list for an account that
        #    "hasn't picked yet" — the picker decides the role, so the reset must
        #    un-decide it too.
        await session.execute(
            update(User)
            .where(User.email.in_(REALM_ACCOUNTS))
            .values(role_source=ROLE_SOURCE_DEFAULT, role_id=role.id)
        )
        # 2. The M9d grant target loses is_admin (never the owner — guarded by
        #    email, not by flag, so the owner account is structurally out of
        #    reach of this statement).
        if reset_admin:
            await session.execute(
                update(User)
                .where(User.email == GRANT_DEMO_TARGET)
                .values(is_admin=False)
            )
        await session.commit()

    # 3. Sessions: flush every key (sessions, oauth states, reverse index).
    #    Fresh logins for everyone; no live session survives to contradict the
    #    freshly reset rows.
    import redis

    r = redis.from_url(_redis_url(), decode_responses=True)
    r.flushall()


def _redis_url() -> str:
    from app.core.settings import settings

    return settings.REDIS_URL


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument("--apply", action="store_true", help="execute (default: dry-run)")
    parser.add_argument(
        "--keep-admin-grant",
        action="store_true",
        help="skip resetting admin.demo's is_admin (picker reset still runs)",
    )
    args = parser.parse_args()

    users = await plan()
    if not users:
        print("No realm accounts found in the user table — nothing to reset.")
        sys.exit(1)

    print("Realm accounts before:")
    for email, source, is_admin, is_owner in users:
        print(f"  {email:26s} role_source={source:15s} is_admin={is_admin} is_owner={is_owner}")

    print("\nPlan:")
    print(f"  1. role_source -> '{ROLE_SOURCE_DEFAULT}' for {len(users)} account(s)"
          " (role picker returns on next login)")
    if not args.keep_admin_grant:
        print(f"  2. {GRANT_DEMO_TARGET}: is_admin -> False (grant it live via the"
              " Users page — M9d module)")
    else:
        print(f"  2. {GRANT_DEMO_TARGET}: is_admin left as-is (--keep-admin-grant)")
    print("  3. Redis flushall — every session cleared, all logins start fresh")
    print("  Owner flags are NEVER touched (deployment-seeded, §2/§12).")

    if not args.apply:
        print("\nDry run — nothing executed. Re-run with --apply.")
        return

    await apply(reset_admin=not args.keep_admin_grant)
    users = await plan()
    print("\nRealm accounts after:")
    for email, source, is_admin, is_owner in users:
        print(f"  {email:26s} role_source={source:15s} is_admin={is_admin} is_owner={is_owner}")
    print("\nDone. Every login now lands on the role picker once, then the app.")


if __name__ == "__main__":
    asyncio.run(main())
