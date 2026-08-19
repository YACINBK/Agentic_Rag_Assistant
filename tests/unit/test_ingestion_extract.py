"""Ingestion Stage A — BookStack extractor unit tests (real filesystem, real stdlib).

Real LocalFileStorage against tmp_path with UPLOAD_DIR monkeypatched, exactly as
M3's own tests (contract Environment: "Mock contract: none, deliberately"). A
mocked save_derived would let assertions 3 and 6 pass while nothing was stored.

Fixtures embed *real* PNG bytes (built with zlib/struct, genuinely valid 1x1
images) base64-encoded inline — never placeholder text. A payload that fails to
decode would silently exercise assertion 7's skip path while the test believed it
was exercising assertion 2. The two size-controlled blobs in the boundary test are
raw bytes of exact length: the extractor measures len(decoded) and never inspects
image structure, so their size is the only property under test there.
"""

import base64
import hashlib
import re
import struct
import uuid
import zlib
from pathlib import Path

import pytest

from app.core.services.storage import BaseStorage
from app.core.settings import settings
from app.ingestion.extract import ExtractResult, extract_bookstack_html
from app.services.storage import LocalFileStorage


def _png(rgb: bytes) -> bytes:
    """Build a genuinely valid 1x1 PNG of the given RGB colour (3 bytes)."""

    def chunk(typ: bytes, data: bytes) -> bytes:
        body = typ + data
        return (
            struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit, RGB
    idat = zlib.compress(b"\x00" + rgb)  # one filter byte + one pixel
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", idat) + chunk(b"IEND", b"")


PNG_RED = _png(b"\xff\x00\x00")
PNG_BLUE = _png(b"\x00\x00\xff")
PNG_GREEN = _png(b"\x00\xff\x00")


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def _img(raw: bytes, mime: str = "image/png") -> str:
    return f'<img src="data:{mime};base64,{_b64(raw)}">'


def _sha(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _ordered_struct_ids(html: str) -> list[str]:
    """Structural ids (chapter-/page-/bkmrk-) in document order.

    Anchored to `id="` immediately followed by one of the three prefixes, so the
    rewritten `data-image-id="{sha}"` (a hex digest, never a prefix) cannot match.
    """
    return re.findall(r'id="((?:chapter|page|bkmrk)-[^"]+)"', html)


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """UPLOAD_DIR monkeypatched onto tmp_path, real LocalFileStorage() — as in M3."""
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return LocalFileStorage()


async def test_base64_images_extracted_stored_and_referenced(storage, tmp_path):
    """Assertions 1, 2, 6: three distinct PNGs extracted, referenced by URL + id,
    stored under derived/ as {image_id}.png with stem == image_id.

    The third declares `image/PNG`. MIME is case-insensitive per RFC 2045 §5.1 and the
    MVP corpus uses that spelling on 215 of its 2304 images, so a case-sensitive lookup
    drops 9.3% of the corpus down assertion 7's skip path — where `skipped` increments,
    assertion 1 still balances, and nothing fails. This is the only assertion that
    distinguishes "extracted" from "plausibly skipped".
    """
    doc_id = str(uuid.uuid4())
    html = (
        f"<p>alpha</p>{_img(PNG_RED)}"
        f"<p>beta</p>{_img(PNG_BLUE)}"
        f"<p>gamma</p>{_img(PNG_GREEN, mime='image/PNG')}"
    )

    result = await extract_bookstack_html(html, document_id=doc_id, storage=storage)

    # Assertion 1: with nothing skipped, no base64 payload survives.
    assert result.skipped == 0
    assert result.cleaned_html.count("base64,") == 0

    # Assertion 2: each rewritten <img> carries data-image-id == sha256(decoded)
    # and the matching src built from that same id.
    for raw in (PNG_RED, PNG_BLUE, PNG_GREEN):
        image_id = _sha(raw)
        assert f'data-image-id="{image_id}"' in result.cleaned_html
        assert f'src="/documents/{doc_id}/images/{image_id}"' in result.cleaned_html

    assert {img.image_id for img in result.images} == {
        _sha(PNG_RED),
        _sha(PNG_BLUE),
        _sha(PNG_GREEN),
    }

    # The uppercase declaration is normalised, not merely tolerated: it must be stored
    # with the .png suffix the route resolves and recorded under the folded MIME.
    green = next(img for img in result.images if img.image_id == _sha(PNG_GREEN))
    assert green.mime_type == "image/png"
    assert Path(green.source_path).suffix == ".png"

    # Assertion 6: stored via save_derived (parent is UPLOAD_DIR/derived), named
    # {image_id}.png, and the stored stem equals the id computed here.
    derived = (tmp_path / "derived").resolve()
    for img in result.images:
        assert Path(img.source_path).resolve().parent == derived
        assert Path(img.source_path).name == f"{img.image_id}.png"
        assert Path(img.source_path).stem == img.image_id
        assert (tmp_path / "derived" / f"{img.image_id}.png").exists()


async def test_duplicate_image_is_stored_once(storage, tmp_path):
    """Assertion 3: identical bytes on three pages → one ExtractedImage,
    occurrences == 3, one file on disk, all three tags share the same id."""
    doc_id = str(uuid.uuid4())
    html = (
        f'<div id="page-1">{_img(PNG_RED)}</div>'
        f'<div id="page-2">{_img(PNG_RED)}</div>'
        f'<div id="page-3">{_img(PNG_RED)}</div>'
    )

    result = await extract_bookstack_html(html, document_id=doc_id, storage=storage)

    assert len(result.images) == 1
    assert result.images[0].occurrences == 3
    assert result.cleaned_html.count(f'data-image-id="{_sha(PNG_RED)}"') == 3
    assert len(list((tmp_path / "derived").glob("*.png"))) == 1


async def test_needs_caption_threshold_at_boundary(storage):
    """Assertion 4: decoded size strictly below decorative_max_bytes → "false",
    at or above → "true". Verified at the boundary (3071 vs 3072), not far from it —
    a 100B/100KB test passes against a heuristic that ignores the parameter."""
    below = bytes(3071)  # 3071 zero bytes — extractor measures len, not pixels
    at = b"\x01" * 3072  # distinct content and length → distinct hash, no dedup
    doc_id = str(uuid.uuid4())
    html = f"{_img(below)}{_img(at)}"

    result = await extract_bookstack_html(
        html, document_id=doc_id, storage=storage, decorative_max_bytes=3072
    )

    def flag_for(raw: bytes) -> str:
        m = re.search(
            rf'data-image-id="{_sha(raw)}" data-needs-caption="(true|false)"', result.cleaned_html
        )
        assert m is not None
        return m.group(1)

    assert flag_for(below) == "false"
    assert flag_for(at) == "true"


async def test_structural_ids_and_order_preserved(storage):
    """Assertion 5: counts of chapter-/page-/bkmrk- ids are identical before and
    after, and their document-order sequence is unchanged. Stage B's page/chapter
    attribution depends on exactly this."""
    doc_id = str(uuid.uuid4())
    raw_html = (
        '<section id="chapter-95"><h1 id="page-1028">P1</h1>'
        f'<div id="bkmrk-a">{_img(PNG_RED)}</div>'
        f'<div id="bkmrk-b">{_img(PNG_BLUE)}</div></section>'
        '<section id="chapter-96"><h1 id="page-1029">P2</h1>'
        '<div id="bkmrk-c">text</div>'
        f'<h2 id="page-1030">P3</h2><div id="bkmrk-d">{_img(PNG_RED)}</div></section>'
    )
    ids_before = _ordered_struct_ids(raw_html)

    result = await extract_bookstack_html(raw_html, document_id=doc_id, storage=storage)
    ids_after = _ordered_struct_ids(result.cleaned_html)

    assert ids_after == ids_before
    assert ids_before.count("chapter-95") + ids_before.count("chapter-96") == 2
    assert sum(1 for i in ids_after if i.startswith("page-")) == 3
    assert sum(1 for i in ids_after if i.startswith("bkmrk-")) == 4


async def test_unknown_mime_and_undecodable_payload_are_skipped(storage, tmp_path):
    """Assertion 7 (and assertion 1 at its non-zero-skipped end): an unknown MIME, an
    undecodable payload, and a data: URI outside an `src` attribute are all left
    byte-for-byte untouched, counted, and warned — never raised, never suffix-guessed.
    Three causes, one degrade path, one counter.

    The CSS case is the corpus's: BookStack inlines its callout icons as
    `background-image:url("data:image/svg+xml;base64,…")` inside its <style> block, 4
    of them. The `src` sweep cannot see them, so without counting them assertion 1's
    strict equality fails by exactly 4 on the real 146 MB input while every fixture
    here stays green.
    """
    doc_id = str(uuid.uuid4())
    tiff_bytes = _b64(b"fake tiff bytes, valid base64, unknown mime")
    tiff_tag = f'<img src="data:image/tiff;base64,{tiff_bytes}">'
    bad_png_tag = '<img src="data:image/png;base64,@@@">'
    css_rule = (
        '<style>.callout:before{background-image:url("data:image/svg+xml;base64,'
        f'{_b64(b"<svg/>")}")}}</style>'
    )
    html = f"<p>a</p>{tiff_tag}<p>b</p>{bad_png_tag}{css_rule}"

    result = await extract_bookstack_html(html, document_id=doc_id, storage=storage)

    assert result.images == []
    assert result.skipped == 3
    # All three survive byte-for-byte, payload included.
    assert tiff_tag in result.cleaned_html
    assert bad_png_tag in result.cleaned_html
    assert css_rule in result.cleaned_html
    # Assertion 1 at skipped == 3: exactly the three survivors carry "base64,".
    assert result.cleaned_html.count("base64,") == result.skipped == 3
    # Nothing was stored for the skipped images.
    assert not list((tmp_path / "derived").glob("*.png"))
    assert not list((tmp_path / "derived").glob("*.svg"))


async def test_cleaned_artifact_is_written_and_idempotent(storage):
    """Assertion 8: cleaned_path is a readable .html artifact whose content equals
    cleaned_html, and two runs on identical input return the same path (content-
    addressing from M3 — re-ingesting unchanged input must not accumulate files)."""
    doc_id = str(uuid.uuid4())
    html = f"<p>only</p>{_img(PNG_RED)}"

    first = await extract_bookstack_html(html, document_id=doc_id, storage=storage)
    second = await extract_bookstack_html(html, document_id=doc_id, storage=storage)

    assert first.cleaned_path.endswith(".html")
    assert Path(first.cleaned_path).read_text() == first.cleaned_html
    assert first.cleaned_path == second.cleaned_path


async def test_empty_input_returns_empty_result(storage):
    """Assertion 9: raw_html="" returns a valid empty ExtractResult with a stored
    (empty) artifact — never None, never a raise. An image-free document is
    legitimate input, not an error."""
    result = await extract_bookstack_html("", document_id=str(uuid.uuid4()), storage=storage)

    assert isinstance(result, ExtractResult)
    assert result.images == []
    assert result.skipped == 0
    assert result.cleaned_html == ""
    assert result.cleaned_path.endswith(".html")
    assert Path(result.cleaned_path).read_bytes() == b""


class _FailingStorage(BaseStorage):
    """save_derived raises OSError on every call, recording the suffix it was asked
    to write. Records let the test prove the .html artifact write was never reached —
    the image write fails first (assertion 10's real-world shape: a Document marked
    ingested whose images do not exist)."""

    def __init__(self) -> None:
        self.suffixes: list[str] = []

    async def save(self, original_filename: str, content: bytes) -> str:  # pragma: no cover
        raise AssertionError("Stage A must never call save() — only save_derived()")

    async def save_derived(self, suffix: str, content: bytes) -> str:
        self.suffixes.append(suffix)
        raise OSError("disk full")

    async def read(self, source_path: str) -> bytes:  # pragma: no cover
        raise AssertionError("not used by this test")

    async def delete(self, source_path: str) -> None:  # pragma: no cover
        raise AssertionError("not used by this test")


async def test_storage_failure_propagates():
    """Assertion 10: a save_derived failure propagates unchanged and no cleaned
    artifact is written. A swallowed write failure produces a Document marked
    ingested whose images do not exist — surfacing weeks later on a citation click."""
    stub = _FailingStorage()
    html = f"<p>x</p>{_img(PNG_RED)}"

    with pytest.raises(OSError, match="disk full"):
        await extract_bookstack_html(html, document_id=str(uuid.uuid4()), storage=stub)

    # The image write failed first, so the .html artifact write was never reached.
    assert ".html" not in stub.suffixes
