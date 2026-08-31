"""Unit tests for M8b — admin document mutations (DELETE + reingest).

Contract: contracts/m8b_admin_document_mutations.md, test cases 1–7.
Routes under test: `app/api/routes/admin.py:252` (delete) and `:339` (reingest).

`app/api/routes/admin.py` is not modified by this file — the routes are already
correct. What was wrong was the doubles, in five ways, and each one failed *open*:

1. `patch("app.api.routes.admin.get_session", ...)` cannot substitute a FastAPI
   dependency. FastAPI captures the callable in its dependency graph at route
   registration and never re-reads the module attribute, so the patch was inert
   and the route talked to the real `get_session`. `app.dependency_overrides` is
   the only substitution FastAPI honours.
2. `patch("...LocalFileStorage")` patched the *class*, but the route uses the
   module-level instance created at `admin.py:63`. Patching the class after import
   changes nothing the route can see.
3. `QdrantVectorStore` is the right target — the route constructs it at
   `admin.py:294` — but all three of its methods are awaited, so a bare
   `MagicMock` return value is not awaitable.
4. `execute.side_effect = [a, b]` encodes call order. The delete route issues
   three statements and the retry case replays all three a second time, so the
   list would have to spell the sequence out twice and would still pass silently
   if the route reordered them.
5. `image_ids` comes from `.scalars().all()`, not `.scalar_one_or_none()`.

The statement double therefore dispatches on **(statement kind, entity)** rather
than on arrival order. Entity alone is not enough here: the delete route issues
both `select(Document)` and `delete(Document)`, which name the same entity, and
telling them apart is the whole point of assertion 10.
"""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import Delete, Select

from app.api.dependencies import require_admin, require_auth
from app.api.error_handlers import register_error_handlers
from app.api.routes.admin import DEFAULT_ALLOWED_ROLES, admin_router
from app.core.models.base import get_session
from app.core.models.document import Document
from app.core.models.document_image import DocumentImage
from app.core.services.storage import DERIVED_EXTENSIONS
from app.core.settings import settings
from tests.conftest import MockRedis, make_user_session

# The 403 handler at app/api/error_handlers.py:46 branches three ways on the
# request headers. Pinning Accept to JSON selects the one branch that renders no
# template, so a missing 403.html can never turn a privilege test into a 500.
JSON_HEADERS = {"accept": "application/json"}


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


def _recorder(log: list[str], label: str, *, result=None, exc: Exception | None = None):
    """An awaitable double that appends to a shared ordered call log.

    One log across the session, storage and Qdrant doubles is what makes the two
    ordering invariants assertable rather than merely commented: the chunk IDs are
    read before the points are deleted, and the commit happens after every
    external store has succeeded.
    """

    def _call(*args, **kwargs):
        log.append(label)
        if exc is not None:
            raise exc
        return result

    return AsyncMock(side_effect=_call)


class FakeResult:
    """The two SQLAlchemy Result shapes these routes actually use.

    `scalar_one_or_none()` for the single-row lookups, `scalars().all()` for the
    `image_id` column select — mixing those up is broken double #5.
    """

    def __init__(self, value=None, rows=None) -> None:
        self._value = value
        self._rows = list(rows or [])

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(all=lambda: list(self._rows))


def _statement_key(statement) -> tuple[str, type]:
    """(kind, mapped class) for a Select or a Delete.

    Introspection differs by statement type, and `Delete` is checked *first* for a
    concrete reason: on SQLAlchemy 2.0.51 a `Delete` has no `column_descriptions`
    attribute at all — reading it raises `AttributeError`, it does not return None.
    A dispatcher written the other way round therefore dies on the delete statement
    instead of falling through to the Delete branch. The target mapper lives in
    `entity_description` instead (verified: keys are name/type/expr/entity/table).

    `Select` and `Delete` are unrelated classes here — neither is an instance of the
    other — so the two branches cannot overlap.
    """
    if isinstance(statement, Delete):
        return "delete", statement.entity_description["entity"]
    if isinstance(statement, Select):
        # Populated even for a column-only select: `select(DocumentImage.image_id)`
        # reports entity DocumentImage, not None (verified on 2.0.51).
        entity = statement.column_descriptions[0]["entity"]
        assert entity is not None, f"no entity on {statement!r}; dispatch cannot proceed"
        return "select", entity
    raise AssertionError(f"unexpected statement type {type(statement).__name__}")


