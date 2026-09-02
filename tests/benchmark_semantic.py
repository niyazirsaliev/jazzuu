#!/usr/bin/env python3
"""CPU/local benchmark on a sanitized representative corpus."""
import json
import os
import resource
import sqlite3
import statistics
import struct
import tempfile
import time
import urllib.request
from pathlib import Path

from archive.semantic_reconcile import Utf8ByteCounter
from semantic_search.chunking import DeterministicChunker
from semantic_search.index import TenantIndex
from semantic_search.lifecycle import Lifecycle, SourceDocument
from semantic_search.provider import SemanticProvider
from semantic_search.runtime import embedder_from_env, index_identity

CORPUS_SIZE = 140
QUERIES = 30
SUPPORTED_STREAMING_CHUNKS = 2500
STREAMING_QUERIES = 10


def elapsed(fn):
    start = time.perf_counter()
    value = fn()
    return value, (time.perf_counter() - start) * 1000


def percentile(values, p):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * p)))]


def main():
    os.environ.setdefault("SEMANTIC_QUERY_TIMEOUT_S", "10")
    endpoint = os.environ.get("OLLAMA_EMBED_ENDPOINT", "http://host.docker.internal:11434").rstrip("/")
    baseline_peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    embedder = embedder_from_env()
    chunker = DeterministicChunker(Utf8ByteCounter(), max_tokens=2048, overlap_tokens=16)
    paragraph = (
        "Обсудили обслуживание автомобиля, семейные планы, бюджет проекта и задачи команды. "
        "Зафиксировали решение, ответственного и следующий проверяемый шаг. "
    )
    documents = [
        SourceDocument(
            f"recording-{index:04d}", f"Санитизированная запись {index}",
            f"Краткое резюме встречи номер {index}.", paragraph * 12,
        )
        for index in range(CORPUS_SIZE)
    ]
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "semantic.db"
        index = TenantIndex(path, tenant_id="benchmark", identity=index_identity())
        lifecycle = Lifecycle(index, chunker, embedder)
        _, build_ms = elapsed(lambda: [lifecycle.reconcile(document) for document in documents])
        indexing_peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        changed = SourceDocument(
            documents[0].recording_id, documents[0].title, documents[0].summary,
            documents[0].transcript + " Заменили тормозные колодки.",
        )
        _, update_ms = elapsed(lambda: lifecycle.reconcile(changed))
        provider = SemanticProvider(index, embedder, max_results=20)
        provider.search("ремонт автомобиля", limit=10)
        latencies = []
        for number in range(QUERIES):
            _, milliseconds = elapsed(lambda: provider.search(
                "ремонт автомобиля" if number % 2 else "планирование бюджета", limit=10
            ))
            latencies.append(milliseconds)
        query_peak_kib = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        index_bytes = path.stat().st_size

    with tempfile.TemporaryDirectory() as directory:
        streaming_path = Path(directory) / "semantic.db"
        streaming_index = TenantIndex(
            streaming_path, tenant_id="streaming-benchmark", identity=index_identity()
        )
        vector = struct.pack("<1024f", *([0.0] * 1023 + [1.0]))
        with sqlite3.connect(streaming_path) as conn:
            conn.executemany(
                "INSERT INTO records(recording_id,source_hash,generation,title) VALUES(?,?,1,?)",
                ((f"recording-{number:05d}", str(number), "Sanitized")
                 for number in range(SUPPORTED_STREAMING_CHUNKS)),
            )
            conn.executemany(
                "INSERT INTO chunks(recording_id,generation,ordinal,text,vector) VALUES(?,1,0,?,?)",
                ((f"recording-{number:05d}", f"sanitized chunk {number}", vector)
                 for number in range(SUPPORTED_STREAMING_CHUNKS)),
            )
            conn.commit()
        streaming_provider = SemanticProvider(
            streaming_index, embedder, max_results=20, min_score=-1.0
        )
        streaming_provider.search("ремонт автомобиля", limit=10)
        streaming_latencies = []
        for _ in range(STREAMING_QUERIES):
            _, milliseconds = elapsed(
                lambda: streaming_provider.search("ремонт автомобиля", limit=10)
            )
            streaming_latencies.append(milliseconds)
        streaming_index_bytes = streaming_path.stat().st_size

    # Release the model, then measure the next request including model load.
    body = json.dumps({
        "model": "bge-m3:latest", "input": ["санитизированный холодный запрос"],
        "truncate": False, "keep_alive": 0,
    }, ensure_ascii=False).encode()
    request = urllib.request.Request(
        f"{endpoint}/api/embed", data=body,
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        response.read()
    _, cold_ms = elapsed(lambda: embedder.embed_queries(["ремонт автомобиля"]))

    with urllib.request.urlopen(f"{endpoint}/api/tags", timeout=5) as response:
        tags = json.load(response)
    model_bytes = next(
        item["size"] for item in tags["models"]
        if item.get("digest") == index_identity().model_checksum
    )
    result = {
        "corpus_records": CORPUS_SIZE,
        "build_ms": round(build_ms, 2),
        "incremental_one_record_ms": round(update_ms, 2),
        "warm_query_p50_ms": round(statistics.median(latencies), 2),
        "warm_query_p95_ms": round(percentile(latencies, 0.95), 2),
        "cold_load_query_ms": round(cold_ms, 2),
        "index_bytes": index_bytes,
        "fleet_model_bytes": model_bytes,
        "baseline_peak_rss_kib": baseline_peak_kib,
        "indexing_peak_rss_kib": indexing_peak_kib,
        "query_peak_rss_kib": query_peak_kib,
        "indexing_peak_delta_kib": max(0, indexing_peak_kib - baseline_peak_kib),
        "query_peak_delta_kib": max(0, query_peak_kib - indexing_peak_kib),
        "query_samples": QUERIES,
        "supported_streaming_chunks": SUPPORTED_STREAMING_CHUNKS,
        "streaming_query_p50_ms": round(statistics.median(streaming_latencies), 2),
        "streaming_query_p95_ms": round(percentile(streaming_latencies, 0.95), 2),
        "streaming_index_bytes": streaming_index_bytes,
        "streaming_query_samples": STREAMING_QUERIES,
        "release_threshold_warm_p95_ms": 750,
        "pass": (percentile(latencies, 0.95) <= 750 and
                 percentile(streaming_latencies, 0.95) <= 750),
    }
    print(json.dumps(result, indent=2))
    return 0 if result["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
