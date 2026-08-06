#!/usr/bin/env python3
"""Per-page size distribution + generalization/cross-ref census.

BookStack export is FLAT: an element with id="page-*" is the page TITLE; its
content follows as siblings until the next page-* marker. So we walk the tree
in document order, attribute each text node / img / table to the current page.

Runs on the already-produced stripped file. lxml only, near-zero cost.
"""

import re
from lxml import etree

PATH = "askgo-621x-fr.stripped.html"
CHARS_PER_TOK = 4  # rough French proxy; 800 tok ~ 3200 chars

raw = open(PATH, "rb").read()
root = etree.HTML(raw)

# ---- flat segmentation by page-* markers ----
pages = []  # (page_id, title, chars, imgs, tables, anchors)
cur = None


def flush():
    if cur:
        pages.append(cur)


for el in root.iter():
    eid = el.get("id") or ""
    tag = el.tag if isinstance(el.tag, str) else ""
    if eid.startswith("page-"):
        flush()
        title = (el.text or "").strip()[:60]
        cur = {"id": eid, "title": title, "chars": 0, "imgs": 0, "tbls": 0, "anch": 0}
    if cur is not None:
        # attribute this node's own text (not descendants -> no double count)
        cur["chars"] += len(el.text or "") + len(el.tail or "")
        if tag == "img":
            cur["imgs"] += 1
        elif tag == "table":
            cur["tbls"] += 1
        if eid.startswith("bkmrk-"):
            cur["anch"] += 1
flush()

if pages:
    n = len(pages)
    sizes = sorted(p["chars"] for p in pages)
    over = sum(1 for s in sizes if s > 3200)
    big = sum(1 for s in sizes if s > 6400)
    print(f"pages measured : {n}")
    print(
        f"page chars     : min={sizes[0]}  median={sizes[n // 2]}  "
        f"p90={sizes[int(n * 0.9)]}  max={sizes[-1]}"
    )
    print(
        f"~tokens        : median~{sizes[n // 2] // CHARS_PER_TOK}  "
        f"max~{sizes[-1] // CHARS_PER_TOK}"
    )
    print(f"pages > 800tok (~3200ch), need split : {over}/{n} ({100 * over // n}%)")
    print(f"pages >1600tok (~6400ch), multi-split: {big}/{n} ({100 * big // n}%)")
    imgs = sorted(p["imgs"] for p in pages)
    print(
        f"imgs/page      : median={imgs[n // 2]}  max={imgs[-1]}  "
        f"pages_with_imgs={sum(1 for i in imgs if i > 0)}"
    )
    print(f"pages w/ table : {sum(1 for p in pages if p['tbls'] > 0)}")
    # show the 5 largest pages so we see what the heavy ones look like
    print("largest pages (chars / imgs / title):")
    for p in sorted(pages, key=lambda x: -x["chars"])[:5]:
        print(f"   {p['chars']:6} / {p['imgs']:3} / {p['title']}")

# ---- generalization + cross-ref census ----
text_all = " ".join(root.itertext())
patterns = {
    "meme_X": r"m[êe]me\s+(d[ée]marche|proc[ée]dure|principe|mani[èe]re|affichage|logique|fonctionnement)",
    "autres_modules": r"pour\s+les\s+autres\s+\w+",
    "s_applique": r"s['’]appliqu\w+",
    "de_meme_facon": r"de\s+la\s+m[êe]me\s+(mani[èe]re|fa[çc]on)",
    "toutes_les": r"toutes?\s+les\s+(pages|modules|fen[êe]tres|entr[ée]es?)",
    "idem": r"\bidem\b",
    "voir_cf": r"\b(cf\.?|se\s+r[ée]f[ée]rer)\b",  # dropped bare 'voir' (too noisy)
}
print("\n--- generalization / cross-ref census ---")
for name, pat in patterns.items():
    print(f"{name:16} {len(re.findall(pat, text_all, re.I))}")
