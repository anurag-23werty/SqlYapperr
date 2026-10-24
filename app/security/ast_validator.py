"""SQLGlot-based AST security validator.

The validator parses the LLM's SQL candidate into an AST, applies an allowlist
policy to every node, and, only if every check passes, serializes the
*validated AST* back to SQL. That serialized SQL (never the original string) is
what the executor receives, wrapped in a :class:`ValidatedQuery`.

Regular expressions are not used for security decisions. The only string
checks are input hygiene (length, NUL bytes) that run before parsing.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

import sqlglot
from sqlglot import exp
from sqlglot.errors import SqlglotError
from sqlglot.optimizer.scope import Scope, traverse_scope

from app.security.limits import LimitPolicy, LimitViolation, enforce_limits
from app.security.policies import AccessPolicy
from app.security.validated import ValidatedQuery, _capability

log = logging.getLogger(__name__)

DIALECT = "sqlite"

# Identifier hygiene (structural, applied to parsed Identifier nodes, not raw SQL).
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")


def _types(*names: str) -> tuple[type[exp.Expression], ...]:
    """Resolve node classes by name, tolerating differences between sqlglot versions."""
    found = (getattr(exp, n, None) for n in names)
    return tuple(t for t in found if isinstance(t, type) and issubclass(t, exp.Expression))


SET_OPERATIONS = _types("Union", "Intersect", "Except")
ROOT_TYPES: tuple[type[exp.Expression], ...] = (exp.Select, *SET_OPERATIONS)
FORBIDDEN_NODES = _types(
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "AlterTable", "Command", "Pragma",
    "Attach", "Detach", "Transaction", "Commit", "Rollback", "Set", "Use", "Merge", "Into", "Copy",
    "LoadData", "Grant", "Revoke", "Analyze", "Describe", "Show", "TruncateTable", "Lateral",
    "Placeholder", "Parameter", "SessionParameter", "Lock", "Kill", "Cache", "Uncache", "Refresh",
    "Values", "Unnest", "Pivot",
)


class IssueCode:
    EMPTY = "EMPTY_QUERY"
    TOO_LONG = "QUERY_TOO_LONG"
    MALFORMED = "MALFORMED_SQL"
    MULTIPLE_STATEMENTS = "MULTIPLE_STATEMENTS"
    NOT_SELECT = "NOT_READ_ONLY_SELECT"
    FORBIDDEN = "FORBIDDEN_OPERATION"
    TABLE = "TABLE_NOT_ALLOWED"
    COLUMN = "COLUMN_NOT_ALLOWED"
    UNKNOWN_REFERENCE = "UNKNOWN_REFERENCE"
    AMBIGUOUS = "AMBIGUOUS_COLUMN"
    STAR = "STAR_NOT_ALLOWED"
    FUNCTION = "FUNCTION_NOT_ALLOWED"
    IDENTIFIER = "INVALID_IDENTIFIER"
    COMPLEXITY = "QUERY_TOO_COMPLEX"
    NESTING = "NESTING_TOO_DEEP"
    RECURSIVE = "RECURSIVE_CTE_NOT_ALLOWED"
    INTERNAL = "VALIDATION_FAILED"


@dataclass(frozen=True)
class ValidationIssue:
    code: str
    message: str  # safe to show to users and to the LLM: built from fixed templates


@dataclass(frozen=True)
class ValidatorConfig:
    max_sql_chars: int = 5_000
    max_nesting_depth: int = 3
    max_joins: int = 6
    max_ast_nodes: int = 600
    max_set_operations: int = 3
    max_string_literal_chars: int = 256


@dataclass(frozen=True)
class ValidationResult:
    ok: bool
    issues: tuple[ValidationIssue, ...] = ()
    query: ValidatedQuery | None = None

    @property
    def safe_sql(self) -> str | None:
        return self.query.sql if self.query else None


class _Reject(Exception):
    def __init__(self, *issues: ValidationIssue) -> None:
        super().__init__(issues[0].message if issues else "rejected")
        self.issues = issues


def _short(name: str, limit: int = 64) -> str:
    """Echo identifiers back only in truncated, printable form."""
    cleaned = "".join(ch for ch in name if ch.isprintable())
    return cleaned[:limit] + ("..." if len(cleaned) > limit else "")


def _func_name(node: exp.Func) -> str:
    if isinstance(node, exp.Anonymous):
        return str(node.name).upper()
    return type(node).sql_name().upper()


def _named_outputs(node: exp.Expression) -> set[str]:
    return {n.lower() for n in getattr(node, "named_selects", []) if n}


def _explicit_aliases(node: exp.Expression) -> set[str]:
    if not isinstance(node, exp.Select):
        return set()
    return {e.alias.lower() for e in node.expressions if isinstance(e, exp.Alias) and e.alias}


def _in_projection(column: exp.Expression, select: exp.Expression) -> bool:
    node = column
    while node.parent is not None and node.parent is not select:
        node = node.parent
    return node.parent is select and node.arg_key == "expressions"


class SQLValidator:
    """Stateless and thread-safe; construct once and share."""

    def __init__(
        self,
        policy: AccessPolicy,
        limits: LimitPolicy,
        config: ValidatorConfig | None = None,
        physical_columns: Mapping[str, frozenset[str]] | None = None,
    ) -> None:
        """``physical_columns`` lists *every* real column of each allowed table
        (including unauthorized ones). It is trusted metadata used only to stop an
        output alias from shadowing a real column; it is never shown to anyone."""
        self.policy = policy
        self.limits = limits
        self.config = config or ValidatorConfig()
        self.physical_columns = {
            t.lower(): frozenset(c.lower() for c in cols) for t, cols in (physical_columns or {}).items()
        }

    # ------------------------------------------------------------------ public
    def validate(self, sql: str) -> ValidationResult:
        try:
            tree = self._parse(sql)
            self._check_tree(tree)
            effective_limit, applied_default = self._apply_limits(tree)
            self._rewrite(tree)
            safe_sql = tree.sql(dialect=DIALECT, comments=False)
            # Re-parse and re-check the exact string we will execute, so validation
            # and execution can never disagree about what the query is.
            reparsed = self._parse(safe_sql)
            self._check_tree(reparsed)
            reparsed_limit, _ = self._apply_limits(reparsed)
            if (
                reparsed_limit != effective_limit
                or reparsed.args.get("limit") is None
                or any(True for _ in reparsed.find_all(exp.Star))
            ):
                raise _Reject(ValidationIssue(IssueCode.INTERNAL, "query could not be validated safely"))
            tables = frozenset(
                t.name.lower() for t in tree.find_all(exp.Table) if self.policy.is_table_allowed(t.name)
            )
            query = ValidatedQuery(
                sql=safe_sql,
                effective_limit=effective_limit,
                tables=tables,
                applied_default_limit=applied_default,
                _token=_capability(),
            )
            return ValidationResult(ok=True, query=query)
        except _Reject as rej:
            return ValidationResult(ok=False, issues=tuple(rej.issues))
        except RecursionError:
            return ValidationResult(ok=False, issues=(ValidationIssue(IssueCode.NESTING, "query is nested too deeply"),))
        except Exception:  # fail closed on anything unexpected
            log.exception("unexpected validator failure")
            return ValidationResult(
                ok=False, issues=(ValidationIssue(IssueCode.INTERNAL, "query could not be validated safely"),)
            )

    # ----------------------------------------------------------------- parsing
    def _parse(self, sql: str) -> exp.Expression:
        if not isinstance(sql, str) or not sql.strip():
            raise _Reject(ValidationIssue(IssueCode.EMPTY, "query is empty"))
        if len(sql) > self.config.max_sql_chars:
            raise _Reject(ValidationIssue(IssueCode.TOO_LONG, f"query exceeds {self.config.max_sql_chars} characters"))
        if "\x00" in sql:
            raise _Reject(ValidationIssue(IssueCode.MALFORMED, "query contains a NUL byte"))
        try:
            statements = [s for s in sqlglot.parse(sql, read=DIALECT) if s is not None]
        except SqlglotError:
            raise _Reject(ValidationIssue(IssueCode.MALFORMED, "query could not be parsed as SQLite SQL")) from None
        if not statements:
            raise _Reject(ValidationIssue(IssueCode.EMPTY, "query is empty"))
        if len(statements) != 1:
            raise _Reject(ValidationIssue(IssueCode.MULTIPLE_STATEMENTS, "exactly one SQL statement is allowed"))
        root = statements[0]
        if not isinstance(root, ROOT_TYPES):
            raise _Reject(ValidationIssue(IssueCode.NOT_SELECT, "only read-only SELECT queries are allowed"))
        return root

    # ------------------------------------------------------------------ checks
    def _check_tree(self, tree: exp.Expression) -> None:
        self._check_forbidden_nodes(tree)
        self._check_complexity(tree)
        self._check_identifiers(tree)
        cte_names = self._check_ctes(tree)
        self._check_tables(tree, cte_names)
        self._check_stars(tree)
        self._check_functions(tree)
        self._check_columns(tree)

    def _check_forbidden_nodes(self, tree: exp.Expression) -> None:
        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise _Reject(
                    ValidationIssue(IssueCode.FORBIDDEN, f"operation '{type(node).__name__.upper()}' is not allowed")
                )
            if isinstance(node, SET_OPERATIONS) and not self.policy.allow_set_operations:
                raise _Reject(ValidationIssue(IssueCode.FORBIDDEN, "set operations are not allowed"))
            if isinstance(node, exp.Join):
                if str(node.args.get("method") or "").upper() == "NATURAL":
                    raise _Reject(ValidationIssue(IssueCode.FORBIDDEN, "NATURAL JOIN is not allowed; use explicit ON"))
                if not node.args.get("on") and not node.args.get("using"):
                    # Comma joins and CROSS JOINs build cartesian products: slow and almost never intended.
                    raise _Reject(ValidationIssue(
                        IssueCode.COMPLEXITY, "every JOIN needs an ON condition (cartesian products are not allowed)"
                    ))

    def _check_complexity(self, tree: exp.Expression) -> None:
        cfg = self.config
        nodes = sum(1 for _ in tree.walk())
        if nodes > cfg.max_ast_nodes:
            raise _Reject(ValidationIssue(IssueCode.COMPLEXITY, f"query has too many elements (max {cfg.max_ast_nodes})"))
        joins = sum(1 for _ in tree.find_all(exp.Join))
        if joins > cfg.max_joins:
            raise _Reject(ValidationIssue(IssueCode.COMPLEXITY, f"query has too many joins (max {cfg.max_joins})"))
        set_ops = sum(1 for n in tree.walk() if isinstance(n, SET_OPERATIONS))
        if set_ops > cfg.max_set_operations:
            raise _Reject(
                ValidationIssue(IssueCode.COMPLEXITY, f"too many UNION/INTERSECT/EXCEPT (max {cfg.max_set_operations})")
            )
        for select in tree.find_all(exp.Select):
            depth = 0
            parent = select.parent
            while parent is not None:
                if isinstance(parent, exp.Select):
                    depth += 1
                parent = parent.parent
            if depth > cfg.max_nesting_depth:
                raise _Reject(
                    ValidationIssue(IssueCode.NESTING, f"sub-queries nested too deeply (max {cfg.max_nesting_depth})")
                )
        for lit in tree.find_all(exp.Literal):
            if lit.is_string and len(str(lit.this)) > cfg.max_string_literal_chars:
                raise _Reject(ValidationIssue(IssueCode.COMPLEXITY, "string literal is too long"))

    def _check_identifiers(self, tree: exp.Expression) -> None:
        for ident in tree.find_all(exp.Identifier):
            name = str(ident.this)
            if not _IDENTIFIER.fullmatch(name) or name.lower().startswith("sqlite_"):
                raise _Reject(ValidationIssue(IssueCode.IDENTIFIER, f"invalid identifier '{_short(name)}'"))

    def _check_ctes(self, tree: exp.Expression) -> set[str]:
        for with_ in tree.find_all(exp.With):
            if with_.args.get("recursive"):
                raise _Reject(ValidationIssue(IssueCode.RECURSIVE, "recursive CTEs are not allowed"))
        names: set[str] = set()
        for cte in tree.find_all(exp.CTE):
            name = cte.alias_or_name.lower()
            if not name or name in names:
                raise _Reject(ValidationIssue(IssueCode.IDENTIFIER, "CTE names must be unique and non-empty"))
            if name in self.policy.tables:
                raise _Reject(ValidationIssue(IssueCode.IDENTIFIER, f"CTE name '{_short(name)}' shadows a table"))
            names.add(name)
        return names

    def _check_tables(self, tree: exp.Expression, cte_names: set[str]) -> None:
        issues: list[ValidationIssue] = []
        for table in tree.find_all(exp.Table):
            if not isinstance(table.this, exp.Identifier):
                raise _Reject(ValidationIssue(IssueCode.FORBIDDEN, "table-valued functions are not allowed"))
            if table.args.get("db") or table.args.get("catalog"):
                raise _Reject(ValidationIssue(IssueCode.TABLE, "schema-qualified table names are not allowed"))
            name = table.name.lower()
            if name in cte_names:
                continue
            if not self.policy.is_table_allowed(name):
                issues.append(ValidationIssue(IssueCode.TABLE, f"table '{_short(name)}' is not in the authorized schema"))
        if issues:
            raise _Reject(*_dedupe(issues))

    def _check_stars(self, tree: exp.Expression) -> None:
        if self.policy.allow_star:
            return
        for star in tree.find_all(exp.Star):
            if isinstance(star.parent, exp.Count):
                continue  # COUNT(*) reads no column values
            raise _Reject(ValidationIssue(IssueCode.STAR, "SELECT * is not allowed; list the columns explicitly"))

    def _check_functions(self, tree: exp.Expression) -> None:
        for func in tree.find_all(exp.Func):
            name = _func_name(func)
            if name not in self.policy.ast_functions:
                raise _Reject(ValidationIssue(IssueCode.FUNCTION, f"function '{_short(name)}' is not allowed"))

    # ----------------------------------------------------------- column scoping
    def _check_columns(self, tree: exp.Expression) -> None:
        scopes = traverse_scope(tree)
        if not scopes:
            raise _Reject(ValidationIssue(IssueCode.INTERNAL, "query could not be validated safely"))
        covered = {id(s.expression) for s in scopes}
        for select in tree.find_all(exp.Select):
            if id(select) not in covered:  # a SELECT the scope analysis did not see: fail closed
                raise _Reject(ValidationIssue(IssueCode.INTERNAL, "query could not be validated safely"))

        issues: list[ValidationIssue] = []
        checked: set[int] = set()
        for scope in scopes:
            for column in scope.columns:
                checked.add(id(column))
                issue = self._check_column(column, scope)
                if issue:
                    issues.append(issue)

        # Columns the scope analysis did not attribute (e.g. ORDER BY <alias>) must
        # be unqualified references to an output alias of the enclosing query.
        for column in tree.find_all(exp.Column):
            if id(column) in checked or isinstance(column.this, exp.Star):
                continue
            owner = column.find_ancestor(*ROOT_TYPES)
            if not column.table and owner is not None and column.name.lower() in _named_outputs(owner):
                continue
            issues.append(
                ValidationIssue(IssueCode.UNKNOWN_REFERENCE, f"column '{_short(column.sql(dialect=DIALECT))}' cannot be resolved")
            )
        if issues:
            raise _Reject(*_dedupe(issues))

    @staticmethod
    def _sources(scope: Scope) -> dict[str, exp.Expression | Scope]:
        out: dict[str, exp.Expression | Scope] = {}
        for name, value in scope.selected_sources.items():
            source = value[1] if isinstance(value, tuple) else value
            out[name.lower()] = source
        return out

    def _lookup(self, scope: Scope | None, qualifier: str) -> exp.Expression | Scope | None:
        while scope is not None:
            src = self._sources(scope).get(qualifier)
            if src is not None:
                return src
            scope = scope.parent  # correlated reference to an enclosing query
        return None

    def _source_has(self, source: exp.Expression | Scope, column: str) -> bool:
        if isinstance(source, Scope):
            return column in _named_outputs(source.expression)
        if isinstance(source, exp.Table):
            return self.policy.is_column_allowed(source.name, column)
        return False

    def _check_column(self, column: exp.Column, scope: Scope) -> ValidationIssue | None:
        if column.args.get("db") or column.args.get("catalog"):
            return ValidationIssue(IssueCode.COLUMN, "schema-qualified column names are not allowed")
        name = column.name.lower()
        display = _short(column.sql(dialect=DIALECT))
        qualifier = column.table.lower()

        if qualifier:
            source = self._lookup(scope, qualifier)
            if source is None:
                return ValidationIssue(IssueCode.UNKNOWN_REFERENCE, f"unknown table or alias in '{display}'")
            if not self._source_has(source, name):
                return ValidationIssue(IssueCode.COLUMN, f"column '{display}' is not in the authorized schema")
            return None

        candidates = [alias for alias, src in self._sources(scope).items() if self._source_has(src, name)]
        if len(candidates) == 1:
            return None
        if len(candidates) > 1:
            return ValidationIssue(IssueCode.AMBIGUOUS, f"column '{display}' is ambiguous; qualify it with a table alias")
        if self._is_safe_alias_reference(column, name, scope):
            return None  # reference to an explicit output alias (GROUP BY / HAVING / ORDER BY)
        return ValidationIssue(IssueCode.COLUMN, f"column '{display}' is not in the authorized schema")

    def _is_safe_alias_reference(self, column: exp.Column, name: str, scope: Scope) -> bool:
        select = scope.expression
        if name not in _explicit_aliases(select) or _in_projection(column, select):
            return False
        # SQLite resolves an identifier to a real column before an output alias, so an
        # alias that shares a name with ANY physical column of a source table (even an
        # unauthorized one, e.g. "AS email") would silently read that column instead.
        for src in self._sources(scope).values():
            if isinstance(src, exp.Table):
                physical = self.physical_columns.get(src.name.lower())
                if physical is None or name in physical:
                    return False
            elif isinstance(src, Scope) and name in _named_outputs(src.expression):
                return False
        return True

    # --------------------------------------------------------------- rewrites
    @staticmethod
    def _rewrite(tree: exp.Expression) -> None:
        """Normalize the validated tree. No ``*`` ever reaches the database:
        ``COUNT(*)`` becomes the equivalent ``COUNT(1)``."""
        for star in list(tree.find_all(exp.Star)):
            if isinstance(star.parent, exp.Count):
                star.replace(exp.Literal.number(1))

    # ------------------------------------------------------------------ limits
    def _apply_limits(self, tree: exp.Expression) -> tuple[int, bool]:
        try:
            return enforce_limits(tree, self.limits)
        except LimitViolation as violation:
            raise _Reject(ValidationIssue(violation.code, violation.message)) from None


def _dedupe(issues: Iterable[ValidationIssue]) -> list[ValidationIssue]:
    seen: set[tuple[str, str]] = set()
    out: list[ValidationIssue] = []
    for issue in issues:
        key = (issue.code, issue.message)
        if key not in seen:
            seen.add(key)
            out.append(issue)
    return out[:10]