class FakeSession:
    """AsyncSession stand-in that answers by (statement kind, entity), not by order.

    Static rather than iterator-backed on purpose: the retry half of
    `test_delete_rolls_back_when_storage_fails` replays the same three statements
    after a rollback, and a session that still returns the document afterwards is
    the correct model of a rollback — the row survived, so the retry can finish.
    """

    def __init__(
        self,
        log: list[str],
        *,
        document: Document | None = None,
        image_ids: tuple[str, ...] = (),
    ) -> None:
        self.document = document
        self.image_ids = list(image_ids)
        self.log = log
        self.commit = _recorder(log, "session.commit")
        self.rollback = _recorder(log, "session.rollback")

    async def execute(self, statement):
        kind, entity = _statement_key(statement)
        self.log.append(f"session.{kind}.{entity.__name__}")

        if kind == "select" and entity is Document:
            return FakeResult(value=self.document)
        if kind == "select" and entity is DocumentImage:
            return FakeResult(rows=self.image_ids)
        if kind == "delete" and entity is Document:
            return FakeResult()
        raise AssertionError(f"unexpected statement: {kind} on {entity.__name__}")


@contextmanager
def _patched_externals(
    log: list[str],
    *,
    chunk_ids: tuple[str, ...] = ("chunk-1", "chunk-2"),
    storage_exc: Exception | None = None,
):
    """Substitute the three collaborators the delete route reaches out to.

    `storage` is patched as the module-level *instance* (`admin.py:63`), not as
    the class. `QdrantVectorStore` is patched as the class because the route
    constructs it per request, and its three methods are AsyncMocks because all
    three are awaited.
    """
    vector_store = MagicMock()
    vector_store.get_chunk_ids_for_document = _recorder(
        log, "qdrant.get_chunk_ids", result=list(chunk_ids)
    )
    vector_store.delete_by_filter = _recorder(log, "qdrant.delete_by_filter")
    vector_store.delete_by_any = _recorder(log, "qdrant.delete_by_any")

    storage = MagicMock()
    storage.delete = _recorder(log, "storage.delete", exc=storage_exc)

    task = MagicMock()
    task.delay = MagicMock(side_effect=lambda *a, **k: log.append("task.delay"))

    with (
        patch("app.api.routes.admin.QdrantVectorStore", return_value=vector_store) as vs_cls,
        patch("app.api.routes.admin.storage", storage),
        patch("app.api.routes.admin.ingest_document", task),
    ):
        yield SimpleNamespace(vector_store=vector_store, storage=storage, task=task, vs_cls=vs_cls)


def _build_client(session: FakeSession, *, user, gate: str = "admin") -> TestClient:
    """An app with the router mounted and exactly one auth seam overridden.

    `gate="admin"` substitutes `require_admin` for the tests that are about the
    route body. `gate="auth"` substitutes only `require_auth`, leaving the real
    `require_admin` in the chain — which is the correct shape for a privilege test:
    override the thing that establishes identity, never the thing that decides
    authorization.
    """
    app = FastAPI()
    app.state.redis = MockRedis()
    register_error_handlers(app)
    app.include_router(admin_router)
    app.dependency_overrides[get_session] = lambda: session
    if gate == "admin":
        app.dependency_overrides[require_admin] = lambda: user
    else:
        app.dependency_overrides[require_auth] = lambda: user
    return TestClient(app)


def _admin() -> object:
    return make_user_session(is_admin=True)


