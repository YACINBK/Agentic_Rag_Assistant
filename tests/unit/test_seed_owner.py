from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

import scripts.seed_owner as seed_owner_module


def _configure_session(monkeypatch, role, user):
    mock_session = AsyncMock()
    mock_session.execute = AsyncMock()
    mock_session.commit = AsyncMock()
    mock_session.rollback = AsyncMock()
    # add() is SYNCHRONOUS on AsyncSession. Left as an AsyncMock it returns a
    # coroutine that seed_owner never awaits, which passes the assertion below
    # while emitting a RuntimeWarning — a double that is wrong about the API it
    # stands in for.
    mock_session.add = MagicMock()
    context = AsyncMock()
    context.__aenter__.return_value = mock_session
    context.__aexit__.return_value = None
    mock_session.execute.side_effect = [
        SimpleNamespace(scalar_one_or_none=lambda: role),
        SimpleNamespace(scalar_one_or_none=lambda: user),
    ]
    monkeypatch.setattr(
        seed_owner_module,
        "async_session",
        MagicMock(return_value=context),
    )
    return mock_session


@pytest.mark.asyncio
async def test_seed_new_user(monkeypatch):
    role = SimpleNamespace(id="role-id")
    mock_session = _configure_session(monkeypatch, role, None)

    await seed_owner_module.seed_owner("new@example.com")

    mock_session.add.assert_called_once()
    user = mock_session.add.call_args.args[0]
    assert user.email == "new@example.com"
    assert user.is_owner is True
    assert user.is_admin is True
    assert user.role_id == role.id
    assert user.role_source == "default"
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_existing_user(monkeypatch):
    role = SimpleNamespace(id="role-id")
    user = SimpleNamespace(email="exists@example.com", is_owner=False, is_admin=False)
    mock_session = _configure_session(monkeypatch, role, user)

    await seed_owner_module.seed_owner(user.email)

    assert user.is_owner is True
    assert user.is_admin is True
    mock_session.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_seed_idempotency(monkeypatch):
    role = SimpleNamespace(id="role-id")
    user = SimpleNamespace(email="owner@example.com", is_owner=True, is_admin=True)
    mock_session = _configure_session(monkeypatch, role, user)

    await seed_owner_module.seed_owner(user.email)

    assert user.is_owner is True
    assert user.is_admin is True
    mock_session.commit.assert_awaited_once()
