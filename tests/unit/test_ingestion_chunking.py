"""Unit tests — Ingestion Stage B chunker (M4).

Pure/offline: no service, no network, no tokenizer download. ``length_fn`` is a
whitespace-token count mock — contract-valid (str→int, monotonic in length) and
what assertion 5 (internal consistency) is written against.

The 8 named cases mirror ``contracts/ingestion_chunking.md`` exactly. Cases 7 and
8 exist specifically to catch the defects the 2026-08-10 real-corpus pass found —
document-order chapter attribution, the ``(page_id, anchor)`` composite key, and
structural ``<style>`` removal. Cases 2–6 were rewritten after the same pass:
the first implementation passed 9 green tests yet shattered the real corpus into
3570 chunks (median 8 tokens) because ``bkmrk-*`` sections were never packed. Case
3 is the real ``page-1121`` copied **verbatim out of ``scratchpad/cleaned.html``**
(the Stage A artifact) — every ``<img>`` carries a ``data-image-id`` and no base64,
which is the only fixture that can exercise assertion 8 on genuine French markup.
"""

from __future__ import annotations

from app.ingestion.chunk import (
    GENERALIZATION_PHRASES,
    MAX_MODULE_WORDS,
    Chunk,
    chunk_bookstack_html,
)


def ws_len(text: str) -> int:
    """Whitespace-token count — the mock length_fn (monotonic, str→int)."""
    return len(text.split())


def _words(n: int, prefix: str = "mot") -> str:
    """`n` unique space-separated words, so overlap == set-intersection size."""
    return " ".join(f"{prefix}{i}" for i in range(n))


# ---------------------------------------------------------------------------
# Case 1 — single_small_page
# ---------------------------------------------------------------------------


def test_single_small_page():
    html = f"""
    <h1 id="chapter-1">Getting Started</h1>
    <h1 id="page-10">Installation</h1>
    <p>{_words(60, "alpha")}</p>
    <p>{_words(60, "beta")}</p>
    """
    chunks = chunk_bookstack_html(html, length_fn=ws_len)

    assert len(chunks) == 1
    c = chunks[0]
    assert c.page_id == "page-10"
    assert c.page_title == "Installation"
    assert c.chapter_id == "chapter-1"
    assert c.chapter_title == "Getting Started"
    # No leading bkmrk-* → anchor falls back to the page id, never empty (assertion 6).
    assert c.anchor == "page-10"
    assert c.chunk_index == 0
    assert c.is_generalized_procedure is False
    assert c.module_candidates == []
    assert c.token_count == ws_len(c.text)  # assertion 5


# ---------------------------------------------------------------------------
# Case 2 — large_page_bkmrk_packing
# ---------------------------------------------------------------------------


def test_large_page_bkmrk_packing():
    # 12 bkmrk-* sections of ~170 tok each (the real export shape: a bkmrk id on
    # nearly every paragraph). Packing must return 3 chunks, NOT 12 — a per-section
    # implementation returns 12 chunks of ~170 tok and fails the count (assertion 4).
    sections = "".join(
        f'<h2 id="bkmrk-sec{i}">Sub {i}</h2><p>{_words(165, f"s{i}_")}</p>' for i in range(12)
    )
    html = f"""
    <h1 id="chapter-2">Reference</h1>
    <h1 id="page-20">Big Page</h1>
    {sections}
    """
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)

    assert len(chunks) == 3
    # Each chunk's anchor is the bkmrk-* of the FIRST section in its pack.
    assert [c.anchor for c in chunks] == ["bkmrk-sec0", "bkmrk-sec4", "bkmrk-sec8"]
    for c in chunks:
        assert c.token_count <= 800
        assert c.token_count == ws_len(c.text)
        assert c.page_id == "page-20"
        assert c.chapter_id == "chapter-2"
        # No packed chunk is under a quarter of the budget — packing filled toward it.
        assert c.token_count >= 800 // 4
    # section_path carries the FIRST section of the pack.
    assert chunks[0].section_path == "Reference > Big Page > Sub 0"
    assert chunks[1].section_path == "Reference > Big Page > Sub 4"
    assert chunks[2].section_path == "Reference > Big Page > Sub 8"
    # chunk_index is 0-based within the page.
    assert [c.chunk_index for c in chunks] == [0, 1, 2]


# ---------------------------------------------------------------------------
# Case 3 — export_generalization  (REAL page-1121 out of scratchpad/cleaned.html)
# ---------------------------------------------------------------------------