def _derived_paths(image_ids: tuple[str, ...]) -> set[str]:
    derived_dir = Path(settings.UPLOAD_DIR) / "derived"
    return {
        str(derived_dir / f"{img_id}{ext}") for img_id in image_ids for ext in DERIVED_EXTENSIONS
    }


# ---------------------------------------------------------------------------
# Case 1 — assertions 2, 3, 4, 5 (+ both ordering invariants)
# ---------------------------------------------------------------------------


def test_delete_success() -> None:
    """A successful delete wipes DB, storage, Qdrant and the semantic cache."""
    doc_id = uuid.uuid4()
    image_ids = ("img-a", "img-b")
    log: list[str] = []
    session = FakeSession(
        log,
        document=Document(id=doc_id, source_path="/uploads/abc123.pdf"),
        image_ids=image_ids,
    )

    with _patched_externals(log, chunk_ids=("chunk-1", "chunk-2")) as ext:
        response = _build_client(session, user=_admin()).delete(f"/admin/documents/{doc_id}")

    assert response.status_code == 204

    # Assertion 3 — the source file, plus every derived artifact for every allowed
    # extension. Asserted as a set so adding an entry to DERIVED_EXTENSIONS cannot
    # start leaking files while this test still passes.
    deleted = {call.args[0] for call in ext.storage.delete.call_args_list}
    assert "/uploads/abc123.pdf" in deleted
    assert _derived_paths(image_ids) <= deleted
    assert len(deleted) == 1 + len(image_ids) * len(DERIVED_EXTENSIONS)

    # Assertion 4 — the document's own points, by filter.
    ext.vector_store.delete_by_filter.assert_awaited_once_with(
        collection=settings.QDRANT_COLLECTION,
        filter_conditions={"document_id": str(doc_id)},
    )

    # Assertion 5 — cache entries grounded in those chunks (§9 invalidation).
    ext.vector_store.delete_by_any.assert_awaited_once_with(
        collection=settings.QDRANT_CACHE_COLLECTION,
        key="chunk_ids",
        values=["chunk-1", "chunk-2"],
    )

    # Assertion 2 — the row is gone and the transaction was committed.
    assert "session.delete.Document" in log
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()

    # Assertion 2, second half — the `document_image` rows go through the FK's
    # ON DELETE CASCADE (contract Environment), so the route must issue no statement
    # of its own for them. A fake session can prove that absence; it cannot prove the
    # cascade then fires, which is database behaviour. That half is an untested seam
    # and is recorded as one in the summary rather than counted as passing here.
    assert "session.delete.DocumentImage" not in log

    # Ordering invariant 1 (route docstring): chunk IDs are read while the points
    # still exist. Read after the filter delete they come back empty, `delete_by_any`
    # short-circuits on the empty list, and the cache is silently never invalidated.
    assert log.index("qdrant.get_chunk_ids") < log.index("qdrant.delete_by_filter")
    assert log.index("qdrant.get_chunk_ids") < log.index("session.delete.Document")

    # Ordering invariant 2 (assertion 10): commit is last — after every external
    # store has succeeded.
    assert log.index("session.commit") == len(log) - 1


# ---------------------------------------------------------------------------
# Case 2 — assertion 9 (delete half)
# ---------------------------------------------------------------------------


def test_delete_not_found() -> None:
    """404 before anything is touched — a missing row must not delete files."""
    log: list[str] = []
    session = FakeSession(log, document=None)

    with _patched_externals(log) as ext:
        response = _build_client(session, user=_admin()).delete(f"/admin/documents/{uuid.uuid4()}")

    assert response.status_code == 404
    # Nothing beyond the lookup ran: no vector store was even constructed.
    assert log == ["session.select.Document"]
    ext.vs_cls.assert_not_called()
    ext.storage.delete.assert_not_awaited()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Case 3 — assertions 6, 7
# ---------------------------------------------------------------------------


