# Saved evaluation runs

Runs that are worth keeping, copied from `eval/runs/` (which is git-ignored). The content
is unchanged. The one exception is line endings: the runner wrote CRLF on Windows, and
Git stores LF (`.gitattributes`). Since then the runner writes LF on every OS.
Each folder contains:
- `report.md`, `report.json` and `results.jsonl`, as the runner wrote them;
- `rescored.json`, metrics computed later from `results.jsonl` with
  `python -m screener.evaluate --rescore <folder>`, without calling a model.

All data is synthetic: invented companies on `.example` domains. The results describe this
small set only and are not evidence of accuracy on real websites. Interpretation, code
versions and the change log are in [docs/evaluation.md](../../docs/evaluation.md).

| Folder | Backend | Code | Outcome |
|---|---|---|---|
| `20261125T170009Z-claude-code` | Claude Code (subscription login), `claude-sonnet-5-5`, effort `low` | `266d6ca` (from commit times; see docs) | Stopped after 14 of 20 cases: a budget bug in the evaluation runner. All 14 processed cases got the expected label |
| `20261125T170638Z-claude-code` | same | `0239322` (includes the runner fix `413998b`) | All 20 cases processed; all 20 final labels equal the expected labels. Started by the assistant right after the fix, without asking the project owner first |

**Billing wording in these files is superseded.** The generated reports say "no per-token
charge" and "not an actual charge". The correct statement is:

> Authorization via Claude subscription; the API-equivalent is given for reference only.
> Actual billing is not confirmed by this report.

`apiKeySource: none` in the logs helps diagnose which credentials were used. It does not
prove that no paid extra usage occurred. The files' content is left as written, so the
original output stays verifiable.

**Scored against the labels of that time.** On 2026-11-26 the expected labels of cases 03,
12 and 13 changed from `non_fit` to `insufficient_information`. These files still hold
the old labels in `expected`, and their scores use them. Under the current labels, run 2's
final labels match in 17 of 20 cases and run 1's in 11 of 20. See the change log in
[docs/evaluation.md](../../docs/evaluation.md#expected-answers).