# Verbatim slice of page-1121 "Export des entries" copied out of the real Stage A
# artifact scratchpad/cleaned.html — NOT askgo-621x-fr.stripped.html, a different
# pre-Stage-A file whose <img> carry base64 src and no data-image-id. Every <img>
# here carries a data-image-id (six of them) and no base64, so this is the one
# fixture that exercises assertion 8 on genuine French markup: the chunker must emit
# a [[image:{id}]] placeholder and an ImageRef per id, never the src.
REAL_PAGE_1121 = """
<h1 id="page-1121">Export des entries</h1>
<h2 id="bkmrk-objectifs">Objectifs</h2>
<ul id="bkmrk-donner-la-possibilit">
<li>Pouvoir <strong>exporter</strong> la liste des résultats d'une recherche, sous forme d'un document <strong>Excel. </strong></li>
<li>L'export concerne tous les entries concernés par la recherche <strong>(Commandes, Demandes, Sorties de stock, Visa, Factures, Réceptions, Contrats)</strong></li>
</ul>
<h2 id="bkmrk-contexte-et-pertinen">Contexte et pertinence stratégique</h2>
<p id="bkmrk-dans-l%27application-e">Pour exporter une  liste, on doit cliquer sur le bouton <strong>Exporter</strong></p>
<p id="bkmrk-"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2022-02/demandes-130.PNG" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/406cba9be62b415f33f31eb3b6730b5f3b1c6bbf10fa5e42547d89fad534228a" data-image-id="406cba9be62b415f33f31eb3b6730b5f3b1c6bbf10fa5e42547d89fad534228a" data-needs-caption="true" alt="demandes_130.PNG"/></a></p>
<p id="bkmrk-l%27export-est-fait-se">L'export est fait selon la trie et la recherche sélectionnée.</p>
<p id="bkmrk-un-rapport-sur-la-li">Un rapport sur la liste de résultats de recherche est exporté dans un document <strong>Excel</strong> avec les champs suivants :</p>
<ul id="bkmrk-cr%E3%A9%E3%A9-par-%3A-correspon">
<li>Créé par : Correspond a l'auteur qui a crée l'entries</li>
<li>Date de création : c'est la date pendant laquelle l'entries est crée</li>
<li>Statut externe : Correspond au statut actuel de l'entries</li>
<li>Libellé statut externe : Désignation du "statut externe"  en libellé (Exemple: <strong>Acceptée</strong> pour le statut <strong>50 )</strong></li>
<li>Numéro : C'est le numéro correspondant à l'entries</li>
<li>Type : C'est le Type de l'entries (Exemple: <strong>Bâtiment</strong>)</li>
<li>Statut interne :  C'est le statut de l'entries par rapport au Workflow</li>
<li>Libellé statut interne : Désignation du "statut interne" en libellé (Exemple: <strong>Acceptée</strong> pour le statut <strong>40 )</strong></li>
<li>Code fournisseur : Correspond au TieCode du fournisseur utilisé dans Ask&amp;go sur l'entête de l'entries</li>
<li>Fournisseur : Correspond au nom du fournisseur utilisé dans Ask&amp;go</li>
<li>Référence 1 : Correspond au premier champ descriptif pour référencier l'entries</li>
<li>Référence 2 : Correspond au deuxième champ descriptif pour référencier l'entries</li>
<li>Référence 3 : Correspond au troisième champ descriptif pour référencier l'entries</li>
<li>Code IMP : </li>
<li>Etablissement : Correspond a l'établissement utilisé dans un entries</li>
<li>Montant : Correspond au montant calculé de l'entries, généré après multiplication de la <strong>Quantité</strong> par le <strong>Prix unitaire </strong></li>
<li>Devise: Correspond au monnaie locale</li>
<li>Code magasin : C'est le magasin dans lequel appartient l'article utilisé dans l'entries</li>
<li>Identifiant de la demande : C'est l'identifiant unique de l'entries</li>
</ul>
<p id="bkmrk--0"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2023-06/devise1.png" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/fec9758f48c2f224555c0f28ac8c4cbd3d512bf80f2cf376a3c623eddb1d9032" data-image-id="fec9758f48c2f224555c0f28ac8c4cbd3d512bf80f2cf376a3c623eddb1d9032" data-needs-caption="true" alt="devise1.png" width="1620" height="100"/></a><span style="color: rgb(35, 111, 161);">Exemple : <a style="color: rgb(35, 111, 161);" href="http://buildsrv2:8090/download/attachments/61506500/Demandes%20%2819%29.xlsx?version=3&amp;amp;modificationDate=1640096689000&amp;amp;api=v2" target="_blank" rel="noopener">Demandes (19).xlsx</a></span></p>
<p id="bkmrk-%C2%A0"><br/></p>
<p id="bkmrk-pour-les-autres-modu">Pour les autres modules (Commandes, Réceptions, Factures etc..), la même démarche s'applique pour exporter le rapport de résultats de recherche via le document <strong>Excel</strong></p>
<p id="bkmrk-pour-le-module-comma"><strong>Pour le module Commandes , </strong>les champs exportés sont :</p>
<ul id="bkmrk-cr%E3%A9%E3%A9-par-%3A%E2%A0correspond">
<li>Créé par : Correspond a l'auteur qui a crée l'entries</li>
<li>Date de création : c'est la date pendant laquelle l'entries est crée</li>
<li>Statut externe : Correspond au statut actuel de l'entries</li>
<li>Libellé statut externe : Désignation du "statut externe"  en libellé</li>
<li>Numéro : C'est le numéro correspondant à l'entries</li>
<li>Type : C'est le Type de l'entries (Exemple: <strong>Bâtiment, Administration, HR </strong>etc..)</li>
<li>Statut interne : C'est le statut de l'entries par rapport au Workflow</li>
<li>Libellé statut interne : Désignation du "statut interne" en libellé</li>
<li>Code fournisseur : Correspond au TieCode du fournisseur utilisé dans Ask&amp;go sur l'entête de l'entries</li>
<li>Fournisseur : Correspond au nom du fournisseur utilisé dans Ask&amp;go</li>
<li>Référence 1 : C'est le premier champ descriptif pour référencier l'entries</li>
<li>Référence 2 : C'est le deuxième champ descriptif pour référencier l'entries</li>
<li>Référence 3 : C'est le troisième premier champ descriptif pour référencier l'entries</li>
<li>Code IMP : </li>
<li>Etablissement : Correspond a l'établissement utilisé dans un entries</li>
<li>Montant : Correspond au montant calculé de l'entries, généré après multiplication de la <strong>Quantité</strong> par le <strong>Prix unitaire </strong></li>
<li>Devise montant: Correspond au monnaie locale du montant<br/></li>
<li>Devise solde: Correspond au monnaie locale du solde</li>
<li>Code magasin : C'est le magasin dans lequel appartient l'article utilisé dans l'entries</li>
<li>Identifiant de la commande : C'est l'identifiant unique de l'entries</li>
</ul>
<p id="bkmrk--1"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2023-07/image.png" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/be3946f4fd61d23622b40542992c70e4160dbaf4b88d33a138b8f112e19abf8f" data-image-id="be3946f4fd61d23622b40542992c70e4160dbaf4b88d33a138b8f112e19abf8f" data-needs-caption="true" alt="image.png"/></a><span style="color: rgb(35, 111, 161);">Exemple : <a style="color: rgb(35, 111, 161);" href="http://buildsrv2:8090/download/attachments/61506500/Commande%20%2813%29.xlsx?version=1&amp;modificationDate=1640096491000&amp;api=v2" data-linked-resource-id="61508073" data-linked-resource-version="1" data-linked-resource-type="attachment" data-linked-resource-default-alias="Commande (13).xlsx" data-nice-type="Excel Spreadsheet" data-linked-resource-content-type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" data-linked-resource-container-id="61506500" data-linked-resource-container-version="33">Commande (13).xlsx</a></span></p>
<p id="bkmrk-%E2%A0-1"><br/></p>
<p id="bkmrk-pour-le-module-r%E3%A9cept"><strong>Pour le module Réceptions:</strong></p>
<ul id="bkmrk-code-catalogue-%3A%E2%A0-dat">
<li>Code catalogue : </li>
<li>Date commande : Correspond a la date pendant laquelle l'entries <strong>Commande</strong> a été crée</li>
<li>Numéro Commande : C'est le numéro de la "Commande" correspondant à l'entries</li>
<li>Identifiant Commande : C'est l'identifiant unique de la "Commande" correspondant à l'entries</li>
<li>Etablissement : Correspond a l'établissement utilisé dans un entries</li>
<li>Date création réception : c'est la date pendant laquelle l'entries est crée</li>
<li>Référence : C'est un champ descriptif pour référencier l'entries</li>
<li>Numéro Réception :  C'est le numéro correspondant à l'entries</li>
<li>Commentaire : Correspond au texte du commentaire ajouté dans l'entries</li>
<li>Type : C'est le Type de l'entries (Exemple: <strong>Bâtiment, Administration, HR </strong>etc..)</li>
<li>Statut externe : Correspond au statut actuel de l'entries</li>
<li>Libellé statut externe : Désignation du "statut externe"  en libellé</li>
<li>Statut interne : C'est le statut de l'entries par rapport au Workflow</li>
<li>Libellé statut interne : Désignation du "statut interne" en libellé</li>
<li>Code fournisseur : Correspond au TieCode du fournisseur utilisé dans Ask&amp;go sur l'entête de l'entries</li>
<li>Fournisseur : Correspond au nom du fournisseur utilisé dans Ask&amp;go</li>
<li>Identifiant de la réception : C'est l'identifiant unique de l'entries</li>
</ul>
<p id="bkmrk--2"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2022-02/demandes-133.PNG" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/fbd4f68fa75d57c47a717d595f605208227d5da4e9c1be028e7241c0476da68f" data-image-id="fbd4f68fa75d57c47a717d595f605208227d5da4e9c1be028e7241c0476da68f" data-needs-caption="true" alt="demandes_133.PNG"/></a><span style="color: rgb(35, 111, 161);">Exemple : <a style="color: rgb(35, 111, 161);" href="http://buildsrv2:8090/download/attachments/61506500/R%C3%A9ceptions%20%282%29.xlsx?version=1&amp;modificationDate=1640096528000&amp;api=v2" data-linked-resource-id="61508074" data-linked-resource-version="1" data-linked-resource-type="attachment" data-linked-resource-default-alias="Réceptions (2).xlsx" data-nice-type="Excel Spreadsheet" data-linked-resource-content-type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" data-linked-resource-container-id="61506500" data-linked-resource-container-version="33">Réceptions (2).xlsx</a></span></p>
<p id="bkmrk-%E2%A0-2"><br/></p>
<p id="bkmrk-pour-le-module-factu"><strong>Pour le module Factures:</strong></p>
<ul id="bkmrk-cr%E3%A9%E3%A9-par-%3A%E2%A0correspond-0">
<li>Créé par : Correspond a l'auteur qui a crée l'entries</li>
<li>Date de création : c'est la date pendant laquelle l'entries est crée</li>
<li>Statut externe : Correspond au statut actuel de l'entries</li>
<li>Libellé statut externe : Désignation du "statut externe"  en libellé</li>
<li>Numéro : C'est le numéro correspondant à l'entries</li>
<li>Statut interne : C'est le statut de l'entries par rapport au Workflow</li>
<li>Libellé statut interne : Désignation du "statut interne" en libellé</li>
<li>Code fournisseur : Correspond au TieCode du fournisseur utilisé dans Ask&amp;go sur l'entête de l'entries</li>
<li>Fournisseur : Correspond au nom du fournisseur utilisé dans Ask&amp;go</li>
<li>Code catalogue : </li>
<li>Responsable : Correspond au nom du Responsable qui a crée l'entries</li>
<li>Montant : Correspond au montant calculé de l'entries, généré après multiplication de la <strong>Quantité</strong> par le <strong>Prix unitaire </strong></li>
<li>Devise: Correspond au monnaie locale<br/></li>
<li>Référence : C'est un champ descriptif pour référencier l'entries</li>
<li>Type facture : C'est le Type de l'entries (Exemple:<strong> F</strong><strong>acture avec BC ss TVA , </strong><strong>N.Credit avec BC ss TVA , </strong><strong>Facture avec BC avec TVA</strong> ,<strong> </strong>etc..)</li>
<li>Etablissement : Correspond a l'établissement utilisé dans l'entries</li>
<li>Gestion interne : Correspond au type de mouvement sous l'entries </li>
<li>Ecriture en attente : Correspond au code de journal comptable de l'entries lors de l'imputation</li>
<li>Ecritures comptables : Correspond au code de journal comptable de l'entries</li>
<li>Identifiant de la facture : C'est l'identifiant unique de l'entries</li>
</ul>
<p id="bkmrk--3"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2023-06/Ejudevise3.png" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/214a45991f28b68b6b1cb5ea304622af79cee1e0d5d936c2f8689f75977ca441" data-image-id="214a45991f28b68b6b1cb5ea304622af79cee1e0d5d936c2f8689f75977ca441" data-needs-caption="true" alt="devise3.png" height="1200"/></a><span style="color: rgb(35, 111, 161);">Exemple : <a style="color: rgb(35, 111, 161);" href="http://buildsrv2:8090/download/attachments/61506500/Factures%20%281%29%20%281%29.xlsx?version=1&amp;modificationDate=1640096563000&amp;api=v2" data-linked-resource-id="61508075" data-linked-resource-version="1" data-linked-resource-type="attachment" data-linked-resource-default-alias="Factures (1) (1).xlsx" data-nice-type="Excel Spreadsheet" data-linked-resource-content-type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" data-linked-resource-container-id="61506500" data-linked-resource-container-version="33">Factures (1) (1).xlsx</a></span></p>
<p id="bkmrk-%E2%A0-3"><br/></p>
<p id="bkmrk-%E2%A0-4"><br/></p>
<p id="bkmrk-pour-le-module-contr"><strong>Pour le module Contrats:</strong></p>
<ul id="bkmrk-cr%E3%A9e-par-%3A%E2%A0correspond-">
<li>Crée par : Correspond a l'auteur qui a crée l'entries</li>
<li>Date de création : c'est la date pendant laquelle l'entry est crée</li>
<li>Statut externe : Correspond au statut actuel de l'entries</li>
<li>Libellé statut externe : Désignation du "statut externe"  en libellé</li>
<li>Numéro : C'est le numéro correspondant à l'entries</li>
<li>Statut interne : C'est le statut de l'entries par rapport au Workflow</li>
<li>Code fournisseur : Correspond au TieCode du fournisseur utilisé dans Ask&amp;go sur l'entête de l'entries</li>
<li>Fournisseur : Correspond au nom du fournisseur utilisé dans Ask&amp;go</li>
<li>Montant Total : Correspond au montant total de l'entries </li>
<li>Devise: Correspond au monnaie locale</li>
<li>Montant maximum : Correspond au montant qui ne doit pas être dépassé , en monnaie locale</li>
<li>Date début du contrat : Correspond à la date de début de l'entries , à partir du moment de sa création</li>
<li>Date de fin de vie du contrat : Correspond à la date d'expiration de l'entries</li>
<li>Date Avertissement : </li>
<li>Référence interne :</li>
<li>Référence externe :</li>
<li>Libellé : C'est la désignation de l'entries en libellé</li>
</ul>
<p id="bkmrk--4"><a href="http://devtools:8012/BookStack/public/uploads/images/gallery/2023-06/devise4.png" target="_blank" rel="noopener"><img src="/documents/00000000-0000-0000-0000-000000000001/images/945f0b2572e4046fad4da4d6281400f749be7e6a80beb748b74daa608efe64c9" data-image-id="945f0b2572e4046fad4da4d6281400f749be7e6a80beb748b74daa608efe64c9" data-needs-caption="true" alt="devise4.png"/></a><span style="color: rgb(35, 111, 161);">Exemple : <a style="color: rgb(35, 111, 161);" href="http://buildsrv2:8090/download/attachments/61506500/Contrat%20%281%29%20%281%29.xlsx?version=1&amp;modificationDate=1640096590000&amp;api=v2" data-linked-resource-id="61508076" data-linked-resource-version="1" data-linked-resource-type="attachment" data-linked-resource-default-alias="Contrat (1) (1).xlsx" data-nice-type="Excel Spreadsheet" data-linked-resource-content-type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" data-linked-resource-container-id="61506500" data-linked-resource-container-version="33">Contrat (1) (1).xlsx</a></span></p>
"""