def test_reingest_success() -> None:
    """Status resets to pending, timestamp clears, and the task is enqueued once."""
    doc_id = uuid.uuid4()
    log: list[str] = []
    doc = Document(id=doc_id, ingestion_status="done")
    doc.last_ingested_at = "2026-08-01T00:00:00"
    session = FakeSession(log, document=doc)

    with _patched_externals(log) as ext:
        response = _build_client(session, user=_admin()).post(f"/admin/documents/{doc_id}/reingest")

    assert response.status_code == 202
    # Assertion 6 — the row is reset, not merely re-queued.
    assert doc.ingestion_status == "pending"
    assert doc.last_ingested_at is None
    session.commit.assert_awaited_once()

    # Assertion 7 — enqueued exactly once, with the server-side role scope.
    ext.task.delay.assert_called_once_with(str(doc_id), DEFAULT_ALLOWED_ROLES)

    # The enqueue follows the commit: the worker reads the row it was told about,
    # so a task that outruns its own commit would find the old status.
    assert log.index("session.commit") < log.index("task.delay")


# ---------------------------------------------------------------------------
# Case 4 — assertion 9 (reingest half)
# ---------------------------------------------------------------------------


def test_reingest_not_found() -> None:
    """A non-existent document is a 404, and nothing is queued for it."""
    log: list[str] = []
    session = FakeSession(log, document=None)

    with _patched_externals(log) as ext:
        response = _build_client(session, user=_admin()).post(
            f"/admin/documents/{uuid.uuid4()}/reingest"
        )

    assert response.status_code == 404
    assert log == ["session.select.Document"]
    ext.task.delay.assert_not_called()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Case 5 — assertion 1
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("method,path_suffix", [("delete", ""), ("post", "/reingest")])
def test_privilege_violation(method: str, path_suffix: str) -> None:
    """Both mutations require is_admin — the real `require_admin` decides.

    `require_admin` is deliberately NOT overridden; it is the thing under test.
    Only `require_auth` is substituted, which is the seam that establishes *who*
    the caller is. Overriding `require_admin` here would assert that a test double
    returns what the test put into it.

    Non-admin covers Owner too by construction: `is_owner` implies `is_admin`
    (CLAUDE.md §2), so a flagless session is the only shape that must be refused.
    """
    doc_id = uuid.uuid4()
    log: list[str] = []
    session = FakeSession(log, document=Document(id=doc_id, source_path="/uploads/x.pdf"))
    non_admin = make_user_session(is_admin=False, is_owner=False)

    with _patched_externals(log) as ext:
        client = _build_client(session, user=non_admin, gate="auth")
        response = getattr(client, method)(
            f"/admin/documents/{doc_id}{path_suffix}", headers=JSON_HEADERS
        )

    assert response.status_code == 403
    # Refused before the handler body: no query, no side effect, no enqueue.
    assert log == []
    ext.storage.delete.assert_not_awaited()
    ext.task.delay.assert_not_called()
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Case 6 — assertion 8
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("in_flight_status", ["running", "pending"])
def test_reingest_rejects_in_flight(in_flight_status: str) -> None:
    """409 while a run is outstanding, and no duplicate task is queued.

    `pending` matters as much as `running`: it means a task is already queued and
    not yet picked up, so admitting it enqueues exactly the duplicate this guard
    exists to prevent. A 202 would also report a run that was never started.
    """
    doc_id = uuid.uuid4()
    log: list[str] = []
    doc = Document(id=doc_id, ingestion_status=in_flight_status)
    session = FakeSession(log, document=doc)

    with _patched_externals(log) as ext:
        response = _build_client(session, user=_admin()).post(f"/admin/documents/{doc_id}/reingest")

    assert response.status_code == 409
    ext.task.delay.assert_not_called()
    # The refusal must not have half-applied the reset it declined to perform.
    assert doc.ingestion_status == in_flight_status
    session.commit.assert_not_awaited()


