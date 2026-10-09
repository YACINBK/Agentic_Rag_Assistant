#!/usr/bin/env python
"""Sweep RELEVANCE_THRESHOLD over frozen retrieval snapshots.

Codex's 2026-10-07 work swept CONTEXT_PRUNE_THRESHOLD with the gate pinned at
0.5, so the gate's own value received none of the rigour the prune's did -- and
the held-out run surfaced one case (holdout_issued_equipment) where direct
evidence scored 0.1192 and was rejected by the 0.5 gate. `gate_specificity` came
back null because no case carried a negative answerability label.

This script closes that. The gate decision is `max(reranked score) >= T`, and a
snapshot already holds every candidate's score, so the whole sweep is offline
recomputation -- no embedder, no Qdrant, no reranker, and no risk of a different
model revision silently changing the numbers.

Positives are read from the existing provisional reviews. Negatives come from
evals/answerability_labels.json, which establishes each from authorized source
documents rather than from retrieval failure.

Prints and writes IDs, scores and verdicts only -- never corpus text, which the
snapshots' own limitations note marks confidential.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load(path: Path) -> dict:
    with path.open(encoding="utf-8") as handle:
        return json.load(handle)


def top_score(case: dict) -> float | None:
    chunks = case.get("reranked_chunks") or []
    scores = [c["score"] for c in chunks if "score" in c]
    return max(scores) if scores else None


def direct_evidence_top(case: dict, graded: dict) -> float | None:
    """Highest score among chunks a reviewer graded as direct evidence."""
    direct = {cid for cid, grade in graded.items() if grade == "direct"}
    if not direct:
        return None
    scores = [c["score"] for c in case.get("reranked_chunks", []) if c["chunk_id"] in direct]
    return max(scores) if scores else None


def collect_labels(reviews: list[dict], negatives: dict, split: str) -> dict:
    """case_id -> {answerable, basis_kind, graded}. Negatives override unknowns."""
    labels: dict[str, dict] = {}
    for review in reviews:
        for case in review.get("cases", []):
            answerable = case.get("answerable")
            graded = {}
            for cand in case.get("candidates", []) or []:
                cid = cand.get("chunk_id") or cand.get("id")
                grade = cand.get("grade") or cand.get("relevance")
                if cid and grade:
                    graded[cid] = grade
            prior = labels.get(case["id"], {})
            labels[case["id"]] = {
                "answerable": answerable if answerable is not None else prior.get("answerable"),
                "basis_kind": "provisional_review" if answerable is not None else prior.get("basis_kind"),
                "graded": graded or prior.get("graded", {}),
            }
    for case in negatives.get("cases", []):
        if case.get("split") not in (split, None):
            continue
        entry = labels.setdefault(case["id"], {"graded": {}})
        if entry.get("answerable") is True:
            raise ValueError(
                f"{case['id']} is labeled answerable by review and false by the "
                "source-grounded labels; resolve before sweeping"
            )
        entry["answerable"] = False
        entry["basis_kind"] = case["negative_kind"]
    return labels


def sweep(snapshot: dict, labels: dict, thresholds: list[float]) -> dict:
    rows = []
    for threshold in thresholds:
        confusion = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0}
        unknown = 0
        misses = []
        for case in snapshot["cases"]:
            score = top_score(case)
            passes = score is not None and score >= threshold
            label = labels.get(case["id"], {})
            answerable = label.get("answerable")
            if answerable is None:
                unknown += 1
                continue
            if answerable and passes:
                confusion["true_positive"] += 1
            elif answerable and not passes:
                confusion["false_negative"] += 1
                misses.append({
                    "id": case["id"],
                    "top_score": score,
                    "direct_evidence_top_score": direct_evidence_top(case, label.get("graded", {})),
                })
            elif not answerable and passes:
                confusion["false_positive"] += 1
            else:
                confusion["true_negative"] += 1

        positives = confusion["true_positive"] + confusion["false_negative"]
        negatives = confusion["true_negative"] + confusion["false_positive"]
        known = positives + negatives
        rows.append({
            "threshold": threshold,
            "gate_passes": sum(
                1 for c in snapshot["cases"]
                if (s := top_score(c)) is not None and s >= threshold
            ),
            "confusion": confusion,
            "sensitivity": confusion["true_positive"] / positives if positives else None,
            "specificity": confusion["true_negative"] / negatives if negatives else None,
            "balanced_accuracy": (
                (confusion["true_positive"] / positives + confusion["true_negative"] / negatives) / 2
                if positives and negatives else None
            ),
            "accuracy": (confusion["true_positive"] + confusion["true_negative"]) / known if known else None,
            "labeled_case_count": known,
            "unknown_case_count": unknown,
            "false_negatives": misses,
        })
    return {"thresholds": rows}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", required=True, type=Path)
    parser.add_argument("--review", action="append", required=True, type=Path)
    parser.add_argument("--negatives", default=Path("evals/answerability_labels.json"), type=Path)
    parser.add_argument("--split", required=True)
    parser.add_argument("--thresholds", default="0.9,0.7,0.5,0.4,0.3,0.2,0.1,0.05,0.02,0.01")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()

    snapshot = load(args.snapshot)
    labels = collect_labels([load(p) for p in args.review], load(args.negatives), args.split)
    thresholds = [float(t) for t in args.thresholds.split(",")]
    result = sweep(snapshot, labels, thresholds)
    result["split"] = args.split
    result["snapshot"] = args.snapshot.name
    result["case_count"] = len(snapshot["cases"])

    header = f"{'thr':<7}{'pass':<6}{'TP':<4}{'FN':<4}{'FP':<4}{'TN':<4}{'sens':<8}{'spec':<8}{'balAcc':<8}"
    print(f"=== {args.split} ({len(snapshot['cases'])} cases) ===")
    print(header)

    def fmt(value: float | None) -> str:
        return f"{value:.3f}" if isinstance(value, float) else "-"

    for row in result["thresholds"]:
        c = row["confusion"]
        print(
            f"{row['threshold']:<7}{row['gate_passes']:<6}{c['true_positive']:<4}"
            f"{c['false_negative']:<4}{c['false_positive']:<4}{c['true_negative']:<4}"
            f"{fmt(row['sensitivity']):<8}{fmt(row['specificity']):<8}{fmt(row['balanced_accuracy']):<8}"
        )
    print(f"labeled={result['thresholds'][0]['labeled_case_count']} "
          f"unknown={result['thresholds'][0]['unknown_case_count']}")

    if args.out:
        args.out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {args.out}")


if __name__ == "__main__":
    main()