def test_export_generalization_real_page():
    chunks = chunk_bookstack_html(REAL_PAGE_1121, length_fn=ws_len, max_tokens=800)

    # Packing yields 2 chunks (~734 + ~410 tok), consistent with assertion 4.
    assert len(chunks) == 2
    for c in chunks:
        assert c.page_id == "page-1121"
        assert c.page_title == "Export des entries"
        assert c.token_count <= 800
        assert c.token_count == ws_len(c.text)

    joined = "".join(c.text for c in chunks)
    # "pour les autres" + "même démarche" + "s'applique" all fire on chunk 0 (assertion 10).
    assert chunks[0].is_generalized_procedure is True
    assert set(chunks[0].module_candidates) == {"Commandes", "Réceptions", "Factures"}
    # No image src ever reaches embed text — placeholders only.
    assert "base64," not in joined
    assert "data:image" not in joined
    assert "buildsrv2" not in joined
    # Six data-image-id images → six placeholders / six refs, in document order.
    assert joined.count("[[image") == 6
    assert sum(len(c.image_refs) for c in chunks) == 6
    # Assertion 8 / Forbidden: never an ImageRef with an empty id.
    assert all(r.image_id != "" for c in chunks for r in c.image_refs)


# ---------------------------------------------------------------------------
# Case 4 — table_atomic_and_embed_ceiling
# ---------------------------------------------------------------------------


