# Known Limitations and Open Risks

## Product gaps
- **No password reset or email verification.** Needs an email provider.
- **No MFA.**
- **CSV only.** Excel, Parquet and direct database connections (Postgres/MySQL) are not supported yet.
- **Rate limits are in-process.** They reset on restart and aren't shared between workers.
  Use Redis for multi-worker deployments.
- **SQLite only.** Each dataset is a local file. Fine for single-node deployments; a shared
  warehouse would need a read-only DB role, statement timeouts and row-level security instead.

## Residual risks
1. **Prompt-injection heuristics are not complete.** The input guard catches common patterns, not
   every phrasing. That's acceptable because the real boundary is the AST guard plus the authorizer:
   a hijacked model still can't read hidden columns or write anything. The worst case is a wrong
   but authorized answer.
2. **Wrong answers.** The guard ensures queries are *safe*, not *correct*. The UI always shows the
   SQL that ran, its assumptions and the raw rows, so users can check. The free gpt-oss-120b model is good
   but can misread ambiguous questions.
3. **Inference over allowed data.** Users can always learn anything about columns they're allowed
   to see, including through predicates. Hide columns that shouldn't be queryable at all.
4. **Summaries can misstate numbers.** The gpt-oss-20b summary model sees only the shown rows and is told not
   to invent values, but the table is the source of truth.
5. **Groq free-tier limits and model churn.** Free-tier quotas are per model and per day, and each
   question uses 1–5 calls. When limits are hit the user gets a clear "try again" message. Groq also
   retires models. Llama 3.x went away on 2026-08-16, which is why the defaults are now
   `openai/gpt-oss-120b` and `openai/gpt-oss-20b`. Check console.groq.com/docs/deprecations and update
   `.env` when a model is deprecated; the app logs a clear error at start-up if one is missing.
6. **Type inference is heuristic.** Ambiguous dates where every value fits both day-first and
   month-first are read as day-first (`dd/mm/yyyy`). Zero-padded codes stay text.
7. **sqlglot version coupling.** Function names are matched on sqlglot's canonical names.
   Upgrades fail closed (queries get rejected) but need re-testing; sqlglot is pinned `<26`.
8. **LangGraph untested in the build sandbox.** The node and routing logic is fully tested
   through the built-in runner. `test_agent_graph.py` re-runs every scenario on LangGraph when it's
   installed, so run `pytest` locally to cover that path.
