"""The only type the executor will run.

A :class:`ValidatedQuery` can only be constructed by the AST validator (it
requires a module-private capability token). This makes "execute something
that was not validated" a type error rather than a code-review hope.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_CAPABILITY = object()


def _capability() -> object:
    """Return the capability token. Only ``ast_validator`` may call this."""
    return _CAPABILITY


@dataclass(frozen=True)
class ValidatedQuery:
    sql: str                       # serialized from the validated AST, never the raw input
    effective_limit: int           # the row limit the AST validator enforced
    tables: frozenset[str]         # base tables referenced (for audit logging)
    applied_default_limit: bool
    _token: object = field(repr=False, compare=False, default=None)

    def __post_init__(self) -> None:
        if self._token is not _CAPABILITY:
            raise PermissionError("ValidatedQuery can only be created by the SQL validator")
