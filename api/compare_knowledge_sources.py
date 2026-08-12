"""Read-only comparison of local and Supabase Knowledge V1 retrieval."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv


PROJECT_DIR = Path(__file__).resolve().parents[1]
BOT_DIR = PROJECT_DIR / "bot"
sys.path.insert(0, str(BOT_DIR))
load_dotenv(PROJECT_DIR / ".env")

import app  # noqa: E402
from knowledge_rag import load_knowledge_chunks, retrieve_local_knowledge  # noqa: E402


def _ids(items):
    return sorted(str(item.get("id") or item.get("url") or "") for item in items)


def _snapshot(retrieval):
    return {
        "governing_topic": retrieval.governing_topic,
        "retrieved_topics": list(retrieval.retrieved_topics),
        "chunks": [
            {
                "source_id": row.get("source_id"),
                "section": row.get("section"),
                "score": round(float(row.get("similarity") or 0), 6),
            }
            for row in retrieval.rows
        ],
        "required_disclosures": _ids(retrieval.obligations.required_disclosures),
        "required_links": _ids(retrieval.obligations.required_links),
        "dynamic_requirements": sorted(
            requirement.fact for requirement in retrieval.dynamic_requirements
        ),
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--cases",
        default=str(PROJECT_DIR / "evals" / "knowledge_v1_cases.jsonl"),
    )
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=6)
    args = parser.parse_args()

    cases = [
        json.loads(line)
        for line in Path(args.cases).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    cases = [case for case in cases if case.get("scope") == "knowledge"]
    chunks = load_knowledge_chunks(PROJECT_DIR / "knowledge")
    app.KNOWLEDGE_RAG_ENABLED = True
    app.KNOWLEDGE_RAG_SOURCE = "supabase"

    results = []
    for case in cases:
        query = "\n".join(case["messages"])
        local = _snapshot(retrieve_local_knowledge(query, chunks, limit=args.limit))
        supabase = _snapshot(app.search_knowledge_bundle(query, limit=args.limit))
        governing_match = local["governing_topic"] == supabase["governing_topic"]
        disclosures_match = local["required_disclosures"] == supabase["required_disclosures"]
        links_match = local["required_links"] == supabase["required_links"]
        dynamic_match = local["dynamic_requirements"] == supabase["dynamic_requirements"]
        results.append({
            "id": case["id"],
            "query": query,
            "local": local,
            "supabase": supabase,
            "comparison": {
                "governing_topic_match": governing_match,
                "required_disclosures_match": disclosures_match,
                "required_links_match": links_match,
                "dynamic_requirements_match": dynamic_match,
                "acceptance_match": all((
                    governing_match, disclosures_match, links_match, dynamic_match,
                )),
            },
        })

    summary = {
        "cases": len(results),
        "acceptance_matches": sum(
            result["comparison"]["acceptance_match"] for result in results
        ),
        "governing_topic_matches": sum(
            result["comparison"]["governing_topic_match"] for result in results
        ),
        "required_disclosures_matches": sum(
            result["comparison"]["required_disclosures_match"] for result in results
        ),
        "required_links_matches": sum(
            result["comparison"]["required_links_match"] for result in results
        ),
        "dynamic_requirements_matches": sum(
            result["comparison"]["dynamic_requirements_match"] for result in results
        ),
    }
    report = {"summary": summary, "results": results}
    Path(args.output).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
