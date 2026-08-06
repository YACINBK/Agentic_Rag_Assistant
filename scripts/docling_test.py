#!/usr/bin/env python3
"""
Docling validation harness for a large Confluence-exported HTML file.

Goal: decide whether Docling parses THIS document cleanly, and whether the
structure we need (headings, image positions, tables, anchors/attachment links)
survives — WITHOUT loading the document into an LLM context.

It prints ONLY diagnostics (counts, small samples). Safe to paste stdout back.

Stages:
  0. Strip inline base64 image data (keeps the <img> tag, drops the bytes).
  1. lxml "ground truth" structural profile of the stripped file.
  2. Docling parse of the stripped file -> markdown + structure introspection.
  3. Fidelity comparison: does Docling agree with lxml on structure?

Usage:
    python scripts/docling_test.py                     # full run on default file
    python scripts/docling_test.py --file other.html
    python scripts/docling_test.py --smoke             # quick: first ~800KB only
"""

from __future__ import annotations
import argparse
import os
import re
import sys
import time
from collections import Counter

DEFAULT_FILE = "askgo-621x-fr.html"
SMOKE_BYTES = 800_000

# data:image/...;base64,<payload>  -> keep tag, drop payload
B64_RE = re.compile(rb"data:image/[^;]+;base64,[A-Za-z0-9+/=\s]+")
PLACEHOLDER = b"data:image/png;base64,iVBORw0KGgo="  # 1x1-ish stub, keeps <img> valid


def hr(title: str) -> None:
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- Stage 0
def strip_base64(raw: bytes) -> tuple[bytes, int, int]:
    """Return (stripped_bytes, num_images_stripped, bytes_of_image_data)."""
    matches = B64_RE.findall(raw)
    img_bytes = sum(len(m) for m in matches)
    stripped = B64_RE.sub(PLACEHOLDER, raw)
    return stripped, len(matches), img_bytes


# ---------------------------------------------------------------- Stage 1
def lxml_profile(stripped: bytes) -> dict:
    from lxml import etree

    root = etree.HTML(stripped)
    tags = Counter()
    heading_levels = Counter()
    ids = 0
    id_samples: list[str] = []
    internal_anchors = 0
    attachment_links = 0
    img_data_uri = 0
    img_referenced = 0
    tables = 0
    id_prefixes: Counter = Counter()
    page_ids: set = set()
    chapter_ids: set = set()

    for el in root.iter():
        tag = el.tag if isinstance(el.tag, str) else "<comment>"
        tags[tag] += 1

        if tag in ("h1", "h2", "h3", "h4", "h5", "h6"):
            heading_levels[tag] += 1

        if tag == "table":
            tables += 1

        eid = el.get("id")
        if eid:
            ids += 1
            if len(id_samples) < 5:
                id_samples.append(eid[:50])
            # BookStack structural prefixes: page-*, chapter-*, book-*, bkmrk-*
            m = re.match(r"([a-z]+)-", eid)
            if m:
                id_prefixes[m.group(1)] += 1
            if eid.startswith("page-"):
                page_ids.add(eid)
            elif eid.startswith("chapter-"):
                chapter_ids.add(eid)

        if tag == "a":
            href = el.get("href") or ""
            if href.startswith("#"):
                internal_anchors += 1
            if "download/attachments" in href or "/download/" in href:
                attachment_links += 1

        if tag == "img":
            src = el.get("src") or ""
            if src.startswith("data:"):
                img_data_uri += 1
            else:
                img_referenced += 1

    return {
        "tags": tags,
        "heading_levels": heading_levels,
        "heading_total": sum(heading_levels.values()),
        "ids": ids,
        "id_samples": id_samples,
        "internal_anchors": internal_anchors,
        "attachment_links": attachment_links,
        "img_data_uri": img_data_uri,
        "img_referenced": img_referenced,
        "img_total": img_data_uri + img_referenced,
        "tables": tables,
        "id_prefixes": id_prefixes,
        "page_count": len(page_ids),
        "chapter_count": len(chapter_ids),
    }


