"""Reverse index from user id to live session ids.

Sessions are stored as `session:{session_id}` (`app/services/auth.py:98-101`) with
no way to find a given user's sessions. `UserSession` carries `is_admin`
(`app/core/security.py:16-24`), and that stored copy is what `require_admin`
(`app/api/dependencies.py:38-41`) and §5's `user_is_admin` filter dimension both
read — so revoking the flag in PostgreSQL alone leaves every live session holding
`admin_only = True` retrieval scope for the rest of the session TTL
(`settings.SESSION_TTL_SECONDS`, 24h by default). That is a confidentiality window
on the §5 hard boundary, which is why this index exists (contract M9, D22).

A Redis SET, not a JSON list under one key: `SADD` is atomic, while
read-modify-write on a blob loses a session id when two logins race — and a lost
id is exactly the session that would then survive the purge.

`smembers` is read as `set[str]`: the application client is built with
`decode_responses=True` (`app/main.py:31`).
"""

from __future__ import annotations

# Mirrors `app/services/auth.py:21` and `app/api/dependencies.py:19`, which each
# already define it. This module cannot import either without a cycle — auth.py
# imports this module — so the literal is repeated and pinned instead: the
# consistency of all three is asserted in
# tests/unit/test_m9_admin_users_ui.py::TestSessionKeyPrefixes. Drift here would
# make the purge silently delete nothing, i.e. reopen D22 without a symptom.
SESSION_PREFIX = "session:"

USER_SESSIONS_PREFIX = "user_sessions:"


def user_sessions_key(user_id: str) -> str:
    """The reverse-index key for one user. One definition, used by all three calls."""
    return f"{USER_SESSIONS_PREFIX}{user_id}"


async def register_session(redis, user_id: str, session_id: str, ttl: int) -> None:
    """SADD the session id to `user_sessions:{user_id}` and refresh the set's TTL.

    The TTL is refreshed on every login so the set always outlives its newest
    member. An expired set only costs a purge that finds nothing; a set that
    expires while a session is still live is a missed revocation.
    """
    key = user_sessions_key(user_id)
    await redis.sadd(key, session_id)
    await redis.expire(key, ttl)


async def unregister_session(redis, user_id: str, session_id: str) -> None:
    """SREM one session id from `user_sessions:{user_id}`. Used on logout."""
    await redis.srem(user_sessions_key(user_id), session_id)


async def purge_user_sessions(redis, user_id: str) -> int:
    """Delete every `session:{sid}` in `user_sessions:{user_id}`, then the set.

    Returns the number of session keys that actually existed and were deleted —
    an id whose session had already expired is not counted. Keys are deleted one
    at a time rather than in a single variadic `DEL` so the call matches
    `MockRedis.delete`'s single-key signature (`tests/conftest.py`), which is
    additive-only by contract.
    """
    key = user_sessions_key(user_id)
    deleted = 0
    for session_id in await redis.smembers(key):
        session_key = f"{SESSION_PREFIX}{session_id}"
        if await redis.get(session_key) is not None:
            deleted += 1
        await redis.delete(session_key)
    await redis.delete(key)
    return deleted
