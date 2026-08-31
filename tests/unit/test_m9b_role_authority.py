"""Unit tests for M9b — role authority moves to PostgreSQL (closes D21).

Contract: contracts/m9b_role_authority.md, test cases 1–4.
Covers assertions 1, 2, 3, 4, 5, 6, 7, 8. Assertions 9 and 10 need a real
async session and a real migration and live in tests/integration/.

Everything external is mocked. The DB mock dispatches on the *entity being
selected* rather than on call order, which is what lets `test_..._survives_a_
contradicting_claim` assert that the Role table was never queried at all —
the structural form of "no role name is read from the claim".
"""

from __future__ import annotations

import json
import uuid
from unittest.mock import MagicMock

import pytest

from app.core.models.role import Role
from app.core.models.user import (
    ROLE_SOURCE_ADMIN_ASSIGNED,
    ROLE_SOURCE_DEFAULT,
    ROLE_SOURCE_SELF_SELECTED,
    User,
)
from app.core.security import UserSession
from app.core.settings import settings
from app.services.auth import KeycloakAuthService
from tests.conftest import (
    MockRedis,
    make_role_model,
    make_user_model,
    make_user_session,
)

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class FakeSession:
    """AsyncSession stand-in that answers by selected entity, not by call order.

    Order-based mocks (`execute.side_effect = [role_result, user_result]`) encode
    which query the code happens to run first. That is exactly the assumption M9b
    invalidates — the User lookup now comes first and the Role lookup happens only
    on the new-user path — so this double keys off the entity instead and records
    which entities were queried, in order.
    """

    def __init__(self, user: User | None = None, role: Role | None = None) -> None:
        self._user = user
        self._role = role
        self.added: list[object] = []
        self.flush_count = 0
        self.selected_entities: list[str] = []
        self.statements: list[object] = []

    async def execute(self, statement):  # noqa: ANN001 — mirrors AsyncSession.execute
        entity = statement.column_descriptions[0]["entity"]
        self.selected_entities.append(entity.__name__)
        self.statements.append(statement)

        result = MagicMock()
        if entity is User:
            result.scalar_one_or_none.return_value = self._user
        elif entity is Role:
            result.scalar_one_or_none.return_value = self._role
        else:  # pragma: no cover — a new query would be a contract change
            raise AssertionError(f"unexpected entity queried: {entity!r}")
        return result

    def add(self, obj: object) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        self.flush_count += 1


def make_existing_user(role_name: str = "developer", **overrides) -> tuple[User, Role]:
    """An existing user with its `role` relationship already populated.

    Populated deliberately: production eagerly loads `User.role` and reads
    `user.role.name` while building the session (contract §Environment, Mock
    contract). A double that left `role` unset would hide that read.
    """
    role = make_role_model(name=role_name)
    user = make_user_model(role_id=role.id, **overrides)
    user.role = role
    return user, role


def make_service(session: FakeSession) -> KeycloakAuthService:
    return KeycloakAuthService(db=session, redis=MockRedis())


# ---------------------------------------------------------------------------
# Case 1 — assertions 1, 2, 4, 7
# ---------------------------------------------------------------------------


async def test_existing_user_role_survives_a_contradicting_claim() -> None:
    user, role = make_existing_user(
        role_name="developer",
        role_source=ROLE_SOURCE_ADMIN_ASSIGNED,
        keycloak_id="kc-existing",
    )
    role_id_before = user.role_id
    session_db = FakeSession(user=user)
    service = make_service(session_db)

    result = await service._lazy_sync_user(
        {
            "sub": "kc-existing",
            "email": user.email,
            # The claim Keycloak would have sent under the old design. Present on
            # purpose: it must be inert, not rejected (contract §Inputs).
            "realm_access": {"roles": ["qa_engineer"]},
        }
    )

    # Assertion 2 — the write that made an admin's assignment revert is gone.
    assert user.role_id == role_id_before
    # Assertions 1 and 4 — the session carries the row's role, not the claim's.
    assert result.role == "developer"
    assert result.role == role.name
    # Assertion 1, structurally: the claim named `qa_engineer`, and no Role query
    # was ever issued, so there is no path by which that name could be resolved.
    assert session_db.selected_entities == ["User"]
    # Assertion 7 — "admin_assigned" is a decided role.
    assert result.role_confirmed is True


async def test_existing_user_with_self_selected_role_is_confirmed() -> None:
    """Assertion 7's second half — stated separately because it is a separate fact."""
    user, _ = make_existing_user(
        role_source=ROLE_SOURCE_SELF_SELECTED,
        keycloak_id="kc-self",
    )
    service = make_service(FakeSession(user=user))

    result = await service._lazy_sync_user({"sub": "kc-self", "email": user.email})

    assert result.role_confirmed is True


# ---------------------------------------------------------------------------
# Case 2 — assertion 3
# ---------------------------------------------------------------------------


async def test_existing_user_email_is_still_synced() -> None:
    """Guards against closing D21 by deleting too much — Keycloak still owns identity."""
    user, _ = make_existing_user(
        role_source=ROLE_SOURCE_ADMIN_ASSIGNED,
        email="old@whitecape.fr",
        keycloak_id="kc-existing",
    )
    role_id_before = user.role_id
    session_db = FakeSession(user=user)
    service = make_service(session_db)

    result = await service._lazy_sync_user(
        {"sub": "kc-existing", "email": "new@whitecape.fr"}
    )

    assert user.email == "new@whitecape.fr"
    assert result.email == "new@whitecape.fr"
    # Identity synced, authorization untouched.
    assert user.role_id == role_id_before
    assert session_db.flush_count == 1


