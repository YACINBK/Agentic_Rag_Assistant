import uuid
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi import Request, HTTPException
from fastapi.responses import Response
from sqlalchemy.ext.asyncio import AsyncSession
from app.api.routes.admin import assign_role
from app.core.models.user import User, ROLE_SOURCE_ADMIN_ASSIGNED
from app.core.models.role import Role
from tests.conftest import make_user_session

@pytest.fixture
def mock_session():
    return AsyncMock(spec=AsyncSession)

@pytest.fixture
def mock_redis():
    return AsyncMock()

@pytest.fixture
def mock_request():
    request = MagicMock(spec=Request)
    request.app = MagicMock()
    request.app.state.redis = AsyncMock()
    request.headers = {}
    return request

@pytest.fixture
def actor():
    return make_user_session(
        user_id=str(uuid.uuid4()), email="admin@test.com", is_admin=True, is_owner=False
    )

@pytest.fixture
def target_user():
    return User(
        id=uuid.uuid4(),
        email="target@example.com",
        keycloak_id="kc-123",
        role_id=uuid.uuid4(),
        is_admin=False,
        is_owner=False,
        role_source="default"
    )

@pytest.fixture
def role():
    return Role(id=uuid.uuid4(), name=f"role-{uuid.uuid4().hex}")

@pytest.mark.asyncio
async def test_assign_role_success(mock_request, mock_session, actor, target_user, role):
    # Setup
    mock_request.app.state.redis = AsyncMock()
    
    # Mock _load_user call (which uses session.execute)
    # First call is for target user, second (if any) for refresh
    mock_session.execute.side_effect = [
        MagicMock(scalar_one_or_none=lambda: target_user), # _load_user
        MagicMock(scalar_one_or_none=lambda: role),         # Role validation
        MagicMock(scalar_one_or_none=lambda: target_user), # Refresh
    ]
    
    # We need to patch the internal _load_user to return our target
    with (
        patch("app.api.routes.admin._load_user", side_effect=[target_user, target_user]),
        patch("app.api.routes.admin.purge_user_sessions", new_callable=AsyncMock) as purge,
    ):
        # Mock Role validation specifically
        mock_session.execute = AsyncMock()
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=lambda: role)
        
        # Act
        response = await assign_role(
            request=mock_request,
            user_id=target_user.id,
            role_id=role.id,
            actor=actor,
            session=mock_session
        )
        
        # Assert
        assert isinstance(response, Response)
        assert response.headers.get("content-type", "").startswith("text/html")
        assert target_user.role_id == role.id
        assert target_user.role_source == ROLE_SOURCE_ADMIN_ASSIGNED
        mock_session.commit.assert_awaited_once()
        purge.assert_awaited_once_with(mock_request.app.state.redis, str(target_user.id))

@pytest.mark.asyncio
async def test_assign_role_idempotent(mock_request, mock_session, actor, target_user, role):
    # Setup: target already has the role
    target_user.role_id = role.id
    mock_request.app.state.redis = AsyncMock()
    
    with patch("app.api.routes.admin._load_user", return_value=target_user):
        mock_session.execute = AsyncMock()
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=lambda: role)
        
        # Act
        response = await assign_role(
            request=mock_request,
            user_id=target_user.id,
            role_id=role.id,
            actor=actor,
            session=mock_session
        )
        
        # Assert
        assert isinstance(response, Response)
        mock_session.commit.assert_not_awaited()
        mock_request.app.state.redis.smembers.assert_not_awaited()

@pytest.mark.asyncio
async def test_assign_role_user_not_found(mock_request, mock_session, actor, role):
    with patch("app.api.routes.admin._load_user", return_value=None):
        with pytest.raises(HTTPException) as exc_info:
            await assign_role(
                request=mock_request,
                user_id=uuid.uuid4(),
                role_id=role.id,
                actor=actor,
                session=mock_session
            )
        assert exc_info.value.status_code == 404
        assert "No such user" in exc_info.value.detail

@pytest.mark.asyncio
async def test_assign_role_role_not_found(mock_request, mock_session, actor, target_user):
    with patch("app.api.routes.admin._load_user", return_value=target_user):
        mock_session.execute = AsyncMock()
        mock_session.execute.return_value = MagicMock(scalar_one_or_none=lambda: None)
        
        with pytest.raises(HTTPException) as exc_info:
            await assign_role(
                request=mock_request,
                user_id=target_user.id,
                role_id=uuid.uuid4(),
                actor=actor,
                session=mock_session
            )
        assert exc_info.value.status_code == 404
        assert "No such role" in exc_info.value.detail


@pytest.mark.asyncio
async def test_assign_role_malformed_role_id_returns_400(mock_request, mock_session, actor):
    with pytest.raises(HTTPException) as exc_info:
        await assign_role(
            request=mock_request,
            user_id=uuid.uuid4(),
            role_id="not-a-uuid",
            actor=actor,
            session=mock_session,
        )

    assert exc_info.value.status_code == 400
    mock_session.execute.assert_not_awaited()
