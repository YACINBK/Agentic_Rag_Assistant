"""Capture live retrieval, then compare pruning on identical frozen candidates.

Run with python -m scripts.evaluate_retrieval. Outputs contain internal document
text and belong in an ignored local directory, not in a public benchmark.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path


def load_cases(path: Path) -> list[dict]:
    cases = json.loads(path.read_text(encoding="utf-8"))["cases"]
    if not cases:
        raise ValueError("At least one case is required")
    identifiers = set()
    for case in cases:
        for key in ("id", "query", "role"):
            if not isinstance(case.get(key), str) or not case[key].strip():
                raise ValueError(f"Case requires a nonempty {key}")
        if type(case.get("is_admin")) is not bool:
            raise ValueError("is_admin must be a boolean")
        if case["id"] in identifiers:
            raise ValueError(f"Duplicate case ID: {case['id']}")
        identifiers.add(case["id"])
    return cases


def write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as output:
        json.dump(value, output, indent=2, ensure_ascii=False, allow_nan=False)
        output.write("\n")


def make_review_template(snapshot: dict, digest: str) -> dict:
    return {
        "version": 1,
        "reviewer": None,
        "review_kind": None,
        "snapshot_sha256": digest,
        "instructions": (
            "Set reviewer and review_kind (human or assistant_provisional). Review every "
            "candidate in each included case; omit unfinished cases. Candidate order is by "
            "ID, not score. Keep answerable null unless corpus answerability is established "
            "for this user scope, and explain the basis. A retrieval miss alone does not "
            "prove that the corpus lacks an answer. Do not use reranker scores as labels. "
            "Raw excerpts may contain confidential information; keep this file private."
        ),
        "rubric": {
            "0": "Does not support the requested task; shared terminology alone is insufficient.",
            "1": "Useful prerequisite, navigation, parameter, or caveat for the requested task.",
            "2": "Direct textual evidence answering at least part of the question.",
        },
        "cases": [
            {
                **{key: case[key] for key in ("id", "query", "role", "is_admin")},
                "answerable": None,
                "answerability_basis": None,
                "judgments": [
                    {
                        "chunk_id": chunk["chunk_id"], "grade": None, "reason": None,
                        "text": chunk["text"], "section_path": chunk.get("section_path", ""),
                        "original_filename": chunk.get("original_filename", ""),
                    }
                    for chunk in sorted(case["reranked_chunks"], key=lambda item: item["chunk_id"])
                ],
            }
            for case in snapshot["cases"]
        ],
    }


async def capture(cases_path: Path, output_path: Path) -> dict:
    import httpx
    from qdrant_client import AsyncQdrantClient

    from app.core.settings import settings
    from app.pipeline.nodes.node_03_parallel_qdrant_search import QdrantSearchNode
    from app.pipeline.nodes.node_04_reranker import RerankerNode
    from app.services.embedder import OllamaEmbedder
    from app.services.reranker import TEIReranker
    from app.services.vector_store import QdrantVectorStore

    review_path = output_path.with_name(f"{output_path.stem}.review.json")
    if output_path.exists() or review_path.exists():
        raise FileExistsError("Choose new output paths; snapshots are not overwritten")
    cases = load_cases(cases_path)
    case_set = json.loads(cases_path.read_text(encoding="utf-8"))
    client = AsyncQdrantClient(host=settings.QDRANT_HOST, port=settings.QDRANT_PORT)
    search = QdrantSearchNode(OllamaEmbedder(), QdrantVectorStore(client), settings)
    rerank = RerankerNode(TEIReranker(), settings)
    try:
        collection = await client.get_collection(settings.QDRANT_COLLECTION)
        async with httpx.AsyncClient(timeout=10) as http:
            response = await http.get(f"{settings.RERANKER_URL}/info")
            response.raise_for_status()
            reranker_info = response.json()
            response = await http.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
            response.raise_for_status()
            model_name = settings.EMBEDDING_MODEL.removesuffix(":latest")
            embedding_info = next(
                (model for model in response.json().get("models", [])
                 if model["name"].removesuffix(":latest") == model_name),
                {},
            )
        manifest = []
        offset = None
        while True:
            points, offset = await client.scroll(
                settings.QDRANT_COLLECTION,
                limit=256,
                offset=offset,
                with_payload=["document_id", "doc_hash", "allowed_roles", "admin_only"],
                with_vectors=False,
            )
            manifest.extend({"id": str(point.id), "payload": point.payload} for point in points)
            if offset is None:
                break
        records = []
        for case in cases:
            state = {
                "query": case["query"],
                "rewritten_query": case["query"],
                "user_role": case["role"],
                "user_is_admin": case["is_admin"],
            }
            started = time.perf_counter()
            state = await search.execute(state)
            retrieval_ms = (time.perf_counter() - started) * 1000
            started = time.perf_counter()
            state = await rerank.execute(state)
            rerank_ms = (time.perf_counter() - started) * 1000
            chunks = state["reranked_chunks"]
            if not all(math.isfinite(chunk["score"]) for chunk in chunks):
                raise ValueError(f"Non-finite reranker score for {case['id']}")
            metadata = await client.retrieve(
                settings.QDRANT_COLLECTION,
                ids=[chunk["chunk_id"] for chunk in chunks],
                with_payload=["doc_hash", "allowed_roles", "admin_only"],
                with_vectors=False,
            ) if chunks else []
            by_id = {str(point.id): point.payload for point in metadata}
            violations = []
            for chunk in chunks:
                payload = by_id.get(chunk["chunk_id"], {})
                role_allowed = bool(set(payload.get("allowed_roles", [])) & {case["role"], "all"})
                privilege_allowed = case["is_admin"] or payload.get("admin_only") is False
                if not role_allowed or not privilege_allowed:
                    violations.append(chunk["chunk_id"])
            records.append({
                **case,
                "retrieval_ms": retrieval_ms,
                "rerank_ms": rerank_ms,
                "retrieved_chunks": state["retrieved_chunks"],
                "reranked_chunks": chunks,
                "source_metadata": by_id,
                "authorization_violations": violations,
            })
            top_score = max((chunk["score"] for chunk in chunks), default=0)
            print(f"{case['id']}: {len(chunks)} candidates, top={top_score:.6f}", flush=True)
        snapshot = {
            "version": 1,
            "captured_at": datetime.now(timezone.utc).isoformat(),
            "git_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True,
            ).strip(),
            "cases_sha256": hashlib.sha256(cases_path.read_bytes()).hexdigest(),
            "case_set_split": case_set.get("split", "development"),
            "case_set_scope": case_set.get("scope"),
            "scope": "retrieval_only_fixed_queries_no_rewriter_cache_or_generator",
            "timing_scope": "single pass; cold starts included; not end-to-end answer latency",
            "collection": settings.QDRANT_COLLECTION,
            "collection_points": collection.points_count,
            "corpus_manifest": sorted(manifest, key=lambda entry: entry["id"]),
            "embedding_model": settings.EMBEDDING_MODEL,
            "embedding_digest": embedding_info.get("digest"),
            "reranker": {key: reranker_info.get(key) for key in (
                "model_id", "model_sha", "max_input_length", "version",
            )},
            "search_limit": settings.QDRANT_SEARCH_LIMIT,
            "gate_threshold": settings.RELEVANCE_THRESHOLD,
            "cases": records,
        }
        write_json(output_path, snapshot)
        write_json(review_path, make_review_template(
            snapshot, hashlib.sha256(output_path.read_bytes()).hexdigest(),
        ))
        return snapshot
    finally:
        await client.close()


def reviewed_labels(snapshot: dict, review: dict | None) -> dict:
    if not review or not str(review.get("reviewer") or "").strip():
        return {}
    if review.get("review_kind") not in ("human", "assistant_provisional"):
        raise ValueError("review_kind must identify human or assistant_provisional labels")
    expected = {case["id"]: case for case in snapshot["cases"]}
    labels = {}
    for case in review["cases"]:
        if case["id"] not in expected or case["id"] in labels:
            raise ValueError("Unknown or duplicate reviewed case")
        judgments = case["judgments"]
        by_id = {judgment["chunk_id"]: judgment["grade"] for judgment in judgments}
        required = {chunk["chunk_id"] for chunk in expected[case["id"]]["reranked_chunks"]}
        if len(by_id) != len(judgments) or set(by_id) != required:
            raise ValueError("Reviewed judgments must cover exactly the frozen candidate pool")
        if any(type(grade) is not int or grade not in (0, 1, 2) for grade in by_id.values()):
            raise ValueError("Every candidate needs a reviewed grade of 0, 1, or 2")
        answerable = case.get("answerable")
        if answerable is not None and type(answerable) is not bool:
            raise ValueError("Reviewed answerable must be a boolean or null")
        if answerable is not None and not str(case.get("answerability_basis") or "").strip():
            raise ValueError("Known answerability requires an answerability_basis")
        labels[case["id"]] = {"grades": by_id, "answerable": answerable}
    return labels


async def compare(snapshot: dict, thresholds: list[float], review: dict | None = None) -> dict:
    from app.core.settings import Settings
    from app.pipeline.nodes.node_05_relevance_gate import RelevanceGateNode

    if snapshot.get("case_set_split") == "held_out" and len({value for value in thresholds if value != 0}) > 1:
        raise ValueError("Held-out evaluation accepts one preselected threshold plus optional baseline 0; tune on development cases")
    labels = reviewed_labels(snapshot, review)
    gate_threshold = snapshot["gate_threshold"]
    baseline_chars = sum(
        sum(len(chunk["text"]) for chunk in case["reranked_chunks"])
        for case in snapshot["cases"]
        if case["reranked_chunks"] and max(
            chunk["score"] for chunk in case["reranked_chunks"]
        ) >= gate_threshold
    )
    results = []
    for threshold in thresholds:
        settings = Settings(_env_file=None, RELEVANCE_THRESHOLD=gate_threshold,
                            CONTEXT_PRUNE_THRESHOLD=threshold)
        node = RelevanceGateNode(settings)
        rows = []
        retained_relevant = retained_labeled = relevant_pool = correct_gate = 0
        known_answerability = 0
        gate_confusion = {"true_positive": 0, "false_positive": 0, "true_negative": 0, "false_negative": 0}
        retained_by_grade = {1: 0, 2: 0}
        pool_by_grade = {1: 0, 2: 0}
        pruned_by_grade = {1: 0, 2: 0}
        for case in snapshot["cases"]:
            chunks = case["reranked_chunks"]
            result = await node.execute({"reranked_chunks": chunks})
            retained = result["reranked_chunks"] if result["relevance_pass"] else []
            baseline_pass = bool(chunks) and max(chunk["score"] for chunk in chunks) >= gate_threshold
            rows.append({
                "id": case["id"], "gate_pass": result["relevance_pass"],
                "gate_changed": result["relevance_pass"] != baseline_pass,
                "candidate_count": len(chunks), "context_count": len(retained),
                "context_characters": sum(len(chunk["text"]) for chunk in retained),
                "retained_chunk_ids": [chunk["chunk_id"] for chunk in retained],
            })
            if case.get("expected_chunk_ids"):
                expected_ids = set(case["expected_chunk_ids"])
                rows[-1]["pinned_evidence"] = {
                    "expected_chunk_ids": sorted(expected_ids),
                    "missing_candidate_ids": sorted(expected_ids - {chunk["chunk_id"] for chunk in chunks}),
                    "missing_context_ids": sorted(expected_ids - {chunk["chunk_id"] for chunk in retained}),
                }
            if case["id"] in labels:
                label = labels[case["id"]]
                retained_ids = {chunk["chunk_id"] for chunk in retained}
                retained_relevant += sum(label["grades"][chunk["chunk_id"]] > 0 for chunk in retained)
                retained_labeled += len(retained)
                relevant_pool += sum(grade > 0 for grade in label["grades"].values())
                for grade, kind in ((1, "support"), (2, "direct")):
                    matching_ids = {
                        chunk_id for chunk_id, value in label["grades"].items() if value == grade
                    }
                    retained_by_grade[grade] += len(matching_ids & retained_ids)
                    pool_by_grade[grade] += len(matching_ids)
                    pruned = matching_ids - retained_ids if result["relevance_pass"] else set()
                    pruned_by_grade[grade] += len(pruned)
                    rows[-1][f"pruned_{kind}_chunk_ids"] = sorted(pruned)
                if label["answerable"] is not None:
                    known_answerability += 1
                    correct_gate += result["relevance_pass"] == label["answerable"]
                    outcome = "true" if result["relevance_pass"] == label["answerable"] else "false"
                    prediction = "positive" if result["relevance_pass"] else "negative"
                    gate_confusion[f"{outcome}_{prediction}"] += 1
        characters = sum(row["context_characters"] for row in rows)
        pinned = [row["pinned_evidence"] for row in rows if "pinned_evidence" in row]
        negative_cases = gate_confusion["true_negative"] + gate_confusion["false_positive"]
        results.append({
            "threshold": threshold,
            "gate_passes": sum(row["gate_pass"] for row in rows),
            "gate_changes": sum(row["gate_changed"] for row in rows),
            "pinned_evidence_case_count": len(pinned),
            "pinned_evidence_candidate_hits": sum(not check["missing_candidate_ids"] for check in pinned),
            "pinned_evidence_context_hits": sum(not check["missing_context_ids"] for check in pinned),
            "context_chunks": sum(row["context_count"] for row in rows),
            "context_characters": characters,
            "context_reduction_percent": 100 * (1 - characters / baseline_chars) if baseline_chars else None,
            "reviewed_case_count": len(labels),
            "reviewed_context_chunks": retained_labeled,
            "reviewed_relevant_pool": relevant_pool,
            "candidate_pool_precision": retained_relevant / retained_labeled if retained_labeled else None,
            "candidate_pool_recall": retained_relevant / relevant_pool if relevant_pool else None,
            "direct_evidence_recall": retained_by_grade[2] / pool_by_grade[2] if pool_by_grade[2] else None,
            "support_evidence_recall": retained_by_grade[1] / pool_by_grade[1] if pool_by_grade[1] else None,
            "pruned_direct_chunks": pruned_by_grade[2] if labels else None,
            "pruned_support_chunks": pruned_by_grade[1] if labels else None,
            "answerability_reviewed_count": known_answerability,
            "answerability_unknown_count": len(snapshot["cases"]) - known_answerability,
            "gate_confusion": gate_confusion,
            "gate_specificity": gate_confusion["true_negative"] / negative_cases if negative_cases else None,
            "gate_accuracy": correct_gate / known_answerability if known_answerability else None,
            "cases": rows,
        })
    return {
        "scope": "paired frozen-candidate pruning comparison; not full-corpus recall or answer quality",
        "case_set_split": snapshot.get("case_set_split", "development"),
        "label_provenance": {
            "review_kind": review["review_kind"] if labels else None,
            "reviewer": review["reviewer"] if labels else None,
            "case_ids": sorted(labels),
            "all_cases_reviewed": bool(labels) and len(labels) == len(snapshot["cases"]),
            "metrics_scope": "Quality metrics use only labeled cases; answerability may be unknown.",
        },
        "case_count": len(snapshot["cases"]),
        "authorization_candidate_checks": sum(len(case["reranked_chunks"]) for case in snapshot["cases"]),
        "authorization_violations": sum(len(case["authorization_violations"]) for case in snapshot["cases"]),
        "gate_threshold": gate_threshold,
        "results": results,
    }


def retrieval_fingerprint(snapshot: dict) -> dict:
    manifest = json.dumps(snapshot["corpus_manifest"], sort_keys=True, separators=(",", ":"))
    return {
        **{key: snapshot[key] for key in (
            "collection", "collection_points", "embedding_model", "embedding_digest", "reranker", "search_limit",
        )},
        "corpus_manifest_sha256": hashlib.sha256(manifest.encode()).hexdigest(),
    }


def implementation_fingerprint() -> dict:
    root = Path(__file__).resolve().parents[1]
    return {
        str(path): hashlib.sha256((root / path).read_bytes()).hexdigest()
        for path in (Path("scripts/evaluate_retrieval.py"), Path("app/pipeline/nodes/node_05_relevance_gate.py"))
    }


async def freeze_policy(
    snapshot_path: Path, review_path: Path, held_out_cases_path: Path, threshold: float,
) -> dict:
    snapshot_bytes = snapshot_path.read_bytes()
    review_bytes = review_path.read_bytes()
    snapshot = json.loads(snapshot_bytes)
    review = json.loads(review_bytes)
    if snapshot.get("case_set_split") == "held_out":
        raise ValueError("A policy must be selected on development data, not held-out data")
    if review.get("snapshot_sha256") != hashlib.sha256(snapshot_bytes).hexdigest():
        raise ValueError("Review belongs to a different snapshot")
    labels = reviewed_labels(snapshot, review)
    if not labels or len(labels) != len(snapshot["cases"]):
        raise ValueError("Freeze requires a complete candidate review for every development case")
    held_out_cases = load_cases(held_out_cases_path)
    held_out_bytes = held_out_cases_path.read_bytes()
    if json.loads(held_out_bytes).get("split") != "held_out":
        raise ValueError("The reserved case set must declare split held_out")
    if {case["query"].casefold().strip() for case in held_out_cases} & {
        case["query"].casefold().strip() for case in snapshot["cases"]
    }:
        raise ValueError("Held-out questions must not repeat development questions")
    report = await compare(snapshot, [0, threshold], review)
    baseline, candidate = report["results"]
    if report["authorization_violations"]:
        raise ValueError("Resolve authorization violations before freezing a policy")
    if candidate["gate_changes"] or candidate["pruned_direct_chunks"] or candidate["pruned_support_chunks"]:
        raise ValueError("The conservative policy may not remove reviewed useful evidence beyond baseline admission")
    if not candidate["context_reduction_percent"] or candidate["context_reduction_percent"] <= 0:
        raise ValueError("The experimental policy must reduce context compared with disabled pruning")
    return {
        "version": 1,
        "status": "frozen_for_offline_holdout",
        "evaluation_only": True,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "gate_threshold": snapshot["gate_threshold"],
        "context_prune_threshold": threshold,
        "held_out_cases_sha256": hashlib.sha256(held_out_bytes).hexdigest(),
        "development_snapshot_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "development_review_sha256": hashlib.sha256(review_bytes).hexdigest(),
        "label_provenance": report["label_provenance"],
        "retrieval_fingerprint": retrieval_fingerprint(snapshot),
        "implementation_fingerprint": implementation_fingerprint(),
        "selection_rule": "Reduce context without pruning reviewed useful evidence beyond the baseline gate; not a claim of global optimality.",
        "development_results": [
            {key: value for key, value in result.items() if key != "cases"}
            for result in (baseline, candidate)
        ],
        "limitations": [
            "Candidate-pool, text-only, development labels; no held-out answer-quality claim.",
            "Assistant-provisional labels are not human validation.",
            "Unknown corpus answerability is not a negative label; specificity needs reviewed negatives.",
            "Reported model metadata is locked; an absent reranker weight revision cannot be verified byte-for-byte.",
            "This file does not enable application pruning or change environment settings.",
        ],
    }


def policy_thresholds(snapshot: dict, policy: dict) -> list[float]:
    from app.core.settings import Settings

    if policy.get("version") != 1 or policy.get("status") != "frozen_for_offline_holdout" or policy.get("evaluation_only") is not True:
        raise ValueError("Expected a frozen evaluation-only policy")
    if snapshot.get("case_set_split") != "held_out":
        raise ValueError("A frozen held-out policy requires a held_out snapshot")
    if snapshot["cases_sha256"] != policy["held_out_cases_sha256"]:
        raise ValueError("Held-out case set differs from the frozen policy")
    if snapshot["gate_threshold"] != policy["gate_threshold"]:
        raise ValueError("Admission gate differs from the frozen policy")
    if retrieval_fingerprint(snapshot) != policy["retrieval_fingerprint"]:
        raise ValueError("Corpus or retrieval metadata differs from the frozen policy")
    if implementation_fingerprint() != policy["implementation_fingerprint"]:
        raise ValueError("Evaluation or gate implementation differs from the frozen policy")
    settings = Settings(_env_file=None, RELEVANCE_THRESHOLD=policy["gate_threshold"],
                        CONTEXT_PRUNE_THRESHOLD=policy["context_prune_threshold"])
    if settings.CONTEXT_PRUNE_THRESHOLD == 0:
        raise ValueError("A frozen experimental cutoff must be positive")
    return [0, settings.CONTEXT_PRUNE_THRESHOLD]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    capture_command = commands.add_parser("capture")
    capture_command.add_argument("--cases", type=Path, default=Path("evals/retrieval_cases.json"))
    capture_command.add_argument("--output", type=Path, required=True)
    review_command = commands.add_parser("review-template", help="Export score-hidden review material")
    review_command.add_argument("--snapshot", type=Path, required=True)
    review_command.add_argument("--output", type=Path, required=True)
    freeze_command = commands.add_parser("freeze-policy", help="Lock a recall-preserving development candidate for offline held-out evaluation")
    freeze_command.add_argument("--snapshot", type=Path, required=True)
    freeze_command.add_argument("--review", type=Path, required=True)
    freeze_command.add_argument("--held-out-cases", type=Path, required=True)
    freeze_command.add_argument("--threshold", type=float, required=True)
    freeze_command.add_argument("--output", type=Path, required=True)
    compare_command = commands.add_parser("compare")
    compare_command.add_argument("--snapshot", type=Path, required=True)
    compare_command.add_argument("--review", type=Path)
    comparison_options = compare_command.add_mutually_exclusive_group()
    comparison_options.add_argument("--thresholds", type=float, nargs="+")
    comparison_options.add_argument("--policy", type=Path, help="Use the frozen policy and baseline only; required for held-out CLI comparisons")
    compare_command.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    if arguments.command == "capture":
        asyncio.run(capture(arguments.cases, arguments.output))
    elif arguments.command == "freeze-policy":
        policy = asyncio.run(freeze_policy(
            arguments.snapshot, arguments.review, arguments.held_out_cases, arguments.threshold,
        ))
        write_json(arguments.output, policy)
    else:
        snapshot = json.loads(arguments.snapshot.read_text(encoding="utf-8"))
        digest = hashlib.sha256(arguments.snapshot.read_bytes()).hexdigest()
        if arguments.command == "review-template":
            write_json(arguments.output, make_review_template(snapshot, digest))
            return
        review = json.loads(arguments.review.read_text(encoding="utf-8")) if arguments.review else None
        if review and review.get("snapshot_sha256") != digest:
            raise ValueError("Review belongs to a different snapshot")
        if arguments.policy:
            policy = json.loads(arguments.policy.read_text(encoding="utf-8"))
            thresholds = policy_thresholds(snapshot, policy)
        else:
            if snapshot.get("case_set_split") == "held_out":
                raise ValueError("Held-out comparisons require --policy; freeze on development data first")
            thresholds = arguments.thresholds or [0, 0.05, 0.1, 0.2, 0.5]
        result = asyncio.run(compare(snapshot, thresholds, review))
        result["policy_sha256"] = hashlib.sha256(arguments.policy.read_bytes()).hexdigest() if arguments.policy else None
        result["snapshot_sha256"] = digest
        result["review_sha256"] = hashlib.sha256(arguments.review.read_bytes()).hexdigest() if review else None
        result["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        write_json(arguments.output, result)
        print(json.dumps({**result, "results": [
            {key: value for key, value in row.items() if key != "cases"}
            for row in result["results"]
        ]}, indent=2))


if __name__ == "__main__":
    main()
