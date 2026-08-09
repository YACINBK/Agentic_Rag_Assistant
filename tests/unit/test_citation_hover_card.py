"""Citation hover card — macro + route-helper rendering tests.

Everything renders through the app's configured Jinja2 environment
(`app.api.routes.search.templates`), never a bare `Template()`, so autoescape
matches production. No browser, no JS execution: assertions cover the markup and
attributes that drive behaviour, not the behaviour itself (contract Environment).
"""

from __future__ import annotations

import re

from app.api.routes.search import markup_answer, templates


def make_citation(**overrides) -> dict:
    """A Citation dict with all 8 keys defaulted; each case overrides one."""
    cite = {
        "index": 1,
        "chunk_id": "chunk-1",
        "document_id": "doc-1",
        "text": "Sample cited chunk text.",
        "section_path": "Chapter 1 > Page 1",
        "anchor": "bkmrk-1",
        "image_refs": [],
        "original_filename": "askgo-621x-fr.html",
    }
    cite.update(overrides)
    return cite


def render_citation(cite: dict) -> str:
    macro = templates.get_template("components/citation.html").module.citation
    return str(macro(cite))


def render_bubble(answer, citations) -> str:
    return templates.get_template("components/message_bubble.html").render(
        answer=answer, citations=citations, sources=[]
    )


def test_marker_renders_index_and_is_focusable() -> None:
    html = render_citation(make_citation(index=3))

    assert "[3]" in html
    assert 'tabindex="0"' in html


def test_card_binds_all_interaction_events() -> None:
    html = render_citation(make_citation())

    assert "x-data" in html
    assert "x-show" in html
    assert "@mouseenter" in html
    assert "@mouseleave" in html
    assert "@focus" in html
    assert "@blur" in html


def test_card_shows_text_and_breadcrumb() -> None:
    html = render_citation(
        make_citation(text="Ouvrez le menu Demandes.", section_path="Demandes > Créer")
    )

    assert "Ouvrez le menu Demandes." in html
    assert "Demandes &gt; Créer" in html


def test_empty_breadcrumb_omits_element() -> None:
    html = render_citation(make_citation(section_path=""))

    assert "cite-crumb" not in html


def test_images_render_lazily_in_order() -> None:
    with_images = render_citation(
        make_citation(
            document_id="doc-a",
            image_refs=[
                {"image_id": "a", "anchor": "bkmrk-a"},
                {"image_id": "b", "anchor": "bkmrk-b"},
            ],
        )
    )
    without_images = render_citation(make_citation(image_refs=[]))

    assert with_images.count("<img") == 2
    assert with_images.count('loading="lazy"') == 2
    assert "/documents/doc-a/images/a" in with_images
    assert "/documents/doc-a/images/b" in with_images
    assert with_images.index("/images/a") < with_images.index("/images/b")

    assert "<img" not in without_images


def test_chunk_text_is_escaped() -> None:
    html = render_citation(
        make_citation(text="Cliquez sur <script>alert(1)</script> puis validez.")
    )

    assert "&lt;script&gt;" in html
    assert "<script>alert(1)</script>" not in html


def test_answer_substitution_escapes_and_preserves_unmatched_markers() -> None:
    answer = "Voir [1] et <b>aussi</b> [7]."
    cite = make_citation(index=1)

    marked = markup_answer(answer, [cite])
    html = render_bubble(marked, [cite])

    assert html.count('class="cite-marker"') == 1
    assert "[7]" in html
    assert "&lt;b&gt;" in html
    assert "<b>aussi</b>" not in html


def test_answer_with_no_citations_renders_cleanly() -> None:
    marked = markup_answer("A plain answer with no citations.", [])
    html = render_bubble(marked, [])

    assert "cite-card" not in html
    assert "A plain answer with no citations." in html


def test_base_links_component_stylesheet() -> None:
    """The card is hidden by CSS ([x-cloak]) until Alpine boots. An unlinked
    stylesheet renders every card expanded on load — markup all present, so no
    other assertion in this file catches it.
    """
    html = templates.get_template("base.html").render()

    assert "/static/css/components.css" in html


def test_base_loads_alpine_pinned_and_deferred() -> None:
    html = templates.get_template("base.html").render()

    assert "alpinejs@3." in html
    assert re.search(
        r"<script[^>]*\bdefer\b[^>]*alpinejs@3\.|<script[^>]*alpinejs@3\.[^>]*\bdefer\b",
        html,
    )
