from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from scripts.evaluate_retrieval import (
    compare,
    freeze_policy,
    load_cases,
    main,
    make_review_template,
    policy_thresholds,
    reviewed_labels,
    write_json,
)
from tests.conftest import make_chunk


def make_snapshot() -> dict:
    return {
        "case_set_split": "development",
        "cases_sha256": "development-cases",
        "collection": "test_documents",
        "collection_points": 4,
        "corpus_manifest": [{"id": "direct", "payload": {"doc_hash": "document-revision"}}],
        "embedding_model": "test-embedder",
        "embedding_digest": "embedding-revision",
        "reranker": {"model_id": "test-reranker", "model_sha": "reranker-revision"},
        "search_limit": 3,
        "gate_threshold": 0.5,
        "cases": [
            {
                "id": "answerable",
                "query": "How do I complete the task?", "role": "developer", "is_admin": False,
                "reranked_chunks": [
                    make_chunk(chunk_id="direct", score=0.9, text="a" * 10),
                    make_chunk(chunk_id="support", score=0.2, text="b" * 20),
                    make_chunk(chunk_id="noise", score=0.01, text="c" * 30),
                ],
                "authorization_violations": [],
            },
            {
                "id": "unanswerable",
                "query": "What is absent from this corpus?", "role": "developer", "is_admin": False,
                "reranked_chunks": [make_chunk(chunk_id="other", score=0.1, text="d" * 40)],
                "authorization_violations": [],
            },
        ],
    }


def make_review() -> dict:
    return {
        "reviewer": "test-fixture",
        "review_kind": "human",
        "cases": [
            {
                "id": "answerable", "answerable": True,
                "answerability_basis": "The fixture includes direct task evidence.",
                "judgments": [
                    {"chunk_id": "direct", "grade": 2},
                    {"chunk_id": "support", "grade": 1},
                    {"chunk_id": "noise", "grade": 0},
                ],
            },
            {
                "id": "unanswerable", "answerable": False,
                "answerability_basis": "The complete toy fixture has no answer.",
                "judgments": [{"chunk_id": "other", "grade": 0}],
            },
        ],
    }


async def test_comparison_measures_only_admitted_context() -> None:
    snapshot = make_snapshot()
    before = copy.deepcopy(snapshot)

    report = await compare(snapshot, [0, 0.1])
    baseline, pruned = report["results"]

    assert baseline["context_characters"] == 60
    assert pruned["context_characters"] == 30
    assert pruned["context_reduction_percent"] == 50
    assert pruned["context_chunks"] == 2
    assert pruned["gate_changes"] == 0
    assert pruned["gate_passes"] == 1
    assert snapshot == before


async def test_unreviewed_data_never_produces_quality_metrics() -> None:
    report = await compare(make_snapshot(), [0.1])
    result = report["results"][0]

    assert result["reviewed_case_count"] == 0
    assert result["candidate_pool_precision"] is None
    assert result["candidate_pool_recall"] is None
    assert result["gate_accuracy"] is None


async def test_reviewed_context_recall_exposes_overpruning() -> None:
    report = await compare(make_snapshot(), [0, 0.1, 0.5], make_review())
    baseline, support_preserved, aggressive = report["results"]

    assert baseline["candidate_pool_precision"] == pytest.approx(2 / 3)
    assert support_preserved["candidate_pool_precision"] == 1
    assert support_preserved["candidate_pool_recall"] == 1
    assert aggressive["candidate_pool_precision"] == 1
    assert aggressive["candidate_pool_recall"] == 0.5
    assert aggressive["gate_accuracy"] == 1
    assert aggressive["direct_evidence_recall"] == 1
    assert aggressive["support_evidence_recall"] == 0
    assert aggressive["pruned_direct_chunks"] == 0
    assert aggressive["pruned_support_chunks"] == 1
    assert aggressive["cases"][0]["pruned_support_chunk_ids"] == ["support"]


