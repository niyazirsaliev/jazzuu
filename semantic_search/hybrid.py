from __future__ import annotations


class HybridRanker:
    """Fixed priority: lexical exact, lexical prefix/token, semantic."""

    def rank(self, lexical: list[dict], semantic: list[dict], limit: int = 20) -> list[dict]:
        exact = [row for row in lexical if row.get("match") == "exact"]
        prefix = [row for row in lexical if row.get("match") != "exact"]
        output = []
        seen = set()
        for row in [*exact, *prefix]:
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            output.append({**row, "source": "lexical"})
        for row in semantic:
            if row.get("id") in seen:
                continue
            seen.add(row.get("id"))
            output.append({**row, "source": "semantic", "match": "semantic", "label": "По смыслу"})
        return output[: max(1, int(limit))]
