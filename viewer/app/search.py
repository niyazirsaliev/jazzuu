import re
import sqlite3
from dataclasses import dataclass
from typing import Callable, Protocol


DEFAULT_LIMIT = 50
MAX_QUERY_TERMS = 8
MAX_TERM_LENGTH = 64
MAX_YO_POSITIONS = 5


@dataclass(frozen=True)
class LexicalQueryPlan:
    exact_match: str
    prefix_match: str


class SearchProvider(Protocol):
    """Replaceable tenant-scoped retrieval provider."""

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]: ...


def _term_variants(term: str) -> list[str]:
    folded = term.casefold()
    if sum(char in {"е", "ё"} for char in folded) > MAX_YO_POSITIONS:
        return sorted({folded, folded.replace("ё", "е"), folded.replace("е", "ё")})
    variants = {""}
    for char in folded:
        choices = ("е", "ё") if char in {"е", "ё"} else (char,)
        variants = {prefix + choice for prefix in variants for choice in choices}
    return sorted(variants)


def _match_expression(terms: list[str], *, prefix: bool) -> str:
    suffix = "*" if prefix else ""
    groups = []
    for term in terms:
        alternatives = [f'"{variant}"{suffix}' for variant in _term_variants(term)]
        groups.append(
            alternatives[0]
            if len(alternatives) == 1
            else "(" + " OR ".join(alternatives) + ")"
        )
    return " AND ".join(groups)


def build_lexical_plan(query: str) -> LexicalQueryPlan | None:
    terms = [
        term[:MAX_TERM_LENGTH]
        for term in re.findall(r"[\w']+", (query or "").strip(), flags=re.UNICODE)[
            :MAX_QUERY_TERMS
        ]
    ]
    if not terms:
        return None
    return LexicalQueryPlan(
        exact_match=_match_expression(terms, prefix=False),
        prefix_match=_match_expression(terms, prefix=True),
    )


class TenantFtsSearchProvider:
    """Indexed lexical retrieval over exactly one tenant connection factory."""

    def __init__(self, connection_factory: Callable[[], sqlite3.Connection]):
        self._connection_factory = connection_factory

    def search(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return self._search(query, limit, ranked=False)

    def search_ranked(self, query: str, limit: int = DEFAULT_LIMIT) -> list[dict]:
        return self._search(query, limit, ranked=True)

    def _search(self, query: str, limit: int, *, ranked: bool) -> list[dict]:
        plan = build_lexical_plan(query)
        if plan is None:
            return []
        limit = max(1, min(int(limit), DEFAULT_LIMIT))
        conn = self._connection_factory()
        try:
            columns = {
                row[1] for row in conn.execute("PRAGMA table_info(recordings)")
            }
            visibility = []
            if "archived_local_at" in columns:
                visibility.append(
                    "(r.archived_local_at IS NULL OR r.archived_local_at='')"
                )
            # Keep this identical to the default feed contract in main.py.
            # archived_at is the ingest timestamp for ordinary archive rows,
            # not a local visibility state. Canonical deletion removes the row,
            # so the inner join also excludes any stale FTS entry.
            visible = "".join(f" AND {clause}" for clause in visibility)
            sql = (
                "SELECT r.id AS id, r.name AS name, "
                "snippet(recordings_fts,-1,'【','】','…',12) AS snip "
                "FROM recordings_fts f JOIN recordings r ON r.id=f.id "
                "WHERE recordings_fts MATCH ?" + visible + " "
                "ORDER BY rank, r.id LIMIT ?"
            )
            exact_rows = conn.execute(sql, (plan.exact_match, limit)).fetchall()
            prefix_rows = conn.execute(sql, (plan.prefix_match, limit)).fetchall()
        except sqlite3.OperationalError:
            return []
        finally:
            conn.close()

        rows = list(exact_rows)
        exact_ids = {row["id"] for row in exact_rows}
        rows.extend(row for row in prefix_rows if row["id"] not in exact_ids)
        return [
            {
                "id": row["id"],
                "name": row["name"] or "Без названия",
                "snippet": row["snip"],
                **({"match": "exact" if row["id"] in exact_ids else "prefix"} if ranked else {}),
            }
            for row in rows[:limit]
        ]