async def test_assistant_labels_are_explicitly_provisional_and_subset_scoped() -> None:
    review = make_review()
    review["review_kind"] = "assistant_provisional"
    review["cases"] = review["cases"][:1]

    report = await compare(make_snapshot(), [0.1], review)

    assert report["label_provenance"]["review_kind"] == "assistant_provisional"
    assert report["label_provenance"]["case_ids"] == ["answerable"]
    assert report["label_provenance"]["all_cases_reviewed"] is False
    assert report["results"][0]["reviewed_case_count"] == 1


async def test_unknown_answerability_is_not_counted_as_an_incorrect_gate() -> None:
    review = make_review()
    for case in review["cases"]:
        case["answerable"] = None
        case["answerability_basis"] = None

    result = (await compare(make_snapshot(), [0.1], review))["results"][0]

    assert result["candidate_pool_precision"] == 1
    assert result["answerability_reviewed_count"] == 0
    assert result["gate_accuracy"] is None


async def test_failed_admission_is_not_reported_as_pruning_loss() -> None:
    review = make_review()
    review["cases"][1]["judgments"][0]["grade"] = 2
    review["cases"][1]["answerable"] = True
    review["cases"][1]["answerability_basis"] = "Low-scoring direct evidence in the toy fixture."

    result = (await compare(make_snapshot(), [0.1], review))["results"][0]

    assert result["direct_evidence_recall"] == 0.5
    assert result["pruned_direct_chunks"] == 0
    assert result["gate_accuracy"] == 0.5


async def test_gate_pass_can_hide_loss_of_the_only_direct_answer() -> None:
    snapshot = make_snapshot()
    direct, support, unrelated = snapshot["cases"][0]["reranked_chunks"]
    direct["score"] = 0.31310463
    support["score"] = 0.13050689
    unrelated["score"] = 0.89312315
    snapshot["cases"][0]["reranked_chunks"] = [unrelated, direct, support]
    snapshot["cases"][0]["expected_chunk_ids"] = ["direct"]

    report = await compare(snapshot, [0.1, 0.5], make_review())
    conservative, aggressive = report["results"]

    assert conservative["gate_changes"] == aggressive["gate_changes"] == 0
    assert conservative["direct_evidence_recall"] == 1
    assert aggressive["direct_evidence_recall"] == 0
    assert aggressive["cases"][0]["pruned_direct_chunk_ids"] == ["direct"]
    assert aggressive["gate_accuracy"] == 1
    assert conservative["pinned_evidence_case_count"] == 1
    assert conservative["pinned_evidence_candidate_hits"] == 1
    assert conservative["pinned_evidence_context_hits"] == 1
    assert aggressive["pinned_evidence_candidate_hits"] == 1
    assert aggressive["pinned_evidence_context_hits"] == 0
    assert aggressive["cases"][0]["pinned_evidence"]["missing_context_ids"] == ["direct"]


async def test_missing_pinned_evidence_is_not_a_retrieval_hit() -> None:
    snapshot = make_snapshot()
    snapshot["cases"][0]["expected_chunk_ids"] = ["absent"]

    result = (await compare(snapshot, [0.1]))["results"][0]

    assert result["pinned_evidence_case_count"] == 1
    assert result["pinned_evidence_candidate_hits"] == 0
    assert result["pinned_evidence_context_hits"] == 0


async def test_held_out_comparison_rejects_threshold_tuning() -> None:
    snapshot = make_snapshot()
    snapshot["case_set_split"] = "held_out"

    with pytest.raises(ValueError, match="preselected threshold"):
        await compare(snapshot, [0, 0.05, 0.1])

    report = await compare(snapshot, [0, 0.1])
    assert report["case_set_split"] == "held_out"
    assert len(report["results"]) == 2