def _twelve_row_table() -> str:
    rows = "".join(f"<tr><td>ligne{i}col1</td><td>ligne{i}col2</td></tr>" for i in range(12))
    return f"<table><tr><th>H1</th><th>H2</th></tr>{rows}</table>"


def test_table_atomic_and_embed_ceiling():
    html = f"""
    <h1 id="page-30">Data Page</h1>
    <p>Intro paragraph before the table.</p>
    {_twelve_row_table()}
    """

    # (a) Under budget: one Markdown block, table_count=1, never split.
    a = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert len(a) == 1
    assert a[0].table_count == 1
    assert "| --- |" in a[0].text
    for i in range(12):
        assert f"ligne{i}col1" in a[0].text
        assert f"ligne{i}col2" in a[0].text

    # (b-i) max_tokens=10, embed_max_tokens=800: the table busts max_tokens but fits
    # the embed ceiling → emitted whole and merely flagged (assertion 7), one chunk.
    bi = chunk_bookstack_html(
        html, length_fn=ws_len, max_tokens=10, overlap_tokens=2, embed_max_tokens=800
    )
    assert sum(c.table_count for c in bi) == 1
    assert max(c.token_count for c in bi) > 10  # licensed over-budget table
    joined_bi = "".join(c.text for c in bi)
    for i in range(12):
        assert f"ligne{i}col1" in joined_bi

    # (b-ii) max_tokens=10, embed_max_tokens=40: the table exceeds the embedder's
    # limit too → row-group split into MULTIPLE parts, each <= embed_max_tokens,
    # each repeating the header row, no row split across parts, every cell present.
    # A "flag and emit whole" implementation returns one 40+ token chunk and fails.
    bii = chunk_bookstack_html(
        html, length_fn=ws_len, max_tokens=10, overlap_tokens=2, embed_max_tokens=40
    )
    table_parts = [c for c in bii if c.table_count > 0]
    assert len(table_parts) >= 2  # genuinely split, not emitted whole
    for c in bii:
        assert c.token_count <= 40  # every part within the embed ceiling
    for c in table_parts:
        # Header row + separator repeated in every table part.
        assert "H1" in c.text and "| --- |" in c.text
    joined_bii = "".join(c.text for c in bii)
    for i in range(12):
        assert f"ligne{i}col1" in joined_bii
        assert f"ligne{i}col2" in joined_bii

    # (c) Nested table (assertion 7, nesting clause). Flattening nested <tr> into
    # the parent via .iter("tr") is intended; the duplication comes from the two
    # OTHER descendant walks over the same subtree — tr.iter("td","th") pulling
    # nested cells in as extra columns of the enclosing row, and cell.itertext()
    # splicing nested text into the enclosing cell as one mangled token. Plus the
    # split site, which must consider only tables with no <table> ancestor.
    #
    # Both real shapes are exercised. All 12 nested tables in the export sit inside
    # a cell, but only 6 have <td> as direct parent — the other 6 are wrapped in a
    # <div>, so a fix keyed on the parent tag passes half the corpus and fails the
    # rest. A direct-child nest (<table>…<table></table></table>) flattens in
    # exactly once and passes even on the fully defective code: it is a false green
    # and must not be the only shape tested.
    nested = "".join(f"<tr><td>ncell{i}</td></tr>" for i in range(3))
    outer_body = "".join(f"<tr><td>outer{i}c1</td><td>outer{i}c2</td></tr>" for i in range(6))
    shapes = {
        # nested table directly inside a <td> (6 of 12 in the real export)
        "td": f"<tr><td><table>{nested}</table></td><td>tail_cell</td></tr>",
        # nested table wrapped in a <div> inside a <td> (the other 6)
        "div_in_td": (f"<tr><td><div><table>{nested}</table></div></td><td>tail_cell</td></tr>"),
    }
    for shape_name, nested_row in shapes.items():
        nested_html = (
            '<h1 id="page-31">Nested Data Page</h1>'
            "<p>Intro paragraph before the table.</p>"
            f"<table><tr><th>H1</th><th>H2</th></tr>{outer_body}{nested_row}</table>"
        )
        cc = chunk_bookstack_html(
            nested_html,
            length_fn=ws_len,
            max_tokens=10,
            overlap_tokens=2,
            embed_max_tokens=40,
        )
        # Every nested cell appears exactly once across all returned chunks.
        for i in range(3):
            assert sum(c.text.count(f"ncell{i}") for c in cc) == 1, (
                f"{shape_name}: ncell{i} duplicated"
            )
        # The enclosing cell keeps its own siblings but never absorbs nested text:
        # no cell may be the concatenation of the nested rows.
        assert not any("ncell0ncell1" in c.text or "ncell0 ncell1 ncell2" in c.text for c in cc), (
            f"{shape_name}: nested text spliced into the enclosing cell"
        )
        assert sum(c.text.count("tail_cell") for c in cc) == 1, shape_name
        # Outer rows are untouched — the fix removes duplication, not content.
        for i in range(6):
            assert sum(c.text.count(f"outer{i}c1") for c in cc) == 1, shape_name
            assert sum(c.text.count(f"outer{i}c2") for c in cc) == 1, shape_name
        # Still a genuine row-group split within the ceiling, header repeated.
        cc_table_parts = [c for c in cc if c.table_count > 0]
        assert len(cc_table_parts) >= 2, shape_name
        for c in cc:
            assert c.token_count <= 40, shape_name
        for c in cc_table_parts:
            assert "H1" in c.text and "| --- |" in c.text, shape_name

    # A flat table must be byte-identical to its pre-fix output — the cell-ancestor
    # and itertext bounds must be no-ops when there is nothing nested.
    flat_html = (
        '<h1 id="page-32">Flat</h1><p>x</p>'
        f"<table><tr><th>H1</th><th>H2</th></tr>{outer_body}</table>"
    )
    fc = chunk_bookstack_html(
        flat_html, length_fn=ws_len, max_tokens=10, overlap_tokens=2, embed_max_tokens=40
    )
    for i in range(6):
        assert sum(c.text.count(f"outer{i}c1") for c in fc) == 1


