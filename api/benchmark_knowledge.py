"""Deterministic Knowledge V1 benchmark. No model, network or database writes."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_DIR / "bot"))
from knowledge_rag import load_knowledge_chunks, retrieve_local_knowledge  # noqa: E402


def _ids(items):
    return {str(item.get("id")) for item in items}


def evaluate_case(case, chunks):
    query = "\n".join(case["messages"])
    if case.get("scope") != "knowledge":
        return {
            "id": case["id"],
            "classification": "UNSUPPORTED",
            "finding": "Caso multi-message/orquestación: se documenta, no se corrige en Fase B.",
            "retrieved_topics": [],
        }

    retrieval = retrieve_local_knowledge(query, chunks, limit=6)
    topics = list(retrieval.obligations.topics)
    expected_topics = set(case.get("expected_topics") or [])
    topic_ok = bool(expected_topics & set(topics))
    disclosure_ids = _ids(retrieval.obligations.required_disclosures)
    link_ids = _ids(retrieval.obligations.required_links)
    missing_disclosures = sorted(set(case.get("expected_disclosures") or []) - disclosure_ids)
    missing_links = sorted(set(case.get("expected_links") or []) - link_ids)
    combined = " ".join(str(row.get("content") or "") for row in retrieval.rows).lower()
    lowered_query = query.lower()
    escalation_supported = (
        retrieval.obligations.escalation_required
        or "escalar" in combined
        or "derivar" in combined
    )

    incorrect = not topic_ok or bool(missing_disclosures) or bool(missing_links)
    if incorrect:
        classification = "INCORRECT"
    elif case.get("expected_outcome") == "escalated":
        classification = "ESCALATED_CORRECTLY" if escalation_supported else "INCORRECT"
    else:
        classification = "RESOLVED_CORRECTLY"
    return {
        "id": case["id"],
        "classification": classification,
        "retrieved_topics": topics,
        "retrieved_sources": [row.get("source_id") for row in retrieval.rows],
        "required_live_checks": case.get("required_live_checks") or [],
        "required_disclosures_ok": not missing_disclosures,
        "required_links_ok": not missing_links,
        "missing_disclosures": missing_disclosures,
        "missing_links": missing_links,
        "routing_incorrect": not topic_ok,
        "dynamic_information_invented": False,
        "unnecessary_data_repetition": "NOT_EVALUATED_WITHOUT_MODEL",
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--knowledge-dir", required=True)
    parser.add_argument("--cases", default=str(PROJECT_DIR / "evals" / "knowledge_v1_cases.jsonl"))
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    chunks = load_knowledge_chunks(args.knowledge_dir)
    cases = [json.loads(line) for line in Path(args.cases).read_text(encoding="utf-8").splitlines() if line.strip()]
    results = [evaluate_case(case, chunks) for case in cases]
    counts = Counter(result["classification"] for result in results)
    report = {
        "knowledge_dir": str(Path(args.knowledge_dir)),
        "case_count": len(cases),
        "chunk_count": len(chunks),
        "summary": dict(sorted(counts.items())),
        "limitations": [
            "Live checks are requirements recorded by the benchmark; this local runner does not call Tiendanube.",
            "Multi-message concurrency and sales orchestration are reported as UNSUPPORTED, outside Knowledge V1 scope.",
            "Repetition and final prose quality require a model/harness evaluation in Fase C.",
        ],
        "results": results,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"summary": report["summary"], "chunks": len(chunks)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
