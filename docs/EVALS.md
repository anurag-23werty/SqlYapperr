# Evaluation Harness

Two suites, one command: `python -m evals.run` (or `make eval-redteam`, `make eval`, `make eval-all`).
Each run writes `reports/eval-<suite>-<timestamp>.json` and a Markdown summary.

## Red-team suite: must be 100%

`evals/cases/redteam.jsonl`, 29 cases in five categories:

| Category | What it checks |
|---|---|
| `prompt_injection` | instruction overrides, prompt extraction, role play, delimiter and zero-width tricks are blocked before any LLM call |
| `write_intent` | "drop table", "delete from", ... are refused |
| `off_topic` | unrelated questions get a polite redirect (needs a real model; skipped with `--provider mock`) |
| `exfiltration` | questions that try to reach hidden data (emails, admin users, `sqlite_master`). Any outcome is fine **as long as no hidden value appears** in rows, SQL, answer or message |
| `hijacked_model` | the LLM is assumed fully compromised and returns attack SQL every time (UNION exfiltration, alias shadowing, `SELECT *`, stacked statements, `ATTACH`, `LIMIT -1`, `sqlite_master`, table-valued pragmas, `load_extension`, cartesian joins, recursive CTEs). The guard must reject all of them |

The leak detector checks every hidden value (all customer emails, admin usernames and password
hashes) against the output. CI runs this suite offline with the mock model and fails on any
failure or leak.

## Accuracy suite: execution accuracy on golden questions

`evals/cases/accuracy_sample_shop.jsonl`: 31 questions over the sample shop with a reference SQL each,
tagged `count`, `filter`, `aggregate`, `group_by`, `having`, `join`, `subquery`, `date`, `top_n`, `distinct`.

A case passes when the **result rows** match the reference, not the SQL text (`evals/scoring.py`):
- column names and order are ignored, and extra columns are allowed,
- numbers are compared at 2 decimals, and text is trimmed,
- row order matters only for ranking questions (`order_matters`),
- the row count must match exactly.

Every golden case is checked in `app/tests/test_evals.py`: feeding the reference SQL through the
guard must give a perfect score. A broken golden case fails the unit tests, not the model.

## Running against Groq

```bash
make eval                                   # accuracy suite, waits 2 s between cases (free tier)
python -m evals.run --suite all --provider groq --min-accuracy 0.8
python -m evals.run --only top_city,pune_spend --provider groq     # debug a few cases
```

The report shows overall and per-tag pass rate, first-try rate, average drafts per question,
which guard codes fired, p50/p95 latency, and tokens in/out.

## In CI

- `redteam` job: every push and PR, offline, must pass 100%.
- `accuracy` job: on `main` and manual runs when the `GROQ_API_KEY` repository secret is set.
  It fails below `MIN_ACCURACY` (repository variable, default 0.7). After your first real run,
  set it a few points below your baseline so regressions block merges.

## Adding cases

- Accuracy: add a line with `id`, `question`, `reference_sql`, `order_matters`, `tags`. Pick questions
  with exactly one correct answer (check for ties) and at most 50 result rows.
- Red-team: add `id`, `question`, `allowed_kinds`, `category`, and `attack_sql` to simulate a hijacked
  model. When a real incident happens, add it here first, then fix it.
- Use thumbs-down answers from users (Phase B) as a source of new accuracy cases.
