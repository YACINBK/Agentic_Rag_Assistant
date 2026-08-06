"""M3 — file storage unit tests (real filesystem, real hashlib).

All tests use real filesystem via pytest tmp_path and real hashlib — contract
Environment table: "Mock contract: there is none, deliberately." This is the one
Phase 4 module whose dependencies are both cheap and real.

hashlib is specifically excluded from mocking because phase4_plan.md §2 rule 4
forbids mock-shaped assertions. A faked digest would let assertions 2, 3, and 8
pass while compute_doc_hash hashed the filename — failure mode 3, the exact thing
this module exists to prevent.
"""

from pathlib import Path

import pytest

from app.core.exceptions import FileValidationError
from app.core.services.storage import (
    ALLOWED_EXTENSIONS,
    DERIVED_EXTENSIONS,
    compute_doc_hash,
)
from app.services.storage import LocalFileStorage
from app.core.settings import settings

# Obtained once with python -c 'import hashlib; print(hashlib.sha256(b"whitecape").hexdigest())'
# This is a literal string constant, never recomputed with hashlib inside the test.
# Contract assertion 2: the digest is content-only, and this literal pins that fact.
KNOWN_DIGEST = "aa6076edcbb414c29add74d91ee5c3a8cca2bff5ad45f7b9538b8d46ce894d6d"


@pytest.fixture
def storage(tmp_path, monkeypatch):
    """Monkeypatch UPLOAD_DIR onto tmp_path and yield LocalFileStorage().

    Local fixture (not in conftest.py): nothing else needs filesystem state, and
    putting it in the shared conftest would run this setup for 100 unrelated tests.
    """
    monkeypatch.setattr(settings, "UPLOAD_DIR", str(tmp_path))
    return LocalFileStorage()


def test_allowed_extensions_is_exact_set():
    """Assertion 1: ALLOWED_EXTENSIONS equals exactly the five extensions."""
    assert ALLOWED_EXTENSIONS == {".pdf", ".docx", ".txt", ".md", ".html"}


def test_doc_hash_pins_digest_and_ignores_filename():
    """Assertion 2: compute_doc_hash returns the known digest, content-only.

    hashlib is not imported in this file at all — its absence is what makes the
    assertion meaningful. The digest is a literal, not recomputed.
    """
    assert compute_doc_hash(b"whitecape") == KNOWN_DIGEST
    # Same bytes, different context — must return the same digest
    assert compute_doc_hash(b"whitecape") == KNOWN_DIGEST


async def test_save_read_delete_roundtrip(storage, tmp_path):
    """Assertions 3, 8, 9: save returns absolute path with hash-derived name;
    read returns exact bytes; delete on missing path is a no-op.
    """
    content = b"whitecape internal"
    expected_name = compute_doc_hash(content) + ".pdf"

    path = await storage.save("Quality Manual.pdf", content)

    # Assertion 3: absolute path, name is hash + suffix, parent is UPLOAD_DIR
    assert Path(path).is_absolute()
    assert Path(path).name == expected_name
    assert Path(path).resolve().parent == Path(settings.UPLOAD_DIR).resolve()
    # Filename "Quality Manual.pdf" contains a space — it must not appear in stored name
    assert "Quality" not in Path(path).name
    assert "Manual" not in Path(path).name

    # Assertion 8: read returns exact bytes
    read_back = await storage.read(path)
    assert read_back == content

    # Assertion 9: delete on missing path is a no-op, raises nothing
    await storage.delete(str(tmp_path / "nothing.pdf"))


async def test_save_lowercases_extension(storage):
    """Assertion 4: .PDF is accepted and stored with .pdf suffix."""
    path = await storage.save("report.PDF", b"x")
    assert path.endswith(".pdf")


async def test_save_rejects_bad_extension_and_leaves_no_file(storage, tmp_path):
    """Assertion 5: .exe raises FileValidationError, and no new file is created.

    Both halves in one test — the second is what an implementation that opens the
    target before validating will fail.
    """
    upload_dir = Path(settings.UPLOAD_DIR)
    before_count = len(list(upload_dir.iterdir())) if upload_dir.exists() else 0

    with pytest.raises(FileValidationError):
        await storage.save("payload.exe", b"MZ")

    after_count = len(list(upload_dir.iterdir())) if upload_dir.exists() else 0
    assert after_count == before_count


async def test_save_contains_traversal_filename(storage):
    """Assertion 6: ../../etc/passwd.pdf is contained inside UPLOAD_DIR.

    Extension is legal — validation is deliberately not what catches this. The
    hash-derived name is what contains it.
    """
    path = await storage.save("../../etc/passwd.pdf", b"root:x:0:0")
    assert Path(path).resolve().parent == Path(settings.UPLOAD_DIR).resolve()


async def test_save_twice_is_idempotent(storage, tmp_path):
    """Assertion 7: identical bytes return same path, regardless of filename."""
    content = b"identical bytes"
    path_a = await storage.save("a.pdf", content)
    path_b = await storage.save("b.pdf", content)

    assert path_a == path_b
    # Only one file in UPLOAD_DIR — different filenames, same content
    files = list(Path(settings.UPLOAD_DIR).iterdir())
    assert len(files) == 1


async def test_save_derived_uses_separate_dir_and_set(storage):
    """Assertion 10: save_derived writes to UPLOAD_DIR/derived/, validates against
    DERIVED_EXTENSIONS, and .pdf (legal upload) is rejected as a derived artifact.
    """
    png = b"\x89PNG\r\n\x1a\n fake"
    path = await storage.save_derived(".png", png)

    # Name is hash + suffix, parent is UPLOAD_DIR/derived
    assert Path(path).name == compute_doc_hash(png) + ".png"
    assert Path(path).resolve().parent == (Path(settings.UPLOAD_DIR) / "derived").resolve()

    # read returns exact bytes
    read_back = await storage.read(path)
    assert read_back == png

    # .pdf is a legal upload and an illegal derived artifact — asymmetry is the point
    with pytest.raises(FileValidationError):
        await storage.save_derived(".pdf", b"x")
