"""LocalFileStorage — filesystem-backed BaseStorage under settings.UPLOAD_DIR.

Built bare (no constructor args) like every other concrete service in
app/pipeline/factory.py; reads settings.UPLOAD_DIR internally. Tests monkeypatch
UPLOAD_DIR onto tmp_path rather than injecting a directory.
"""

from __future__ import annotations

from pathlib import Path

from app.core.exceptions import FileValidationError
from app.core.services.storage import (
    ALLOWED_EXTENSIONS,
    DERIVED_EXTENSIONS,
    BaseStorage,
    compute_doc_hash,
)
from app.core.settings import settings


class LocalFileStorage(BaseStorage):
    """Stores uploads on the local filesystem under settings.UPLOAD_DIR."""

    @property
    def _upload_dir(self) -> Path:
        # Read at use site, not __init__: tests monkeypatch settings.UPLOAD_DIR
        # and a cached __init__ value would ignore the patch.
        return Path(settings.UPLOAD_DIR)

    async def save(self, original_filename: str, content: bytes) -> str:
        suffix = Path(original_filename).suffix.lower()
        # Validate BEFORE touching the filesystem — an implementation that opens
        # the target first leaves a zero-byte orphan on every rejected upload
        # (assertion 5's load-bearing half).
        if not suffix or suffix not in ALLOWED_EXTENSIONS:
            raise FileValidationError(
                f"Extension {suffix!r} is not an allowed upload type "
                f"(allowed: {', '.join(sorted(ALLOWED_EXTENSIONS))})"
            )

        # Name derived entirely from the content hash — no attacker-controlled
        # character survives except the validated extension. Traversal contained.
        target = self._upload_dir / f"{compute_doc_hash(content)}{suffix}"
        return await self._write(target, content)

    async def save_derived(self, suffix: str, content: bytes) -> str:
        suffix = suffix.lower()
        if suffix not in DERIVED_EXTENSIONS:
            raise FileValidationError(
                f"Suffix {suffix!r} is not an allowed derived-artifact type "
                f"(allowed: {', '.join(sorted(DERIVED_EXTENSIONS))})"
            )

        # Separate directory: UPLOAD_DIR/ is admin-uploaded documents a retention
        # policy operates on; UPLOAD_DIR/derived/ is regenerable output.
        target = self._upload_dir / "derived" / f"{compute_doc_hash(content)}{suffix}"
        return await self._write(target, content)

    async def read(self, source_path: str) -> bytes:
        # Missing path propagates OSError (contract Inputs table). Plain pathlib
        # read: filesystem I/O is microseconds and the contract Environment
        # specifies pathlib/open, not an async file library.
        return Path(source_path).read_bytes()

    async def delete(self, source_path: str) -> None:
        # Missing path is a no-op: M8's delete route can run after the Document
        # row is gone, and FileNotFoundError would fail a deletion that succeeded.
        Path(source_path).unlink(missing_ok=True)

    @staticmethod
    async def _write(target: Path, content: bytes) -> str:
        # mkdir(exist_ok): a first upload on a fresh deployment must not fail
        # because nobody created the directory by hand.
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        return str(target.resolve())
