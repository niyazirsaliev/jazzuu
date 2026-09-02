#!/usr/bin/env python3
"""Sanitized retrieval acceptance against the pinned local model."""
import json
import os
import sys
from pathlib import Path

from semantic_search.embedder import PrefixingNormalizedEmbedder
from semantic_search.ollama import OllamaBackend

DIGEST = "7907646426070047a77226ac3e684fbbe8410524f7b4a74d02837e43f2146bab"


def main():
    fixture = json.loads(Path(__file__).with_name("fixtures").joinpath("semantic_retrieval.json").read_text())
    backend = OllamaBackend(
        endpoint=os.environ.get("OLLAMA_EMBED_ENDPOINT", "http://host.docker.internal:11434"),
        model="bge-m3:latest",
        digest=DIGEST, dimension=1024, timeout_seconds=10,
    )
    embedder = PrefixingNormalizedEmbedder(backend, query_prefix="", passage_prefix="")
    documents = fixture["documents"]
    passages = embedder.embed_passages([row["text"] for row in documents])
    reciprocal = []
    recalled = 0
    results = []
    for case in fixture["queries"]:
        query = embedder.embed_queries([case["query"]])[0]
        ranked = sorted(
            ((sum(a * b for a, b in zip(query, vector)), row["id"])
             for row, vector in zip(documents, passages)), reverse=True,
        )
        ids = [item[1] for item in ranked]
        rank = ids.index(case["expected"]) + 1
        recalled += int(rank <= 3)
        reciprocal.append(1.0 / rank)
        results.append({"kind": case["kind"], "expected": case["expected"], "rank": rank})
    metrics = {
        "recall_at_3": recalled / len(results),
        "mrr": sum(reciprocal) / len(reciprocal),
    }
    output = {"metrics": metrics, "thresholds": fixture["thresholds"], "cases": results}
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if all(metrics[key] >= value for key, value in fixture["thresholds"].items()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