# ---------------------------------------------------------------- Stage 2
def docling_parse(stripped_path: str) -> dict:
    import docling
    from docling.document_converter import DocumentConverter

    version = getattr(docling, "__version__", "unknown")

    t0 = time.time()
    converter = DocumentConverter()
    result = converter.convert(stripped_path)
    elapsed = time.time() - t0

    doc = result.document
    md = doc.export_to_markdown()

    # Version-proof heading count: ATX headings in the markdown.
    md_headings = len(re.findall(r"(?m)^#{1,6}\s", md))

    # Introspect the structured tree defensively (API varies across versions).
    texts = pictures = tables = 0
    heading_samples: list[str] = []
    pic_sections: list[str] = []
    current_path: list[str] = []

    try:
        for item, _level in doc.iterate_items():
            tname = type(item).__name__
            label = str(getattr(item, "label", "")).lower()
            text = (getattr(item, "text", "") or "").strip()

            is_heading = (
                "sectionheader" in tname.lower()
                or "title" in tname.lower()
                or "header" in label
                or "title" in label
            )
            if is_heading and text:
                texts += 1
                # crude path: replace tail if same-ish depth, else append
                current_path = current_path[:0] + [text[:40]]
                if len(heading_samples) < 15:
                    heading_samples.append(text[:60])
            elif "picture" in tname.lower():
                pictures += 1
                if len(pic_sections) < 8:
                    pic_sections.append(current_path[-1] if current_path else "<no heading yet>")
            elif "table" in tname.lower():
                tables += 1
            elif "text" in tname.lower():
                texts += 1
    except Exception as e:  # noqa: BLE001
        heading_samples.append(f"<iterate_items unavailable: {e!r}>")

    # Fallbacks via direct collections if present.
    if pictures == 0:
        pictures = len(getattr(doc, "pictures", []) or [])
    if tables == 0:
        tables = len(getattr(doc, "tables", []) or [])

    # Did attachment URLs survive into the markdown?
    md_attachments = len(re.findall(r"download/attachments|/download/", md))

    return {
        "version": version,
        "elapsed": elapsed,
        "md_size": len(md.encode("utf-8")),
        "md_headings": md_headings,
        "texts": texts,
        "pictures": pictures,
        "tables": tables,
        "heading_samples": heading_samples,
        "pic_sections": pic_sections,
        "md_attachments": md_attachments,
    }


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default=DEFAULT_FILE)
    ap.add_argument(
        "--smoke",
        action="store_true",
        help=f"parse only the first ~{SMOKE_BYTES // 1000}KB (quick sanity)",
    )
    args = ap.parse_args()

    if not os.path.exists(args.file):
        print(f"ABORT: file not found: {args.file}", file=sys.stderr)
        return 1

    raw = open(args.file, "rb").read()

    hr("STAGE 0: base64 strip")
    stripped, n_imgs, img_bytes = strip_base64(raw)
    if args.smoke:
        stripped = stripped[:SMOKE_BYTES] + b"</body></html>"
    red = 100 * (1 - len(stripped) / max(1, len(raw)))
    print(f"raw        : {len(raw) / 1e6:.1f} MB")
    print(f"stripped   : {len(stripped) / 1e6:.1f} MB  (smoke={args.smoke})")
    print(f"images stripped: {n_imgs}   image data: {img_bytes * 3 // 4 / 1e6:.1f} MB")
    print(f"size reduction : {red:.0f}%")

    stripped_path = args.file.rsplit(".", 1)[0] + ".stripped.html"
    with open(stripped_path, "wb") as f:
        f.write(stripped)
    print(f"wrote          : {stripped_path}")

    hr("STAGE 1: lxml structural ground truth")
    try:
        p = lxml_profile(stripped)
    except Exception as e:  # noqa: BLE001
        print(f"lxml FAILED: {e!r}", file=sys.stderr)
        return 2
    top_tags = ", ".join(f"{t}:{c}" for t, c in p["tags"].most_common(12))
    print(f"top tags   : {top_tags}")
    hl = p["heading_levels"]
    print(
        "headings   : "
        + "  ".join(f"{k}={hl.get(k, 0)}" for k in ("h1", "h2", "h3", "h4", "h5", "h6"))
        + f"   total={p['heading_total']}"
    )
    print(
        f"img tags   : {p['img_total']}  (data-uri:{p['img_data_uri']} referenced:{p['img_referenced']})"
    )
    print(f"tables     : {p['tables']}")
    print(f"id= attrs  : {p['ids']}   samples={p['id_samples']}")
    print(f"internal anchors (href^=#): {p['internal_anchors']}")
    print(f"attachment links          : {p['attachment_links']}")
    top_prefixes = ", ".join(f"{k}-:{c}" for k, c in p["id_prefixes"].most_common(8))
    print(f"id prefixes: {top_prefixes}")
    print(
        f"BookStack structure: pages={p['page_count']}  chapters={p['chapter_count']}"
        + "   <- these are the real chunk boundaries, not heading levels"
    )

    hr("STAGE 2: Docling parse")
    try:
        d = docling_parse(stripped_path)
    except ImportError:
        print("Docling NOT installed. Run:  pip install docling", file=sys.stderr)
        print("(Stages 0 and 1 above are still valid.)", file=sys.stderr)
        return 3
    except Exception as e:  # noqa: BLE001
        print(f"Docling convert FAILED: {e!r}", file=sys.stderr)
        print(
            "Report this error back — it tells us if Confluence HTML is the problem.",
            file=sys.stderr,
        )
        return 4

    print(f"docling version : {d['version']}")
    print(f"convert time    : {d['elapsed']:.1f}s")
    print(f"markdown size   : {d['md_size'] / 1e6:.2f} MB")
    print(f"md headings (#) : {d['md_headings']}")
    print(f"tree counts     : texts~{d['texts']}  pictures={d['pictures']}  tables={d['tables']}")
    print(f"attachment refs preserved in md: {d['md_attachments']}")
    print("heading tree sample (first 15):")
    for h in d["heading_samples"]:
        print(f"   - {h}")
    print("picture -> section (first 8):")
    for s in d["pic_sections"]:
        print(f"   - {s}")

    hr("STAGE 3: fidelity comparison (lxml vs docling)")

    def cmp(name, a, b):
        if a == 0:
            verdict = "n/a"
        else:
            diff = 100 * abs(a - b) / a
            verdict = "GOOD" if diff <= 10 else ("CONCERN" if diff <= 30 else "BAD")
        print(f"{name:12} lxml={a:5}  docling={b:5}   -> {verdict}")

    cmp("headings", p["heading_total"], d["md_headings"])
    cmp("images", p["img_total"], d["pictures"])
    cmp("tables", p["tables"], d["tables"])
    print(
        "\nDeep-link viability (Option 2): "
        + (
            "VIABLE — source has anchors/ids"
            if p["ids"] > p["heading_total"] * 0.5 or p["internal_anchors"] > 0
            else "WEAK — few/no anchors; deep-link may need generated anchors"
        )
    )
    print("\nDone. Paste this whole output back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
