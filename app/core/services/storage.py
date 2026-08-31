"""File storage ABC and hash computation for document uploads and derived artifacts.

Closes three silent failure modes (contract M3):
  1. Filename collision — two admins upload "spec.pdf", second overwrites first.
  2. Path traversal — attacker-controlled filename escapes UPLOAD_DIR.
  3. Hash drift — hash includes filename/mtime, re-upload creates duplicate Document rows.

The stored filename is f"{compute_doc_hash(content)}{suffix}", closing 1 and 2
simultaneously: different content cannot collide, identical content is recognised
as the same document, and no attacker-controlled character survives except the
validated extension.
"""

from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod

# Upload whitelist — what an admin may upload. Validated at save() entry.
# .html is not breadth: the MVP corpus (askgo-621x-fr.html, 146 MB BookStack
# export) arrives in this format, and the ingestion pipeline was designed for it.
ALLOWED_EXTENSIONS: frozenset[str] = frozenset({".pdf", ".docx", ".txt", ".md", ".html"})

# Derived-artifact whitelist — what ingestion may write via save_derived().
# Images extracted from BookStack, cleaned HTML for source view. Validated at
# save_derived() entry. Never merge with ALLOWED_EXTENSIONS: .png is a writable
# artifact, not an uploadable document.
DERIVED_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg", ".html"}
)


def compute_doc_hash(content: bytes) -> str:
    """SHA256 hex digest of the file content.

    Content only — never the filename, path, size, or mtime. Document.doc_hash is
    unique and is the only mechanism that recognises a re-upload of an unchanged
    document. A hash that includes the filename makes spec.pdf and spec_v2.pdf
    two distinct documents with identical bytes — the corpus doubles silently.
    """
    return hashlib.sha256(content).hexdigest()


class BaseStorage(ABC):
    """File storage ABC for uploads and ingestion-derived artifacts."""

    @abstractmethod
    async def save(
        self,
        original_filename: str,
        content: bytes,
    ) -> str:
        """Persist an uploaded file and return its absolute storage path.

        The stored filename is f"{compute_doc_hash(content)}{lowercased_suffix}",
        where suffix is original_filename's extension (case-insensitive). This
        closes collision and traversal failure modes: different bytes cannot
        overwrite each other, and no attacker-controlled character survives
        except the validated extension.

        Args:
            original_filename: Multipart upload filename. Extension must be in
                ALLOWED_EXTENSIONS (case-insensitive), or FileValidationError is
                raised. Used for the stored suffix only — the rest is discarded.
            content: Bytes to store verbatim. Empty bytes is valid.

        Returns:
            Absolute path string of the written file.

        Raises:
            FileValidationError: extension outside ALLOWED_EXTENSIONS, or
                original_filename is empty/extensionless.
        """
        ...

    @abstractmethod
    async def save_derived(
        self,
        suffix: str,
        content: bytes,
    ) -> str:
        """Persist an artifact produced by ingestion (images, cleaned HTML).

        The stored filename is f"{compute_doc_hash(content)}{suffix}". Identical
        bytes resolve to the same path — the Ask&Go export repeats the same UI
        screenshot across many pages, and this gives image deduplication for free.

        Args:
            suffix: Extension with leading dot (e.g. ".png", ".html"),
                case-insensitive. Must be in DERIVED_EXTENSIONS, or
                FileValidationError is raised. No filename component — the caller
                has bytes and MIME type, not an original name to preserve.
            content: Bytes to store verbatim. Empty bytes is valid.

        Returns:
            Absolute path string of the written file, under UPLOAD_DIR/derived/.

        Raises:
            FileValidationError: suffix outside DERIVED_EXTENSIONS.
        """
        ...

    @abstractmethod
    async def read(
        self,
        source_path: str,
    ) -> bytes:
        """Read back previously saved content.

        Args:
            source_path: Path previously returned by save() or save_derived().

        Returns:
            Exact bytes that were saved, unmodified.

        Raises:
            OSError: path does not exist or is not readable.
        """
        ...

    @abstractmethod
    async def delete(
        self,
        source_path: str,
    ) -> None:
        """Remove a stored file.

        Args:
            source_path: Path previously returned by save() or save_derived().

        Returns:
            None. A missing path is a no-op (M8's delete route can run after the
            Document row is gone, and FileNotFoundError would fail a deletion that
            already succeeded).
        """
        ...
