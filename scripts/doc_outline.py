#!/usr/bin/env python3
"""Print the BookStack outline: chapters -> pages, for scoping decisions.

Reads the stripped file locally, prints only titles (a few KB).
"""

from lxml import etree

PATH = "askgo-621x-fr.stripped.html"
root = etree.HTML(open(PATH, "rb").read())

chapters = []  # (chapter_title, [page_titles])
cur_ch = ("(no chapter)", [])
chapters.append(cur_ch)

for el in root.iter():
    eid = el.get("id") or ""
    if eid.startswith("chapter-"):
        cur_ch = ((el.text or "").strip()[:70] or eid, [])
        chapters.append(cur_ch)
    elif eid.startswith("page-"):
        cur_ch[1].append((el.text or "").strip()[:70] or eid)

total_pages = sum(len(p) for _, p in chapters)
print(f"chapters={len([c for c in chapters if c[1]])}  pages={total_pages}\n")
for title, pages in chapters:
    if not pages:
        continue
    print(f"### {title}  ({len(pages)} pages)")
    for pt in pages:
        print(f"    - {pt}")