def test_reserved_cases_do_not_repeat_development_queries() -> None:
    root = Path(__file__).resolve().parents[2]
    development = load_cases(root / "evals/retrieval_cases.json")
    regressions = load_cases(root / "evals/retrieval_regression_cases.json")
    held_out_path = root / "evals/retrieval_holdout_cases.json"
    held_out = load_cases(held_out_path)

    assert json.loads(held_out_path.read_text())["split"] == "held_out"
    assert {case["id"] for case in held_out}.isdisjoint(case["id"] for case in development + regressions)
    assert {case["query"].casefold().strip() for case in held_out}.isdisjoint(
        case["query"].casefold().strip() for case in development + regressions
    )


@pytest.mark.parametrize("kind", [None, "", "unknown"])
def test_label_provenance_is_required(kind) -> None:
    review = make_review()
    review["review_kind"] = kind

    with pytest.raises(ValueError, match="review_kind"):
        reviewed_labels(make_snapshot(), review)


def test_known_answerability_requires_a_basis() -> None:
    review = make_review()
    review["cases"][0]["answerability_basis"] = None

    with pytest.raises(ValueError, match="answerability_basis"):
        reviewed_labels(make_snapshot(), review)


def test_review_template_hides_ranks_scores_and_initializes_no_labels() -> None:
    snapshot = make_snapshot()
    before = copy.deepcopy(snapshot)

    template = make_review_template(snapshot, "snapshot-digest")
    judgments = template["cases"][0]["judgments"]

    assert template["snapshot_sha256"] == "snapshot-digest"
    assert template["reviewer"] is None
    assert template["review_kind"] is None
    assert [judgment["chunk_id"] for judgment in judgments] == ["direct", "noise", "support"]
    assert all("score" not in judgment and "rank" not in judgment for judgment in judgments)
    assert all(judgment["grade"] is None for judgment in judgments)
    assert template["cases"][0]["answerable"] is None
    assert snapshot == before


async def test_no_admitted_context_has_no_reduction_percentage() -> None:
    snapshot = make_snapshot()
    snapshot["cases"] = snapshot["cases"][1:]

    result = (await compare(snapshot, [0.1]))["results"][0]

    assert result["context_chunks"] == 0
    assert result["context_reduction_percent"] is None


@pytest.mark.parametrize("grade", [None, True, -1, 3])
def test_incomplete_or_invalid_judgments_are_rejected(grade) -> None:
    review = make_review()
    review["cases"][0]["judgments"][0]["grade"] = grade

    with pytest.raises(ValueError, match="reviewed grade"):
        reviewed_labels(make_snapshot(), review)


def test_judgments_must_cover_the_frozen_pool() -> None:
    review = make_review()
    review["cases"][0]["judgments"].pop()

    with pytest.raises(ValueError, match="frozen candidate pool"):
        reviewed_labels(make_snapshot(), review)


def test_duplicate_case_ids_are_rejected(tmp_path) -> None:
    case = {"id": "same", "query": "Question", "role": "developer", "is_admin": False}
    path = tmp_path / "cases.json"
    path.write_text(json.dumps({"cases": [case, case]}))

    with pytest.raises(ValueError, match="Duplicate case"):
        load_cases(path)


def test_snapshot_write_does_not_overwrite_existing_file(tmp_path) -> None:
    path = tmp_path / "snapshot.json"
    write_json(path, {"original": True})

    with pytest.raises(FileExistsError):
        write_json(path, {"original": False})

    assert json.loads(path.read_text()) == {"original": True}


def make_policy_inputs(tmp_path, snapshot=None, review=None, held_out=None) -> tuple[Path, Path, Path]:
    snapshot = snapshot if snapshot is not None else make_snapshot()
    review = review if review is not None else make_review()
    held_out = held_out if held_out is not None else {
        "split": "held_out",
        "cases": [{"id": "fresh", "query": "An independent question?", "role": "developer", "is_admin": False}],
    }
    snapshot_path = tmp_path / "snapshot.json"
    review_path = tmp_path / "review.json"
    held_out_path = tmp_path / "held-out.json"
    write_json(snapshot_path, snapshot)
    write_json(review_path, {
        **review, "snapshot_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
    })
    write_json(held_out_path, held_out)
    return snapshot_path, review_path, held_out_path


