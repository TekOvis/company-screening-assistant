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
- Label mix: 6 `fit`, 6 `non_fit`, 8 `insufficient_information`. Until 2026-11-26 it was
  6 / 9 / 5; see the [change log](#expected-answers).
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
stated in the page texts. Any later change to expected answers must be listed in the
change log below, with a reason.

**Label review (2026-11-26).** The assistant prepared a review table: for each case the
criteria, the relevant page text, the expected label and facts, and the reasoning, without
model results. The author went through it together with remarks from a second AI assistant
(Codex), agreed with the conclusions and decided the geography rule in
[eval/README.md](../eval/README.md#label-definitions). Three labels changed as a result.
This was not an independent manual check of all source material by the author or by
anyone else.

## Running

```bash
uv run python -m screener.evaluate --mode mock
uv run python -m screener.evaluate --mode claude-code --only 01-valves-fit-homepage --yes
uv run python -m screener.evaluate --mode api --only 01-valves-fit-homepage --yes
```

- **mock**: the full application with the keyword-rule stand-in. Free and used in CI. It
  shows that the pipeline works; it does not measure model quality.
- **claude-code**: the real model through the local Claude Code login (authorization via
  the Claude subscription). The runner cannot check whether paid extra usage is enabled
  on the account, so check that before a full run.
- **api**: the real model through the Anthropic API, billed per token. Before starting,
  the runner prints the hard caps and their worst-case price at the listed tariffs.

Both real-model modes use the saved pages; `.example` domains are never requested. Both
print their limits and do not start without `--yes`. The plan is to run one case first,
check the result and the usage, and only then run the whole set.

Reports state the backend and how it is billed. For Claude Code they say: "Authorization
via Claude subscription; the API-equivalent is given for reference only. Actual billing is
not confirmed by this report." The API-equivalent is what the reported tokens would cost
at API tariffs. The session's `apiKeySource` (logged as `none` so far) helps diagnose
which credentials Claude Code used. It does not prove that no paid extra usage occurred.

Each run writes `report.md`, `report.json` and `results.jsonl` to `eval/runs/<time>-<mode>/`
(git-ignored). Reports record:
- the model, effort and limits;
- the code version: the commit and a fingerprint of the application source, which also
  catches uncommitted edits;
- a fingerprint of the dataset;
- a fingerprint of the prompts, schemas and tool definition.

Runs worth keeping are copied unchanged to [`eval/results/`](../eval/results), which is
versioned. `--rescore RUN_DIR` recomputes the fact metrics from a saved `results.jsonl`
without calling a model.

## Metrics

| Metric | Definition |
|---|---|
| Schema-valid rate | final answers that passed Pydantic validation ÷ (those + `invalid_output` answers) |
| Accuracy | correct final labels ÷ all cases. A case without a label (an error) counts as wrong |
| Macro-F1 | unweighted mean of per-label F1. An error lowers recall of the true label but is no false positive |
| Confusion matrix | rows expected, columns predicted, plus a `no_label` column for errors |
| Model's own label | the same metrics for `model_fit`, before evidence checks, to show what the checks change |
| Facts | name correct; own locations exactly right (all expected, nothing extra, no trap place); invented or trap locations before and after checks; markets found |
| Unconfirmed facts | facts are the name, description, industry, each own location and each market. **Before checks:** facts in the model's answer that the checks did not confirm, so they were removed. **After checks:** facts in the final answer without a supported evidence item of a matching category, which should be 0; the metric checks it instead of assuming it. Only cases whose checks ran count |
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

**With the labels changed on 2026-11-26:** 0.50 (10/20), macro-F1 0.47. The three changed
cases (03, 12, 13) are now misses: like the prompt, the mock treats a stated own location
outside the target region as "not met" and answers `non_fit`.

### Claude Code, one case (2026-11-25)

A first check of the `claude_code` backend, not an evaluation result. Case
`01-valves-fit-homepage`, model `claude-sonnet-5-5`, effort `low`.

| | |
|---|---|
| Final label | fit, as expected; the model's own label was also fit |
| Time | 13.1 s |
| Model requests | 3 (2 agent turns + 1 evidence check), 0 failed |
| Tokens | 4 input, 984 output, 6,066 cache write (Claude Code caches the prompt itself) |
| Billing | Authorization via Claude subscription; the API-equivalent ($0.025) is given for reference only. Actual billing is not confirmed by this report |
| Session | `apiKeySource` reported as `none`; tools available: `StructuredOutput` and `mcp__screener__fetch_company_page` only; no session saved to disk |

The evidence checks removed two facts:
- The description: the model quoted the right sentence, but labelled the quote as
  supporting the name and business, not the description.
- A "market" ("chemical plants and refineries"): this is a customer segment, not a place.

The final label did not depend on either. **Case 01 was used during development.** This
check led to the description rule change (see the change log), so results on case 01 are
not an independent quality check.

### Full set through Claude Code (2026-11-25)

Two runs of the whole set. Both used:
- the same dataset (sha256 `1cdffc833ab6`);
- the same prompts (sha256 `1e4d53b4453f`);
- model `claude-sonnet-5-5`, effort `low`.

Between the runs only the evaluation runner's budget changed; prompts, cases and label
rules did not. Both runs are kept unchanged in [`eval/results/`](../eval/results).

**Scored against the expected answers of that time.** On 2026-11-26 the labels of cases
03, 12 and 13 changed from `non_fit` to `insufficient_information` (see the
[change log](#expected-answers)). In both runs the model answered `non_fit` for those
three, with geography `not_met`, as the prompt's rule says. Under the current labels,
final labels would match in 17 of 20 cases in run 2 and 11 of 20 in run 1. That figure
comes from the saved answers, without a new run. The tables below keep the original
scoring.

| | Run 1 | Run 2 |
|---|---|---|
| Folder | `20261125T170009Z-claude-code` | `20261125T170638Z-claude-code` |
| Started (UTC) | 2026-11-25 17:00:09 | 2026-11-25 17:06:38 |
| Code | `266d6ca`, see note | `0239322`, which includes the runner fix `413998b` |
| Cases processed (checks completed) | 14 of 20 | 20 of 20 |
| Final label = expected label | 14 of 20; all 14 processed cases correct | 20 of 20 |
| Why it ended | stopped by the runner's budget (a bug, below) | all cases done |

**Code note for run 1.** The reports of these runs do not record the commit; newer
reports do. The versions above come from the commit times. When run 1 started, HEAD was
`266d6ca`. The demo-protection code committed one minute later (`27e4120`: `config.py`,
`demo.py`, `web.py`, templates) was very likely already in the working tree. None of it is
on the evaluation path, except demo settings added to `config.py`. The evaluation code
and prompts were the same in both runs; the matching prompt fingerprints confirm this for
the prompts.

**Why run 1 stopped.** The runner shared one per-run cap of 60 requests across all 20
cases. That cap is sized for one web screening of up to 10 companies.
- After 60 requests, cases 16–20 were refused before any model call.
- Case 15 was refused before its evidence check, although the model's unverified label was
  correct.

This was a bug in the evaluation runner, not in the model or the app. It was fixed in
`413998b`: an evaluation now gets the per-company caps once per case.

**Rerun.** The assistant started run 2 right after the fix, without asking the project
owner first. From now on a rerun needs the owner's approval.

**What "20/20" means in run 2:** all 20 cases were processed with completed checks, *and*
all 20 final labels equal the expected labels.

| Metric | Run 1 (6 cases not processed) | Run 2 |
|---|---|---|
| Final label: accuracy / macro-F1 (missing label = wrong) | 0.70 (14/20) / 0.79 | 1.00 (20/20) / 1.00 |
| Model's own label before checks: accuracy / macro-F1 | 0.75 (15/20) / 0.83 | 1.00 (20/20) / 1.00 |
| Errors (result statuses) | `budget_exceeded` 6, `ok` 14 | `ok` 20 |
| Schema-valid final answers | 15 of 15 | 20 of 20 |
| Company name correct / own locations exactly right | 14/14 / 14/14 | 20/20 / 20/20 |
| Markets found | 7/7 | 7/7 |
| Locations invented or taken from traps (before and after checks) | none | none |
| Unconfirmed facts **before** checks (removed) | 5 of 62 facts, in 14 checked cases: description 4, market 1 | 6 of 84 facts: description 5, industry 1 |
| Unconfirmed facts **after** checks | 0 of 57 | 0 of 78 |
| Evidence items | 73; all verbatim; 96% of them supported by the second check | 106; all verbatim; 100 supported, 4 unclear, 2 not supported |
| Tool calls | 9 in 5 cases | 12 in 8 cases |
| Time per case | median 11.2 s, max 27.3 s; whole run 227 s | median 12.1 s, max 18.5 s; whole run 268 s |
| Model requests | 60, 0 failed | 81, 0 failed; 3–5 per case |
| Tokens (input / output / cache write / cache read) | 84 / 25,515 / 47,539 / 104,145 | 116 / 34,524 / 39,393 / 166,636 |
| Billing | Authorization via Claude subscription; the API-equivalent ($0.38) is given for reference only. Actual billing is not confirmed by this report | Same; API-equivalent $0.46 |
| Session | `apiKeySource` reported as `none` in every Claude Code run: useful for diagnostics, but no proof that no paid extra usage occurred | same |

The unconfirmed-fact rows were computed later from the saved answers with `--rescore`,
without calling a model. Each run folder holds them in `rescored.json`.

Confusion matrices of final labels (rows: expected, columns: predicted):

| Run 1 | fit | non_fit | insufficient | no_label |
|---|---|---|---|---|
| fit | 3 | 0 | 0 | 3 |
| non_fit | 0 | 8 | 0 | 1 |
| insufficient_information | 0 | 0 | 3 | 2 |

| Run 2 | fit | non_fit | insufficient | no_label |
|---|---|---|---|---|
| fit | 6 | 0 | 0 | 0 |
| non_fit | 0 | 9 | 0 | 0 |
| insufficient_information | 0 | 0 | 5 | 0 |

**Failed cases.**
- Run 1: the six cases without a label (15–20) are budget stops by the runner, not wrong
  answers.
- Run 2: no case failed.

**What the checks removed** (11 facts in both runs together, none of which changed a
label):
- **Partly stated (9).** Descriptions in cases 03, 06, 12 (run 1) and 03, 04, 06, 17, 18
  (run 2), and the industry in case 18 (run 2). The model combined several facts in one
  sentence, such as product, location and customers, and cited a quote that states only
  part of them.
  - In at least case 03 the missing part (exports across Europe) is on the page, just not
    in the cited quote. The cost of the strict check is a lost description, not a wrong
    fact.
  - These are still rejected after the description rule change.
- **No quote filed for the description (1):** case 01 in run 1, the situation the rule
  change addresses.
- **Not a place (1):** the "market" "chemical plants and refineries" in case 01, run 1.
  It is a customer segment and had no quote.

In run 2's case 18, the industry criterion became `unknown` after the industry was
removed. The label stayed `non_fit` because of the retail-only business.

**How to read this.** These are pipeline checks on 20 short, clean pages written by the
same assistant that wrote the prompts. Even 100% here does not mean the same accuracy on
real company websites. The runs show only two things:
- the pipeline works end to end with a real model;
- the traps in this set did not mislead it: markets vs locations, indirect signals, trade
  fairs, prompt injection, an off-site redirect.

The set is too small and too easy to tell good settings from better ones. Harder cases,
written or reviewed by someone else, are the next step.

## Threats to validity

- Twenty short pages are far simpler and cleaner than real websites.
- The same assistant wrote the prompts and the cases, so results are likely optimistic.
  Do not tune prompts on these cases without recording it.
- The labels are the assistant's reading of the definitions. The author agreed with a
  review of them prepared by the assistant, with a second AI assistant's remarks.
  Nobody has checked the source material independently.
- Three labels were changed after both full model runs had been seen. The review table
  left model results out, but the set is not blind.
- The prompt and the mock still apply the earlier geography rule: stated own locations
  outside the target region mean "not met". On cases 03, 12 and 13 the system is
  therefore expected to disagree with the labels until the prompt is aligned.
- The model is not deterministic. The 14 cases that completed in both runs got the same
  labels both times, but tool use and request counts differed. Two runs are too few to
  measure the variance.
- With a perfect score, the set no longer shows differences between settings.
- The set is no longer blind. Case 01 was used during development, all 20 cases have been
  seen in two full runs, and the description rule was changed after looking at outputs
  (see the change log).

## Change log

### Expected answers

The set was committed in `07cd40e`.

| Date | Cases | Change | Reason |
|---|---|---|---|
| 2026-11-26 | 03, 12, 13 | `non_fit` → `insufficient_information`; rationales rewritten; tag `geography-mismatch` → `own-site-elsewhere` | **Geography rule agreed by the author:** any confirmed own site in the target region is enough. So own sites elsewhere do not show that geography is not met, unless the text confirms that there is no own site in the target region. The full texts of these cases do not confirm that: 03 names only a headquarters in Spain, 12 only a team based in Latvia, 13 only a headquarters in France. No negative statements were added to the pages to keep the old labels. Case 16 (headquarters abroad, own office in the target region) stays `fit` under the same rule. The author decided this after the label review described [above](#data), after both full model runs had been seen |

New label mix: 6 `fit`, 6 `non_fit`, 8 `insufficient_information` (before: 6 / 9 / 5).

### Pipeline, prompts and evaluation runner

Any change made after seeing evaluation output is listed here. Results on this set after
such a change are not independent of development.

| Date | Commit | Change | Reason |
|---|---|---|---|
| 2026-11-25 | `2629233` | Mock only: location cue words match in any case | The mock missed "Headquartered" at the start of a sentence. Not a model change |
| 2026-11-25 | `413998b` | Evaluation runner: the budget is the per-company caps times the number of cases | Run 1 shared the web app's per-run cap and stopped after 14 cases (see Results) |
| 2026-11-25 | `7880ef4` | Evidence check: the description may rest on quotes the model filed under "business", not only under "description". The claim text sent to the verifier now says that the quotes must state every statement in the description. The verifier prompt gained two rules: a partly stated description, or a quote that only names the company, does not support it; promotional or self-assessing wording is not an established fact. Name quotes are never used for the description | **Observed during development on case 01**, the first one-case Claude Code check: the model quoted the right sentence but filed it under business and name, so the description was removed. The project owner chose this option. It is not meant to fix the 5 descriptions removed in run 2: there the quotes stated only part of the description, and such descriptions are still rejected |

| 2026-11-25 | the commit after `7880ef4` | Reports: billing wording for Claude Code; unconfirmed facts before and after the checks; code version; `--rescore` for saved runs | The old wording ("no per-token charge") claimed more than the tool can know. No effect on answers or labels |

The `7880ef4` change touches the verifier prompt, so the prompts fingerprint changes. Its effect
on this set is **not measured yet**. It cannot be recomputed from the saved answers,
because the verifier would have to judge description claims it never saw.