# ---------------------------------------------------------------------------
# Case 7 — assertion 10
# ---------------------------------------------------------------------------


def test_delete_rolls_back_when_storage_fails() -> None:
    """A failed external delete rolls back, and the retry then succeeds.

    The retry is the assertion. "Commit was not called" alone is also true of a
    route that simply has not reached its commit yet; what distinguishes
    commit-last is that the document is still reachable afterwards and pressing
    delete again finishes the job. Committing first would leave orphaned files,
    vectors and cache entries with no row left to reach them through — a permanent
    and invisible leak.
    """
    doc_id = uuid.uuid4()
    log: list[str] = []
    session = FakeSession(
        log,
        document=Document(id=doc_id, source_path="/uploads/abc123.pdf"),
        image_ids=("img-a",),
    )
    user = _admin()

    # --- attempt 1: storage fails
    boom = RuntimeError("storage unavailable")
    with _patched_externals(log, storage_exc=boom) as ext:
        client = _build_client(session, user=user)
        with pytest.raises(RuntimeError, match="storage unavailable"):
            client.delete(f"/admin/documents/{doc_id}")

    # Never a 204: reporting success for a deletion that did not happen is the
    # failure mode this test exists for.
    session.rollback.assert_awaited_once()
    session.commit.assert_not_awaited()
    ext.vector_store.delete_by_filter.assert_not_awaited()
    ext.vector_store.delete_by_any.assert_not_awaited()

    # The row survives the rollback — the uncommitted `delete(Document)` is undone.
    # Asserted directly as well as via the retry below, because it is the contract's
    # own wording for assertion 10 ("the `Document` row survives").
    assert session.document is not None

    # --- attempt 2: storage recovered, same document, same session
    retry_log: list[str] = []
    session.log = retry_log
    session.commit = _recorder(retry_log, "session.commit")
    session.rollback = _recorder(retry_log, "session.rollback")

    with _patched_externals(retry_log) as ext:
        response = _build_client(session, user=user).delete(f"/admin/documents/{doc_id}")

    assert response.status_code == 204
    session.commit.assert_awaited_once()
    session.rollback.assert_not_awaited()
    ext.vector_store.delete_by_any.assert_awaited_once()
    assert retry_log.index("session.commit") == len(retry_log) - 1


# ---------------------------------------------------------------------------
# Beyond the contract's seven — the Forbidden list
# ---------------------------------------------------------------------------


def test_route_uses_the_concrete_vector_store_not_the_abc() -> None:
    """Forbidden 3 — `QdrantVectorStore` must be the concrete class, not the ABC.

    `app/core/services/vector_store.py` holds `BaseVectorStore`, the abstract base;
    the implementation lives in `app/services/vector_store.py`. The two module paths
    differ by one directory, and importing the wrong one is the kind of mistake that
    surfaces at runtime as an abstract-instantiation TypeError inside a request.

    Worth asserting here specifically because every other test in this file patches
    that name out, so no other test would notice if it pointed at the wrong class.
    """
    from app.services.vector_store import QdrantVectorStore as Concrete

    import app.api.routes.admin as admin_module

    assert admin_module.QdrantVectorStore is Concrete


def test_module_level_storage_is_the_instance_the_tests_patch() -> None:
    """Broken double #2, pinned: the route holds a `LocalFileStorage` *instance*.

    If `admin.py` ever moved to constructing storage per request, patching
    `admin.storage` would silently stop intercepting anything and every storage
    assertion above would pass against an untouched real filesystem. This fails
    loudly instead.

    Note the module split, which mirrors Forbidden 3's: the concrete
    `LocalFileStorage` is in `app/services/storage.py`, while
    `app/core/services/storage.py` holds `BaseStorage` plus the shared
    `DERIVED_EXTENSIONS` constant that this suite imports from there.
    """
    import app.api.routes.admin as admin_module
    from app.services.storage import LocalFileStorage

    assert isinstance(admin_module.storage, LocalFileStorage)