def make_held_out_snapshot(policy: dict) -> dict:
    snapshot = make_snapshot()
    snapshot["case_set_split"] = "held_out"
    snapshot["cases_sha256"] = policy["held_out_cases_sha256"]
    snapshot["cases"] = [{
        **snapshot["cases"][0], "id": "fresh", "query": "An independent question?",
    }]
    return snapshot


async def test_policy_freezes_recall_preserving_candidate_for_evaluation_only(tmp_path) -> None:
    paths = make_policy_inputs(tmp_path)
    before = [path.read_bytes() for path in paths]

    policy = await freeze_policy(*paths, threshold=0.1)

    assert policy["evaluation_only"] is True
    assert policy["status"] == "frozen_for_offline_holdout"
    assert policy["context_prune_threshold"] == 0.1
    assert policy["label_provenance"]["all_cases_reviewed"] is True
    assert policy["development_results"][1]["candidate_pool_recall"] == 1
    assert [path.read_bytes() for path in paths] == before
    assert policy_thresholds(make_held_out_snapshot(policy), policy) == [0, 0.1]


@pytest.mark.parametrize("threshold, message", [(0.5, "useful evidence"), (0, "reduce context")])
async def test_policy_rejects_evidence_loss_or_no_reduction(tmp_path, threshold, message) -> None:
    paths = make_policy_inputs(tmp_path)

    with pytest.raises(ValueError, match=message):
        await freeze_policy(*paths, threshold=threshold)


async def test_policy_requires_complete_development_judgments(tmp_path) -> None:
    review = make_review()
    review["cases"] = review["cases"][:1]
    paths = make_policy_inputs(tmp_path, review=review)

    with pytest.raises(ValueError, match="complete candidate review"):
        await freeze_policy(*paths, threshold=0.1)


async def test_policy_rejects_wrong_review_snapshot(tmp_path) -> None:
    paths = make_policy_inputs(tmp_path)
    review = json.loads(paths[1].read_text())
    review["snapshot_sha256"] = "wrong-snapshot"
    paths[1].write_text(json.dumps(review))

    with pytest.raises(ValueError, match="different snapshot"):
        await freeze_policy(*paths, threshold=0.1)


async def test_policy_rejects_selection_using_held_out_results(tmp_path) -> None:
    snapshot = make_snapshot()
    snapshot["case_set_split"] = "held_out"
    paths = make_policy_inputs(tmp_path, snapshot=snapshot)

    with pytest.raises(ValueError, match="development data"):
        await freeze_policy(*paths, threshold=0.1)


async def test_policy_rejects_authorization_violations(tmp_path) -> None:
    snapshot = make_snapshot()
    snapshot["cases"][0]["authorization_violations"] = ["unauthorized-chunk"]
    paths = make_policy_inputs(tmp_path, snapshot=snapshot)

    with pytest.raises(ValueError, match="authorization violations"):
        await freeze_policy(*paths, threshold=0.1)


async def test_policy_rejects_development_query_overlap(tmp_path) -> None:
    paths = make_policy_inputs(tmp_path, held_out={
        "split": "held_out",
        "cases": [{"id": "different-id", "query": " HOW DO I COMPLETE THE TASK? ", "role": "developer", "is_admin": False}],
    })

    with pytest.raises(ValueError, match="repeat development questions"):
        await freeze_policy(*paths, threshold=0.1)


