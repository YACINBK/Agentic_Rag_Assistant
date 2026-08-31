"""Tests for the upload outcome surface (upload_status.js wiring).

Found in manual testing 2026-08-31: a duplicate upload returns 409 with a
proper JSON detail, but nothing appears in the UI — htmx never swaps 4xx bodies
by default, so the user sees silence and assumes success. The fix is a status
region plus a scoped afterRequest listener (the csrf.js pattern — a static JS
file, no inline script, §17 rule 7).

These tests assert the WIRING, not the JS execution: the form is identifiable,
the status region exists beside it, the page loads the script, and the script
is scoped to the upload form so the list's polling requests cannot touch the
status box. The route's 4xx JSON shapes are already covered by the M7 suite.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "app" / "static" / "js" / "upload_status.js"

_templates = Environment(
    loader=FileSystemLoader(REPO_ROOT / "app" / "templates"),
    autoescape=True,
)


def _render_page() -> str:
    """The documents page with the context shape M8's route builds."""
    document = SimpleNamespace(
        id="11111111-1111-4111-8111-111111111111",
        original_filename="demo.html",
        category="technical",
        restricted=False,
        ingestion_status="done",
        chunk_count=4,
        last_ingested_at=None,
    )
    return _templates.get_template("pages/admin/documents.html").render(
        user=SimpleNamespace(
            email="owner@whitecape.fr",
            role="developer",
            is_admin=True,
            is_owner=True,
            role_confirmed=True,
        ),
        documents=[document],
        poll=False,
        poll_interval=3,
    )


def test_documents_page_loads_the_status_script() -> None:
    html = _render_page()
    assert "/static/js/upload_status.js" in html


def test_upload_form_is_identifiable_and_carries_status_region() -> None:
    html = _render_page()
    assert 'id="upload-form"' in html
    assert 'id="upload-status"' in html
    assert "hx-post" in html  # still an HTMX upload — the logic is untouched


def test_status_script_is_scoped_to_the_upload_form() -> None:
    """The listener must check the element id — without the scope check, the
    list's 3-second polling responses would overwrite the status message."""
    js = SCRIPT_PATH.read_text(encoding="utf-8")
    assert 'elt.id !== "upload-form"' in js
    # Reads the route's existing JSON detail — no new response shape invented.
    assert "body.detail" in js
    assert "upload-status" in js


def test_status_script_has_no_network_or_dom_side_effects_beyond_the_box() -> None:
    """It renders text into #upload-status and nothing else: no fetch, no
    submit interception, no swap manipulation — the upload flow's logic stays
    exactly as the M7/M8 contracts verified it."""
    js = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "fetch(" not in js
    assert "addEventListener(\"submit\"" not in js
    assert "htmx:" not in js.replace("htmx:afterRequest", "")