async def test_stale_keycloak_id_relinks_by_email_instead_of_failing() -> None:
    """A realm re-import regenerates subs; the row must heal, not 500.

    Observed live 2026-08-31: two `--import-realm` runs into fresh volumes minted
    new subs, the keycloak_id-only lookup missed, and the create branch died on
    the email unique constraint — a 500 on login for every pre-existing user.
    The email fallback finds the row and RELINKS identity: same person, new sub.
    Authorization (role, flags, role_source) must survive untouched — this is
    the D21 discipline applied to identity instead of role.
    """
    user, _ = make_existing_user(
        role_source=ROLE_SOURCE_SELF_SELECTED,
        email="owner@whitecape.fr",
        keycloak_id="kc-old-sub",
        is_admin=True,
        is_owner=True,
    )
    session_db = FakeSession(user=user)
    service = make_service(session_db)

    result = await service._lazy_sync_user(
        {"sub": "kc-new-sub", "email": "owner@whitecape.fr"}
    )

    # Relinked, not recreated.
    assert user.keycloak_id == "kc-new-sub"
    assert session_db.added == []
    # Authorization untouched — the whole point of relink over recreate.
    assert user.is_admin is True
    assert user.is_owner is True
    assert user.role_source == ROLE_SOURCE_SELF_SELECTED
    assert result.role_confirmed is True


# ---------------------------------------------------------------------------
# Case 3 — assertions 5, 6
# ---------------------------------------------------------------------------


async def test_new_user_gets_the_configured_default_role_unconfirmed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "DEFAULT_ROLE", "qa_engineer")
    default_role = make_role_model(name="qa_engineer")
    # No user row; the qa_engineer Role row exists, as a seeded database has it.
    session_db = FakeSession(user=None, role=default_role)
    service = make_service(session_db)

    result = await service._lazy_sync_user(
        {
            "sub": "kc-brand-new",
            "email": "newcomer@whitecape.fr",
            # Inert here too: a first login cannot steer which Role it lands on.
            "realm_access": {"roles": ["developer"]},
        }
    )

    created = [obj for obj in session_db.added if isinstance(obj, User)]
    assert len(created) == 1
    # Assertion 5 — the row named by operator config, not by the claim.
    assert created[0].role_id == default_role.id
    # Assertion 6, both halves: the column and the session field derived from it.
    assert created[0].role_source == ROLE_SOURCE_DEFAULT
    assert result.role_confirmed is False
    # Assertion 4 on the new-user path.
    assert result.role == "qa_engineer"
    # No Role row was created — the seeded one was found and reused.
    assert not [obj for obj in session_db.added if isinstance(obj, Role)]


async def test_new_user_creates_the_default_role_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The create-on-demand branch of `_resolve_default_role`.

    Not a contract test case; it exists because that branch writes a Role row and
    an untested write path is how a reserved name would slip through unnoticed.
    """
    monkeypatch.setattr(settings, "DEFAULT_ROLE", "developer")
    session_db = FakeSession(user=None, role=None)
    service = make_service(session_db)

    result = await service._lazy_sync_user(
        {"sub": "kc-empty-db", "email": "first@whitecape.fr"}
    )

    created_roles = [obj for obj in session_db.added if isinstance(obj, Role)]
    assert len(created_roles) == 1
    assert created_roles[0].name == "developer"
    assert result.role == "developer"
    assert result.role_confirmed is False


async def test_reserved_default_role_fails_loudly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A misconfigured DEFAULT_ROLE must break the login, not corrupt the §5 filter.

    Forbidden item 4 keeps the `Role.name` validator in place; this is the test
    that shows why it matters here. `admin` is a flag, never a role, so a Role row
    named `admin` would sit in `allowed_roles` matching no user at all.
    """
    monkeypatch.setattr(settings, "DEFAULT_ROLE", "admin")
    service = make_service(FakeSession(user=None, role=None))

    with pytest.raises(ValueError, match="reserved"):
        await service._lazy_sync_user({"sub": "kc-bad-config", "email": "x@y.fr"})


# ---------------------------------------------------------------------------
# Case 4 — assertion 8
# ---------------------------------------------------------------------------


async def test_legacy_session_payload_without_role_confirmed_still_loads() -> None:
    """Sessions written to Redis before the field existed must still reconstruct.

    `json.dumps(user_session.__dict__)` wrote six keys; `UserSession(**payload)`
    reads them back at three sites — app/api/dependencies.py:35, app/main.py:157,
    app/services/auth.py:132. Without the default on `role_confirmed`, every live
    session raises TypeError on the first request after deploy.
    """
    legacy_payload = {
        "user_id": str(uuid.uuid4()),
        "keycloak_id": "kc-legacy",
        "email": "legacy@whitecape.fr",
        "role": "developer",
        "is_admin": False,
        "is_owner": False,
    }
    assert "role_confirmed" not in legacy_payload

    session = UserSession(**legacy_payload)

    assert session.role_confirmed is True

    # The same payload through the real reconstruction site, which is where the
    # TypeError would actually have surfaced.
    redis = MockRedis({"session:legacy-abc": json.dumps(legacy_payload)})
    service = KeycloakAuthService(db=FakeSession(), redis=redis)
    request = MagicMock()
    request.cookies = {"session_id": "legacy-abc"}

    restored = await service.get_current_user(request)

    assert restored is not None
    assert restored.role_confirmed is True
    assert restored.role == "developer"


async def test_conftest_factories_carry_the_new_fields() -> None:
    """The shared factories are the mock contract; drift here breaks other suites."""
    assert make_user_session().role_confirmed is True
    assert make_user_model().role_source == ROLE_SOURCE_DEFAULT
