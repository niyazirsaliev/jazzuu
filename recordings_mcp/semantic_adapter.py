from __future__ import annotations

from semantic_search.hybrid import HybridRanker
from semantic_search.runtime import readonly_provider_from_env

from . import store


MAX_HYBRID_RESULTS = 20


def _visible_id_map(config, allowed_numbers):
    allowed_where, allowed_params = store._allowlist_sql(allowed_numbers)
    with store.open_db(config) as conn:
        store._require_numbering(conn)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(recordings)")}
        visible = ""
        if "archived_local_at" in columns:
            visible = " AND (archived_local_at IS NULL OR archived_local_at='')"
        rows = conn.execute(
            "SELECT id,recording_number,name FROM recordings WHERE recording_number LIKE ?" +
            allowed_where + visible + " ORDER BY recording_number",
            (store._series_clause(config), *allowed_params),
        ).fetchall()
    return {row["id"]: {"number": row["recording_number"], "name": row["name"] or "Без названия"} for row in rows}


def search_recordings_hybrid(config, query=None, limit=None, allowed_numbers=None, provider=None):
    if not isinstance(query, str) or len(" ".join(query.split())) < 3:
        raise store.InvalidArgument("query must contain at least 3 characters")
    limit = min(store.clamp_limit(limit), MAX_HYBRID_RESULTS)
    mapping = _visible_id_map(config, allowed_numbers)
    lexical = store.search_recordings(config, query=query, limit=limit, allowed_numbers=allowed_numbers)
    lexical_rows = [{
        "id": next((rid for rid, meta in mapping.items() if meta["number"] == item["number"]), ""),
        "name": item["name"], "snippet": item["snippet"], "match": "exact",
    } for item in lexical["items"]]
    try:
        semantic_provider = provider or readonly_provider_from_env()
        semantic_rows = semantic_provider.search(query, limit=limit, allowed_ids=set(mapping))
        ranked = HybridRanker().rank(lexical_rows, semantic_rows, limit=limit)
    except Exception:
        ranked = lexical_rows
    items = []
    for row in ranked:
        meta = mapping.get(row.get("id"))
        if not meta:
            continue
        item = {"number": meta["number"], "name": meta["name"], "snippet": row.get("snippet")}
        if row.get("source") == "semantic":
            item["label"] = "По смыслу"
        items.append(item)
    return {"items": items, "limit": limit, "next_cursor": None}