@pytest.mark.parametrize("field, value, message", [
    ("cases_sha256", "changed", "case set"),
    ("gate_threshold", 0.4, "Admission gate"),
    ("embedding_digest", "changed", "Corpus or retrieval"),
    ("corpus_manifest", [], "Corpus or retrieval"),
    ("reranker", {"model_id": "changed"}, "Corpus or retrieval"),
    ("search_limit", 5, "Corpus or retrieval"),
])
async def test_policy_rejects_evaluation_drift(tmp_path, field, value, message) -> None:
    policy = await freeze_policy(*make_policy_inputs(tmp_path), threshold=0.1)
    snapshot = make_held_out_snapshot(policy)
    snapshot[field] = value

    with pytest.raises(ValueError, match=message):
        policy_thresholds(snapshot, policy)


async def test_policy_rejects_changed_implementation(tmp_path) -> None:
    policy = await freeze_policy(*make_policy_inputs(tmp_path), threshold=0.1)
    policy["implementation_fingerprint"]["app/pipeline/nodes/node_05_relevance_gate.py"] = "changed"

    with pytest.raises(ValueError, match="implementation"):
        policy_thresholds(make_held_out_snapshot(policy), policy)


async def test_policy_cannot_be_misrepresented_as_deployed(tmp_path) -> None:
    policy = await freeze_policy(*make_policy_inputs(tmp_path), threshold=0.1)
    policy["evaluation_only"] = False

    with pytest.raises(ValueError, match="evaluation-only"):
        policy_thresholds(make_held_out_snapshot(policy), policy)


def test_held_out_cli_requires_frozen_policy(tmp_path, monkeypatch) -> None:
    snapshot = make_snapshot()
    snapshot["case_set_split"] = "held_out"
    snapshot_path = tmp_path / "held-out-snapshot.json"
    output = tmp_path / "comparison.json"
    write_json(snapshot_path, snapshot)
    monkeypatch.setattr("sys.argv", [
        "evaluate_retrieval", "compare", "--snapshot", str(snapshot_path),
        "--thresholds", "0.1", "--output", str(output),
    ])

    with pytest.raises(ValueError, match="require --policy"):
        main()
    assert not output.exists()


async def test_positive_only_labels_do_not_imply_perfect_specificity() -> None:
    review = make_review()
    review["cases"][1]["answerable"] = None

    result = (await compare(make_snapshot(), [0.1], review))["results"][0]

    assert result["gate_accuracy"] == 1
    assert result["gate_specificity"] is None
    assert result["answerability_unknown_count"] == 1
    assert result["gate_confusion"] == {
        "true_positive": 1, "false_positive": 0, "true_negative": 0, "false_negative": 0,
    }


async def test_gate_confusion_exposes_false_positives_and_false_negatives() -> None:
    snapshot = make_snapshot()
    for chunk in snapshot["cases"][0]["reranked_chunks"]:
        chunk["score"] = 0.2
    snapshot["cases"][1]["reranked_chunks"][0]["score"] = 0.9

    result = (await compare(snapshot, [0.1], make_review()))["results"][0]

    assert result["gate_accuracy"] == 0
    assert result["gate_specificity"] == 0
    assert result["gate_confusion"] == {
        "true_positive": 0, "false_positive": 1, "true_negative": 0, "false_negative": 1,
    }


async def test_low_scored_answer_is_gate_loss_not_pruning_loss() -> None:
    snapshot = make_snapshot()
    snapshot["cases"] = snapshot["cases"][:1]
    for chunk, score in zip(
        snapshot["cases"][0]["reranked_chunks"],
        (0.11920292, 0.011732464, 0.0015011822),
        strict=True,
    ):
        chunk["score"] = score
    review = make_review()
    review["cases"] = review["cases"][:1]

    report = await compare(snapshot, [0, 0.001], review)

    for result in report["results"]:
        assert result["gate_confusion"]["false_negative"] == 1
        assert result["gate_changes"] == 0
        assert result["context_chunks"] == 0
        assert result["candidate_pool_recall"] == 0
        assert result["direct_evidence_recall"] == 0
        assert result["support_evidence_recall"] == 0
        assert result["pruned_direct_chunks"] == 0
        assert result["pruned_support_chunks"] == 0
