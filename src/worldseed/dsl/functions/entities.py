"""``entities_of(type=..., where=<expr>)`` — list entity ids of a type with
optional filter.

Used by autoresearch's action ``enum_from`` expressions to surface only
the papers a given agent can legally act on, e.g.:

    enum_from: "entities_of(type='paper', where=status in ['draft','under_review'] and author != $agent)"

The expression after ``where=`` is a compact Python-like condition that
references the entity's properties and a handful of context vars
(``$agent`` = current agent id). Supported operators:

- ``==``, ``!=``
- ``in [a, b, c]`` / ``in ('x','y')``
- ``and`` / ``or`` / ``not``

Nothing exotic — this is intentionally small. If you need more, promote
the filter into a purpose-built function.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from worldseed.dsl.functions._registry import register_function

if TYPE_CHECKING:
    from worldseed.engine.state_store import StateStore


def _eval_where(expr: str, entity_data: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """Tiny predicate evaluator. No eval, no exec. Handles just enough."""
    if not expr:
        return True

    # Substitute $agent -> current agent id (quoted)
    agent_id = ctx.get("agent_id", "")
    expr = re.sub(r"\$agent\b", repr(agent_id), expr)

    # Split on 'and' / 'or' respecting brackets? Keep it simple — left-to-right
    # with precedence and > or. Quick approach: split on ' or ' then each on ' and '.
    def eval_atom(atom: str) -> bool:
        atom = atom.strip()
        # NOT handling
        if atom.startswith("not "):
            return not eval_atom(atom[4:])
        # Member-in-list: "status in ['a','b']"
        m = re.match(r"(\w+(?:\.\w+)*)\s+in\s+\[(.+)\]$", atom)
        if m:
            key, items = m.group(1), m.group(2)
            lhs = _resolve(key, entity_data)
            values = [v.strip().strip("'\"") for v in items.split(",")]
            return str(lhs) in values
        m = re.match(r"(\w+(?:\.\w+)*)\s+in\s+\((.+)\)$", atom)
        if m:
            key, items = m.group(1), m.group(2)
            lhs = _resolve(key, entity_data)
            values = [v.strip().strip("'\"") for v in items.split(",")]
            return str(lhs) in values
        # Equality: "author == 'x'" or "author != 'x'" or "author == $var"
        m = re.match(r"(\w+(?:\.\w+)*)\s*(==|!=)\s*(.+)$", atom)
        if m:
            key, op, raw = m.group(1), m.group(2), m.group(3).strip().strip("'\"")
            lhs = _resolve(key, entity_data)
            if op == "==":
                return str(lhs) == raw
            return str(lhs) != raw
        return False  # unrecognized

    def eval_or(expr: str) -> bool:
        for part in re.split(r"\s+or\s+", expr):
            if eval_and(part):
                return True
        return False

    def eval_and(expr: str) -> bool:
        for part in re.split(r"\s+and\s+", expr):
            if not eval_atom(part):
                return False
        return True

    return eval_or(expr)


def _resolve(key: str, data: dict[str, Any]) -> Any:
    """Walk ``dot.path`` in the entity's data (shallow — one level is enough)."""
    parts = key.split(".")
    cur: Any = data
    for p in parts:
        if isinstance(cur, dict):
            cur = cur.get(p)
        else:
            return None
    return cur


def _parse_args(args_str: str) -> tuple[str, str]:
    """Extract ``type`` and ``where`` from ``type='X', where=<expr>``.

    The ``where`` expression may contain commas inside ``[...]`` lists, so we
    can't reuse the generic comma-splitter — instead take everything after
    ``where=`` verbatim.
    """
    type_match = re.search(r"type\s*=\s*['\"]([^'\"]+)['\"]", args_str)
    entity_type = type_match.group(1) if type_match else ""
    where_match = re.search(r"where\s*=\s*(.+)$", args_str, re.DOTALL)
    where = where_match.group(1).strip() if where_match else ""
    return entity_type, where


def _call_entities_of(
    args_str: str,
    store: StateStore,
    ctx: dict[str, Any],
) -> list[str]:
    """Return entity ids matching type + optional where filter."""
    entity_type, where = _parse_args(args_str)
    if not entity_type:
        return []
    results: list[str] = []
    for ent in store.query_by_type(entity_type):
        if _eval_where(where, ent.data, ctx):
            results.append(ent.id)
    return results


register_function("entities_of", _call_entities_of)
