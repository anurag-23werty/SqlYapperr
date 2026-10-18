"""Strict LIMIT / OFFSET handling.

SQLite treats a negative LIMIT (e.g. ``LIMIT -1``) as "no limit". We therefore
never trust the presence of a LIMIT clause: every LIMIT and OFFSET anywhere in
the tree must be a bare, non-negative, base-10 integer literal within bounds.
Expressions (``1+1``), unary minus, strings, floats, hex, placeholders and
sub-queries are all rejected. The executor additionally caps fetched rows, so
even a bug here cannot return more than ``max_rows`` rows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp

# 18 digits always fits in a signed 64-bit integer; anything longer is an overflow attempt.
_MAX_DIGITS = 18


@dataclass(frozen=True)
class LimitPolicy:
    max_rows: int
    default_limit: int
    max_offset: int


class LimitViolation(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def parse_non_negative_int(node: exp.Expression | None) -> int | None:
    """Return the integer value of ``node`` if it is a plain non-negative integer literal."""
    if not isinstance(node, exp.Literal) or node.is_string:
        return None
    text = node.this
    if not isinstance(text, str) or not text.isascii() or not text.isdigit():
        return None
    if len(text) > _MAX_DIGITS:
        return None
    return int(text)


def _limit_value_node(limit: exp.Expression) -> exp.Expression | None:
    # sqlglot stores the row count in ``expression``; some versions use ``this``.
    return limit.args.get("expression") if limit.args.get("expression") is not None else limit.args.get("this")


def _check_offset(node: exp.Expression | None, policy: LimitPolicy) -> None:
    if node is None:
        return
    value_node = node.args.get("expression") if isinstance(node, exp.Offset) else node
    value = parse_non_negative_int(value_node)
    if value is None:
        raise LimitViolation("INVALID_OFFSET", "OFFSET must be a non-negative integer literal")
    if value > policy.max_offset:
        raise LimitViolation("INVALID_OFFSET", f"OFFSET must be <= {policy.max_offset}")


def check_limit_node(limit: exp.Limit, policy: LimitPolicy) -> int:
    value = parse_non_negative_int(_limit_value_node(limit))
    if value is None:
        raise LimitViolation(
            "INVALID_LIMIT", "LIMIT must be a non-negative integer literal (no expressions, signs or strings)"
        )
    if value > policy.max_rows:
        raise LimitViolation("LIMIT_TOO_LARGE", f"LIMIT must be <= {policy.max_rows}")
    # ``LIMIT a, b`` (SQLite offset form) is stored as an offset on the Limit node.
    _check_offset(limit.args.get("offset"), policy)
    return value


def enforce_limits(root: exp.Expression, policy: LimitPolicy) -> tuple[int, bool]:
    """Validate every LIMIT/OFFSET in the tree and ensure the root has a safe limit.

    Returns ``(effective_limit, applied_default)``. Mutates ``root`` in place
    when a default limit has to be added.
    """
    if any(True for _ in root.find_all(exp.Fetch)):
        raise LimitViolation("INVALID_LIMIT", "FETCH clauses are not supported; use LIMIT")

    for limit in list(root.find_all(exp.Limit)):
        check_limit_node(limit, policy)
    for offset in list(root.find_all(exp.Offset)):
        _check_offset(offset, policy)
        holder = offset.parent
        # The root gets a default LIMIT below; nested OFFSET without LIMIT is rejected.
        if holder is not None and holder is not root and holder.args.get("limit") is None:
            raise LimitViolation("INVALID_OFFSET", "OFFSET requires an explicit LIMIT")

    root_limit = root.args.get("limit")
    if root_limit is None:
        root.set("limit", exp.Limit(expression=exp.Literal.number(policy.default_limit)))
        return policy.default_limit, True
    return check_limit_node(root_limit, policy), False
