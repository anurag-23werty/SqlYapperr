"""Prompt-injection defences around the LLM.

These checks are a *signal* layer that blocks obvious attacks early and keeps
untrusted text clearly separated from instructions ("spotlighting"). They are
NOT the security boundary for data access: even a fully hijacked model can only
produce a SQL candidate, and that candidate must still pass the AST guard and
the SQLite authorizer.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Literal

# Explicit attempts to override the model's instructions or extract them.
_INJECTION = [
    r"\b(ignore|disregard|forget|override|bypass)\b.{0,40}\b(previous|prior|above|earlier|all|any|your|the|system)\b"
    r".{0,40}\b(instructions?|rules?|prompts?|guidelines?|polic(y|ies)|constraints?)\b",
    r"\b(system|developer|hidden|initial)\s+(prompt|message|instructions?)\b",
    r"\b(reveal|print|show|repeat|leak|output|display|tell\s+me)\b.{0,30}\byour\s+(system\s+)?(prompt|instructions?|rules|guidelines)\b",
    r"\b(instructions?|rules|prompt)\s+you\s+(were|have\s+been)\s+(given|told)\b",
    r"\byou\s+are\s+(now|no\s+longer)\b",
    r"\b(act|behave|pretend)\s+as\s+(if|an?|the)\b.{0,30}\b(admin|root|dba|developer|unrestricted|jailbroken)\b",
    r"\b(jailbreak|dan\s+mode|developer\s+mode|god\s+mode)\b",
    r"</?\s*(system|assistant|user|instructions?|user_question|data|schema|im_start|im_end)\s*>",
    r"\[\s*(system|inst)\s*\]|<<\s*sys\s*>>",
    r"\b(new|updated|real)\s+instructions?\s*:",
]
# Requests to change data or schema. The system is read-only by construction.
_WRITE_INTENT = [
    r"\b(drop|truncate)\s+(the\s+)?(table|database|schema|column|all)\b",
    r"\bdelete\s+(from|all|every|the\s+(rows?|records?|data|table))\b",
    r"\binsert\s+into\b",
    r"\bupdate\s+\w+\s+set\b",
    r"\balter\s+table\b",
    r"\b(attach|detach)\s+database\b",
    r"\bpragma\s+\w+",
    r"\b(grant|revoke)\s+\w+",
    r"\bcreate\s+(table|index|view|trigger)\b",
]
_INJECTION_RE = [re.compile(p, re.IGNORECASE | re.DOTALL) for p in _INJECTION]
_WRITE_RE = [re.compile(p, re.IGNORECASE) for p in _WRITE_INTENT]
_DELIMITERS = re.compile(r"</?\s*(user_question|data|schema|history|system|assistant)\s*>", re.IGNORECASE)


@dataclass(frozen=True)
class GuardVerdict:
    action: Literal["allow", "block"]
    category: Literal["ok", "prompt_injection", "write_intent", "empty"]
    text: str
    message: str = ""


def normalize_text(text: str, max_chars: int) -> str:
    """NFKC-normalize and strip control, format (zero-width, bidi) and private-use characters."""
    text = unicodedata.normalize("NFKC", text)
    kept = []
    for ch in text:
        cat = unicodedata.category(ch)
        if ch in "\n\t" or cat[0] != "C":
            kept.append(ch)
    return " ".join("".join(kept).split())[:max_chars].strip()


def inspect_question(question: str, max_chars: int) -> GuardVerdict:
    text = normalize_text(question, max_chars)
    if not text:
        return GuardVerdict("block", "empty", text, "Please type a question about your data.")
    for pattern in _INJECTION_RE:
        if pattern.search(text):
            return GuardVerdict(
                "block", "prompt_injection", text,
                "That looks like an attempt to change how the assistant works. Ask a question about your data instead.",
            )
    for pattern in _WRITE_RE:
        if pattern.search(text):
            return GuardVerdict(
                "block", "write_intent", text,
                "This assistant is read-only: it can answer questions about your data but never change it.",
            )
    return GuardVerdict("allow", "ok", _DELIMITERS.sub(" ", text))


def fence(value: Any, max_chars: int = 60) -> str:
    """Render an untrusted data value for a prompt: printable, delimiter-free, truncated."""
    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return repr(value)
    text = normalize_text(str(value), max_chars)
    return _DELIMITERS.sub(" ", text).replace("<", "‹").replace(">", "›")