# ---------------------------------------------------------------------------
# Case 5 — image_ordering_and_external
# ---------------------------------------------------------------------------


def test_image_ordering_and_external():
    # Two images with data-image-id, then one EXTERNAL image (buildsrv2 host) with
    # no data-image-id — the real artifact carries 142 of these.
    html = """
    <h1 id="page-40">Screens</h1>
    <p>First paragraph.</p>
    <p><img data-image-id="img-aaa" data-needs-caption="true"/></p>
    <p>Second paragraph.</p>
    <p><img data-image-id="img-bbb" data-needs-caption="false"/></p>
    <p><img src="http://buildsrv2:8090/x.png"/></p>
    """
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)

    assert len(chunks) == 1
    c = chunks[0]
    # First two placeholders appear in document order.
    assert c.text.index("[[image:img-aaa]]") < c.text.index("[[image:img-bbb]]")
    assert [r.image_id for r in c.image_refs] == ["img-aaa", "img-bbb"]
    assert [r.needs_caption for r in c.image_refs] == [True, False]
    # Each image is attributed to the (only) chunk holding the preceding text.
    assert all(r.anchor == c.anchor for r in c.image_refs)
    # The id-less external image is dropped entirely (assertion 8): absent from
    # text and refs, never an empty placeholder, never an ImageRef(image_id="").
    assert c.text.count("[[image") == 2
    assert len(c.image_refs) == 2
    assert "buildsrv2" not in c.text
    assert all(r.image_id != "" for r in c.image_refs)


