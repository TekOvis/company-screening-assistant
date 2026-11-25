# Evaluation

## Purpose and scope

The evaluation measures how the whole pipeline behaves on a **small synthetic set**:
- the agent with its tool;
- structured output;
- evidence checks;
- the label rules.

It is meant to catch regressions and to compare settings, such as effort levels, models
or prompt changes. It cannot show accuracy on real company websites, and no such claim
is made.

## Data

- 20 invented companies in [`eval/cases`](../eval/cases), each on a reserved `.example`
  domain. The format is described in [eval/README.md](../eval/README.md).
- Label mix: 6 `fit`, 9 `non_fit`, 5 `insufficient_information`.
- Three criteria profiles: industrial valves in Germany, B2B software for manufacturers
  in DACH, and packaged bakery products in Poland.
- Cases cover:
  - plain fits;
  - a location that is only on the About page;
  - markets mistaken for locations;
  - indirect signals (phone code, currency, time zone);
  - a trade-fair trap;
  - exclusions (distributor, private equity, retail-only);
  - visible and hidden prompt injection;
  - a vague homepage;
  - a German-language page;
  - a second office inside the target region;
  - missing subpages;
  - an off-site redirect.
- Each case fixes the expected label and the facts that can be extracted: name, own
  locations (with accepted spellings), trap places that must not be reported as
  locations, and markets.

**How the expected answers were made.** The AI assistant wrote them during development,
applying the label definitions to the page texts. They were committed (`Add synthetic
evaluation set with expected answers`) before any evaluation runner or live run existed;
the git history shows the order. A test checks that every expected fact is literally
stated in the page texts. **Human review of the labels is still pending.** Any later
change to expected answers must be listed in the change log below, with a reason.

## Running

```bash
uv run python -m screener.evaluate --mode mock
uv run python -m screener.evaluate --mode claude-code --only 01-valves-fit-homepage --yes
uv run python -m screener.evaluate --mode api --only 01-valves-fit-homepage --yes
```

- **mock**: the full application with the keyword-rule stand-in. Free and used in CI. It
  shows that the pipeline works; it does not measure model quality.
- **claude-code**: the real model through the local Claude Code login. Usage counts
  against the Claude subscription's limits; there is no per-token charge.
- **api**: the real model through the Anthropic API, billed per token. Before starting,
  the runner prints the hard caps and their worst-case price at the listed tariffs.

Both real-model modes use the saved pages; `.example` domains are never requested. Both
print their limits and do not start without `--yes`. The plan is to run one case first,
check the result and the usage, and only then run the whole set.

Reports state the backend and how it is billed. For Claude Code they show
"no per-token charge" and, separately, an API-equivalent figure: what the reported tokens
would cost at API tariffs. That figure is an estimate for comparison, not a charge.

Each run writes `report.md`, `report.json` and `results.jsonl` to `eval/runs/<time>-<mode>/`
(git-ignored). Reports record the model, effort, limits, a fingerprint of the dataset and
a fingerprint of the prompts, schemas and tool definition.

## Metrics

| Metric | Definition |
|---|---|
| Schema-valid rate | final answers that passed Pydantic validation ÷ (those + `invalid_output` answers) |
| Accuracy | correct final labels ÷ all cases. A case without a label (an error) counts as wrong |
| Macro-F1 | unweighted mean of per-label F1. An error lowers recall of the true label but is no false positive |
| Confusion matrix | rows expected, columns predicted, plus a `no_label` column for errors |
| Model's own label | the same metrics for `model_fit`, before evidence checks, to show what the checks change |
| Facts | name correct; own locations exactly right (all expected, nothing extra, no trap place); invented or trap locations before and after checks; markets found |
| Evidence | share of quotes verbatim on the cited page; share of those the second check supported |
| Time | wall time, median and maximum per case |
| Tokens and cost | tokens reported by the API for successful responses; cost at the tariffs listed in `pricing.py` (source and date included) |

Formulas are implemented by hand in `screener/metrics.py` and tested against
hand-computed examples.

## Results

### Mock run, pipeline check only (2026-11-25)

This run uses keyword rules, not a model. It confirms that all 20 cases pass through the
whole pipeline offline: fetching saved pages, the tool loop, schema validation, both
verification stages, the label rules and the reports.

| Metric | Value |
|---|---|
| Final label accuracy | 0.65 (13/20) |
| Macro-F1 | 0.65 |
| Schema-valid final answers | 20 of 20 |
| Locations invented or taken from traps | none |
| Tokens and cost | estimates only, nothing billed |

The misses are what keyword rules cannot do: understand "distributor" or "bakery vs
valves", read German or Polish text, or decide to fetch an About page to look for
ownership. These numbers say nothing about a real model.

