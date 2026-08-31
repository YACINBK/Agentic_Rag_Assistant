"""Unit tests for M8c — admin document action buttons and refresh wiring.

Contract: contracts/m8c_admin_document_actions.md, test cases 1-7.

The route-level cases import the M8b doubles rather than copying them — the
duplicated-by-copy helper is exactly the D41 drift trap, and these tests need
the same (statement kind, entity) dispatch the M8b suite verified.

What these tests prove about the seam the manual M8b pass left open: the routes
were reachable and correct, but nothing in the UI invoked them and nothing
refreshed the list afterwards. The button shapes (cases 1-3), the HX-Trigger
announcement (cases 4-5), the always-wired refresh path (case 6) and the
polling bootstrap (case 7) close that seam without touching any verified
route logic — the route-side diff is one response header per success path.
"""

from __future__ import annotations

import uuid

import pytest
from jinja2 import Environment, FileSystemLoader
from sqlalchemy import Select

from app.core.models.document import Document
from tests.conftest import make_user_session
from tests.unit.test_m8b_admin_mutations import (
    FakeResult,
    FakeSession,
    _build_client,
    _patched_externals,
)

_templates = Environment(
    loader=FileSystemLoader("app/templates"),
    autoescape=True,
)


def _make_document(ingestion_status: str) -> Document:
    return Document(
        id=uuid.uuid4(),
        original_filename="guide-fr.html",
        source_path="/uploads/guide.html",
        category="technical",
        ingestion_status=ingestion_status,
    )


def _render_card(document: Document) -> str:
    return _templates.get_template("components/document_card.html").render(
        document=document
    )


def _render_list(*, poll: bool, documents=None) -> str:
    return _templates.get_template("partials/document_list.html").render(
        documents=documents if documents is not None else [_make_document("done")],
        poll=poll,
        poll_interval=3,
    )


# ---------------------------------------------------------------------------
# Cases 1-3 — button shapes (assertions 1, 2, 3)
# ---------------------------------------------------------------------------


def test_delete_button_shape() -> None:
    doc = _make_document("done")
    html = _render_card(doc)

    assert f'hx-delete="/admin/documents/{doc.id}"' in html
    assert 'hx-swap="none"' in html
    # Non-empty confirm that names the file — a bare confirm is a click-through.
    assert "guide-fr.html" in html.split("hx-confirm=", 1)[1].split('"', 2)[1]


@pytest.mark.parametrize("status", ["pending", "running"])
def test_reingest_hidden_when_in_flight(status: str) -> None:
    html = _render_card(_make_document(status))

    assert "reingest" not in html
    # The refusal to offer re-ingest never takes the delete control with it.
    assert "hx-delete" in html


@pytest.mark.parametrize("status", ["done", "failed"])
def test_reingest_button_shape_when_idle(status: str) -> None:
    doc = _make_document(status)
    html = _render_card(doc)

    assert f'hx-post="/admin/documents/{doc.id}/reingest"' in html
    assert 'hx-swap="none"' in html


# ---------------------------------------------------------------------------
# Cases 4-5 — the routes announce the mutation (assertions 4, 5, 6)
# ---------------------------------------------------------------------------


def test_delete_204_carries_hx_trigger() -> None:
    doc_id = uuid.uuid4()
    log: list[str] = []
    session = FakeSession(
        log,
        document=Document(id=doc_id, source_path="/uploads/abc123.pdf"),
    )

    with _patched_externals(log):
        response = _build_client(session, user=_m8c_admin()).delete(
            f"/admin/documents/{doc_id}"
        )

    assert response.status_code == 204
    assert response.headers["HX-Trigger"] == "document-changed"


def test_reingest_responses_announce_correctly() -> None:
    doc_id = uuid.uuid4()
    done_doc = Document(
        id=doc_id,
        source_path="/uploads/abc123.pdf",
        ingestion_status="done",
    )

    # 202 — accepted run: the list is stale (status flipped to pending).
    log: list[str] = []
    session = FakeSession(log, document=done_doc)
    with _patched_externals(log):
        accepted = _build_client(session, user=_m8c_admin()).post(
            f"/admin/documents/{doc_id}/reingest"
        )

    assert accepted.status_code == 202
    assert accepted.headers["HX-Trigger"] == "document-changed"

    # 409 — already in flight: nothing changed, so nothing is announced. A
    # refresh here would render a byte-identical list; worse, an event fired
    # on refusal invites a refresh loop on every rejected click.
    running_doc = Document(
        id=doc_id,
        source_path="/uploads/abc123.pdf",
        ingestion_status="running",
    )
    session_409 = FakeSession([], document=running_doc)
    with _patched_externals([]):
        refused = _build_client(session_409, user=_m8c_admin()).post(
            f"/admin/documents/{doc_id}/reingest"
        )

    assert refused.status_code == 409
    assert "HX-Trigger" not in refused.headers


# ---------------------------------------------------------------------------
# Case 6 — the refresh path is unconditional (assertions 7, 8)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("poll", [False, True])
def test_document_list_refresh_always_wired(poll: bool) -> None:
    html = _render_list(poll=poll)

    assert 'hx-get="/admin/documents"' in html
    assert "document-changed from:body" in html
    assert 'hx-swap="outerHTML"' in html

    # The polling trigger alone is conditional — M8's two-sided behaviour,
    # re-expressed for the new attribute layout.
    if poll:
        assert "every 3s" in html
    else:
        assert "every" not in html


# ---------------------------------------------------------------------------
# Case 7 — the D28 reingest bootstrap, at the level the unit suite can reach
# ---------------------------------------------------------------------------


class _ListSession(FakeSession):
    """A FakeSession shaped for the list route: `scalars().all()` over Documents.

    The mutation routes use `scalar_one_or_none()`; the list route collects a
    list. Dispatching on the same (kind, entity) key keeps the D41 rule — no
    call-order doubles — while answering the one query this case needs.
    """

    def __init__(self, documents: list[Document]) -> None:
        super().__init__([])
        self._documents = documents

    async def execute(self, statement):
        assert isinstance(statement, Select), f"unexpected statement {statement!r}"
        entity = statement.column_descriptions[0]["entity"]
        assert entity is Document, f"unexpected entity {entity!r}"
        return FakeResult(rows=self._documents)


def test_reingest_bootstraps_polling() -> None:
    """A document the reingest route just flipped to pending polls on refresh.

    The chain a real 202 sets in motion: route writes `pending` + announces the
    event, the event refreshes the list, and the refreshed partial — rendered
    from post-mutation statuses — carries the polling trigger M8 made
    conditional on exactly those statuses. No page reload, no special casing:
    the bootstrap rides the render path that already existed.
    """
    pending_doc = _make_document("pending")
    client = _build_client(_ListSession([pending_doc]), user=_m8c_admin())

    response = client.get(
        "/admin/documents", headers={"hx-request": "true", "accept": "text/html"}
    )

    assert response.status_code == 200
    assert "guide-fr.html" in response.text
    assert "every 3s" in response.text
    assert f'hx-post="/admin/documents/{pending_doc.id}/reingest"' not in response.text


def _m8c_admin():
    return make_user_session(is_admin=True)