# ---------------------------------------------------------------------------
# Case 6 — oversized_single_section  (separator preservation)
# ---------------------------------------------------------------------------


def test_oversized_single_section():
    max_tokens, overlap_tokens = 200, 50
    # ~720 tok in one bkmrk-* section, no sub-anchors, with sentence ("`. `") and
    # paragraph ("\n\n") separators — enough that windows land mid-sentence.
    paras = [
        " ".join(f"alpha{p}_{s} beta{p}_{s} gamma{p}_{s}." for s in range(6)) for p in range(40)
    ]
    body = "\n\n".join(paras)
    html = f'<h1 id="page-50">Long Page</h1><p id="bkmrk-big">{body}</p>'
    chunks = chunk_bookstack_html(
        html, length_fn=ws_len, max_tokens=max_tokens, overlap_tokens=overlap_tokens
    )

    # One section, > budget → recursive token-window split.
    assert len(chunks) >= 2
    for c in chunks:
        assert c.token_count <= max_tokens
        assert c.token_count == ws_len(c.text)
        assert c.anchor == "bkmrk-big"  # every window keeps the section anchor
    # Adjacent chunks share a non-empty, bounded overlap (assertion 5 — a ceiling,
    # never exact).
    for a, b in zip(chunks, chunks[1:]):
        shared = set(a.text.split()) & set(b.text.split())
        assert 0 < len(shared) <= overlap_tokens
    # Separators preserved: every "." and every paragraph break survives (overlap
    # can only duplicate, never drop), and no window fuses two words the source
    # separated by ". " with the "." now gone.
    assert sum(c.text.count(".") for c in chunks) >= body.count(".")
    assert sum(c.text.count("\n\n") for c in chunks) >= body.count("\n\n")
    import re

    consumed = re.compile(r"gamma\d+_\d+\s+alpha\d+_\d+")
    for c in chunks:
        assert consumed.search(c.text) is None