### Claude Code, one case (2026-11-25)

A first check of the `claude_code` backend, not an evaluation result. Case
`01-valves-fit-homepage`, model `claude-sonnet-5-5`, effort `low`.

| | |
|---|---|
| Final label | fit, as expected; the model's own label was also fit |
| Time | 13.1 s |
| Model requests | 3 (2 agent turns + 1 evidence check), 0 failed |
| Tokens | 4 input, 984 output, 6,066 cache write (Claude Code caches the prompt itself) |
| Billing | Claude subscription, no per-token charge. API-equivalent estimate: $0.025, not a charge |
| Session | credential source reported as `none` (no API key); tools available: `StructuredOutput` and `mcp__screener__fetch_company_page` only; no session saved to disk |

The evidence checks removed two facts:
- The description: the model quoted the right sentence, but labelled the quote as
  supporting the name and business, not the description.
- A "market" ("chemical plants and refineries"): this is a customer segment, not a place.

The final label did not depend on either. Whether a description should also accept quotes
labelled for the business is an open design question. It has not been changed after
seeing this run.

### Full set through Claude Code (2026-11-25)

Two runs of the whole set, with the same dataset (sha256 `1cdffc833ab6`), the same prompts
(sha256 `1e4d53b4453f`), model `claude-sonnet-5-5` and effort `low`. Between the runs only
the evaluation runner's budget changed. Prompts, cases and label rules did not.

**Run 1: stopped by the runner's budget after 14 cases.** The runner shared one per-run
cap of 60 requests across all 20 cases. That cap is sized for one web screening of up to
10 companies. After 60 requests the remaining cases were refused before any model call.
Case 15 was refused before its evidence check, although its unverified label was
correct. All 14 completed cases got the expected label. Counted over all 20 cases,
accuracy was 0.70 (14/20) with macro-F1 0.79, because a missing label counts as wrong. This was a
bug in the runner, not in the model or the app. It was fixed in `Eval: give each case its
own share of the budget`: an evaluation now gets the per-company caps once per case.

**Run 2: the full set.**

| Metric | Value |
|---|---|
| Final label accuracy | 1.00 (20/20) |
| Macro-F1 | 1.00 |
| Model's own label, before checks | 1.00 (20/20) |
| Schema-valid final answers | 20 of 20 |
| Company name correct / own locations exactly right | 20/20 / 20/20 |
| Markets found | 7/7 |
| Locations invented or taken from traps (before and after checks) | none |
| Evidence items | 106. All 106 quotes are verbatim on the cited page; the second check supported 100, called 4 unclear and found 2 not supported |
| Tool calls | 12 in 8 cases |
| Time per case | median 12.1 s, max 18.5 s; whole run 268 s |
| Model requests | 81, of which 0 failed; 3–5 per case: agent turns plus one evidence check |
| Tokens | 116 input, 34,524 output, 39,393 cache write, 166,636 cache read |
| Billing | Claude subscription, no per-token charge. API-equivalent estimate: $0.46 for the set, about $0.023 per case. This is not a charge |
| Session | credential source reported as `none` for every Claude Code run |

Confusion matrix of final labels: all counts on the diagonal (fit 6, non_fit 9,
insufficient_information 5); no errors and no `no_label`.

**What the evidence checks removed.** In 5 cases (03, 04, 06, 17, 18) the description was
removed. The model writes a description that combines several facts, such as product,
location and customers. It then cites one quote that covers only part of them, and the
second check correctly answers "unclear" or "not supported" for the whole sentence. In
case 18 the industry was removed too, and the industry criterion became `unknown`. The
label stayed `non_fit` because of the retail-only business. No label changed because of
the checks.

**How to read this.** A perfect score on 20 short, clean pages written by the same
assistant that wrote the prompts shows something limited:
- the pipeline works end to end with a real model;
- the traps in this set (markets vs locations, indirect signals, trade fairs, prompt
  injection, off-site redirect) did not mislead it.

It is not an accuracy estimate for real websites. The set is too small and too easy to
separate good settings from better ones. Harder cases, written or reviewed by someone
else, are the next step.

## Threats to validity

- Twenty short pages are far simpler and cleaner than real websites.
- The same assistant wrote the prompts and the cases, so results are likely optimistic.
  Do not tune prompts on these cases without recording it.
- The labels are the assistant's reading of the definitions until a human reviews them.
- The model is not deterministic. The 14 cases that completed in both runs got the same
  labels both times, but tool use and request counts differed. Two runs are too few to
  measure the variance.
- With a perfect score, the set no longer shows differences between settings.

## Change log of expected answers

No changes since the set was committed.