# ---------------------------------------------------------------------------
# Case 7 — provenance_and_crosslinks  (defect test: document-order chapters)
# ---------------------------------------------------------------------------


def test_provenance_and_crosslinks():
    # chapter-104 BEFORE chapter-96 on purpose: an implementation that sorts on the
    # numeric id attributes chapter-96's page to chapter-104 and fails here.
    html = """
    <div>Stray marker-less prose that belongs to no page.</div>
    <h1 id="chapter-104">Chapter Alpha</h1>
    <h1 id="page-1">Page A</h1>
    <p>Alpha page A body content here.</p>
    <h1 id="page-2">Page B</h1>
    <p id="bkmrk-links">See <a href="#bkmrk-x">X</a> then <a href="#bkmrk-y">Y</a>
       and again <a href="#bkmrk-x">X duplicated</a>.</p>
    <h1 id="chapter-96">Chapter Beta</h1>
    <h1 id="page-3">Page C</h1>
    <p>Beta page C body content here.</p>
    """
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    by_page = {c.page_id: c for c in chunks}

    # Nearest preceding chapter in DOCUMENT order, never numeric id order.
    assert by_page["page-1"].chapter_id == "chapter-104"
    assert by_page["page-1"].chapter_title == "Chapter Alpha"
    assert by_page["page-2"].chapter_id == "chapter-104"
    assert by_page["page-3"].chapter_id == "chapter-96"
    assert by_page["page-3"].chapter_title == "Chapter Beta"

    # Marker-less prose is dropped, never merged into the following page.
    assert all("Stray marker-less prose" not in c.text for c in chunks)
    assert "Stray marker-less prose" not in by_page["page-1"].text

    # related_anchors: internal hrefs, deduped, '#' stripped, order preserved.
    assert by_page["page-2"].related_anchors == ["bkmrk-x", "bkmrk-y"]


# ---------------------------------------------------------------------------
# Case 8 — anchor_collision_and_style_block  (defect test: composite key + <style>)
# ---------------------------------------------------------------------------


def test_anchor_collision_and_style_block():
    # Two pages each carrying id="bkmrk-objectifs" (the real export repeats it on 208
    # pages), with a <style> holding a base64 rule placed INSIDE the first page.
    html = """
    <h1 id="page-100">Page One</h1>
    <div id="bkmrk-objectifs">Objectives for page one.</div>
    <style>.icon { background-image: url("data:image/svg+xml;base64,PHN2Zz48L3N2Zz4="); }
           .warn { color: red; }</style>
    <p>Body content one.</p>
    <h1 id="page-200">Page Two</h1>
    <div id="bkmrk-objectifs">Objectives for page two.</div>
    <p>Body content two.</p>
    """
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)

    assert len(chunks) == 2

    # anchor alone is NOT unique — both pages yield "bkmrk-objectifs"…
    anchors = [c.anchor for c in chunks]
    assert anchors == ["bkmrk-objectifs", "bkmrk-objectifs"]
    assert len(set(anchors)) == 1
    # …but the composite (page_id, anchor) key IS unique across the list (assertion 6).
    pairs = [(c.page_id, c.anchor) for c in chunks]
    assert len(set(pairs)) == len(pairs) == 2

    # <style>/<script> removed from the tree before extraction — no CSS, no base64,
    # ever reaches Chunk.text (Forbidden entry / assertion 1's structural half).
    for c in chunks:
        assert "base64," not in c.text
        assert "background-image" not in c.text
        assert "color: red" not in c.text
        assert ".icon" not in c.text


# ---------------------------------------------------------------------------
# Input validation and the narrow generalization set (contract Inputs + assertion 10)
# ---------------------------------------------------------------------------


def test_input_validation_and_generalization_set():
    # Empty document → [] (never None).
    assert chunk_bookstack_html("", length_fn=ws_len) == []

    # Out-of-range budgets → ValueError.
    for bad in [
        dict(max_tokens=0),
        dict(overlap_tokens=-1),
        dict(max_tokens=100, overlap_tokens=100),
        dict(max_tokens=100, overlap_tokens=150),
        dict(max_tokens=100, embed_max_tokens=50),
    ]:
        try:
            chunk_bookstack_html("<h1 id='page-1'>x</h1><p>y</p>", length_fn=ws_len, **bad)
        except ValueError:
            pass
        else:  # pragma: no cover - defensive
            raise AssertionError(f"expected ValueError for {bad}")

    # A raising length_fn propagates unchanged — never swallowed.
    def boom(_text: str) -> int:
        raise RuntimeError("length_fn failed")

    try:
        chunk_bookstack_html("<h1 id='page-1'>x</h1><p>y</p>", length_fn=boom)
    except RuntimeError as exc:
        assert "length_fn failed" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected RuntimeError to propagate")

    # 'toutes les' is deliberately excluded from the narrow set (assertion 10).
    assert "toutes les" not in GENERALIZATION_PHRASES
    assert isinstance(chunk_bookstack_html("", length_fn=ws_len), list)
    # Chunk is the public return element type.
    one = chunk_bookstack_html("<h1 id='page-1'>T</h1><p>body here</p>", length_fn=ws_len)
    assert isinstance(one[0], Chunk)


# ---------------------------------------------------------------------------
# Assertion 10 (amended) — module_candidates provenance, scope, full-name
# preservation. Every test below is DISCRIMINATING: it fails against the
# previous page-scoped, truncating _module_candidates. See
# reviews/ingestion_chunking_assertion10_summary.md for the per-test rejection.
# ---------------------------------------------------------------------------


def test_module_candidates_scope_excludes_foreign_parens():
    # A parenthesized identifier group lives in a NON-generalization sentence;
    # the generalization sentence carries its own group. Scope is the flagged
    # chunk's own generalization sentence, so only its names appear.
    # Rejects OLD page-scope: it swept SIRET/SIREN/NIC into the flagged chunk.
    html = (
        '<h1 id="page-2001">Scope</h1>'
        '<p id="bkmrk-a">Les identifiants (SIRET, SIREN, NIC) figurent dans la table.</p>'
        '<p id="bkmrk-b">Pour les autres modules (Commandes, Factures), '
        "la même démarche s'applique.</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert {"SIRET", "SIREN", "NIC"}.isdisjoint(chunks[0].module_candidates)


def test_module_candidates_preserves_multi_word_names():
    # Full multi-word names are kept whole — no head-word truncation.
    # Rejects OLD split(" ")[0]: it yielded ['Sorties', 'Bons', 'Notes'].
    html = (
        '<h1 id="page-2002">Multi</h1>'
        '<p id="bkmrk-a">Pour les autres modules (Sorties de stock, Bons de commande, '
        "Notes de crédit), la même démarche s'applique.</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert "Sorties" not in chunks[0].module_candidates


def test_module_candidates_parenthesis_aware_sentence_split():
    # A distractor parens group sits in the sentence BEFORE the generalization
    # sentence; the generalization group ends with 'etc..'. Sentence splitting is
    # parenthesis-aware and drops 'etc' plus trailing punctuation, yielding
    # exactly the three module names. Rejects OLD page-scope, which also swept the
    # distractor 'Total' from the earlier (Total HT, Total TTC) group.
    html = (
        '<h1 id="page-2003">Paren</h1>'
        '<p id="bkmrk-a">Les colonnes calculées (Total HT, Total TTC) ne sont pas des modules. '
        "Pour les autres modules (Commandes, Réceptions, Factures etc..), "
        "la même démarche s'applique.</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert set(chunks[0].module_candidates) == {"Commandes", "Réceptions", "Factures"}


def test_module_candidates_rejects_over_word_cap_clause():
    # A parenthetical clause longer than MAX_MODULE_WORDS words is not a name.
    # Rejects OLD split(" ")[0], which truncated the clause to its head 'Le'.
    assert MAX_MODULE_WORDS < len(["Le", "même", "principe", "que", "le", "bouton", "supprimer"])
    html = (
        '<h1 id="page-2004">Cap</h1>'
        '<p id="bkmrk-a">Le même principe (Le même principe que le bouton supprimer) '
        "s'applique.</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert chunks[0].module_candidates == []


def test_module_candidates_rejects_digits_and_symbols():
    # Digit-bearing and symbol tokens are not names. A digit-bearing token
    # ('Écran 2024') is rejected whole — no fallback to a leading letters-only
    # head. Rejects OLD split(" ")[0], which truncated 'Écran 2024' to 'Écran'.
    html = (
        '<h1 id="page-2005">Reject</h1>'
        '<p id="bkmrk-a">Pour les autres modules (Écran 2024, 19, P2P, $int32), '
        "la même démarche s'applique.</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    assert chunks[0].module_candidates == []


def test_unflagged_chunk_paren_list_does_not_leak():
    # One page, two packed chunks. chunk 0 carries the generalization phrase and
    # NO parens; chunk 1 has no phrase but holds the page's only parenthesized
    # capitalized list. The unflagged chunk 1 stays False/[] (contract invariant),
    # and — the discriminating fact — its (Commandes, Factures) never surfaces on
    # the flagged sibling chunk 0. Rejects OLD page-scope, which computed
    # candidates over the whole page and attached them to every flagged chunk
    # regardless of which chunk the parens lived in.
    html = (
        '<h1 id="page-2006">Unflagged</h1>'
        f'<p id="bkmrk-a">Pour les autres modules, la même démarche s\'applique. '
        f"{_words(500, 'a')}</p>"
        f'<p id="bkmrk-b">Les écrans concernés sont (Commandes, Factures). '
        f"{_words(500, 'b')}</p>"
    )
    chunks = chunk_bookstack_html(html, length_fn=ws_len, max_tokens=800)
    # Contract invariant on the unflagged chunk (holds under OLD too — see summary).
    assert chunks[1].is_generalized_procedure is False
    assert chunks[1].module_candidates == []
    # Discriminator: the flagged sibling never inherits chunk 1's page-wide parens.
    assert chunks[0].module_candidates == []
