# Evaluation report: Claude Code (subscription)

> CLAUDE CODE MODE: the real model through your Claude Code login (Claude subscription). Usage counts against your subscription limits; there is no per-token charge. The numbers describe this small synthetic set of invented companies only; they are not evidence of accuracy on real company websites.

- Billing: Claude subscription: usage limits, no per-token charge
- Started 2026-11-25T17:00:09+00:00, wall time 227.02 s, app version 0.1.0
- Model: claude-sonnet-5-5 via Claude Code (subscription), effort low
- Cases: 20 synthetic (`eval/cases`, sha256 `1cdffc833ab6`); prompts sha256 `1e4d53b4453f`

## Classification

Final label = after evidence checks and the explicit label rules. A case without a label (an error) counts as wrong.

| | Accuracy | Macro-F1 |
|---|---|---|
| Final label | 0.70 (14/20) | 0.79 |
| Model's own label, before checks | 0.75 (15/20) | 0.83 |

| Label (final) | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| fit | 1.00 | 0.50 | 0.67 | 6 |
| non_fit | 1.00 | 0.89 | 0.94 | 9 |
| insufficient_information | 1.00 | 0.60 | 0.75 | 5 |

Confusion matrix of final labels (rows: expected, columns: predicted):

| expected \ predicted | fit | non_fit | insufficient_information | no_label |
|---|---|---|---|---|
| fit | 3 | 0 | 0 | 3 |
| non_fit | 0 | 8 | 0 | 1 |
| insufficient_information | 0 | 0 | 3 | 2 |

## Structured output, evidence and facts

- Final answers that passed schema validation: 15 of 15 (100%)
- Result statuses: budget_exceeded 6, ok 14
- Evidence items: 73; quote verbatim on the cited page: 100%; of those, supported by the second check: 96%
- Company name correct: 14/14; own locations exactly right: 14/14; markets found: 7/7 (labelled cases only)
- Invented or trap locations before checks: none; after checks: none
- Tool calls: 9 in 5 cases

## Time, tokens and cost

- Time per case: median 11.21 s, max 27.29 s
- API requests: 60 (0 failed); tokens: 84 input, 25,515 output, 47,539 cache write, 104,145 cache read
- Cost: no per-token charge (Claude subscription usage limits apply). API-equivalent estimate: $0.3846 (what the reported tokens would cost on the API at the listed tariffs; an estimate for comparison, not an actual charge). Tariffs: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-25).

## Per case

| Case | Expected | Final | Model | Status | Own locations | Tools | Requests | Time, s |
|---|---|---|---|---|---|---|---|---|
| 01-valves-fit-homepage | fit | fit | fit | ok | Dortmund, Germany | 0 | 3 | 27.3 |
| 02-valves-fit-location-on-about | fit | fit | fit | ok | Kassel, Germany | 2 | 6 | 24.6 |
| 03-valves-nonfit-other-country | non_fit | non_fit | non_fit | ok | Valencia, Spain | 0 | 3 | 10.6 |
| 04-valves-insufficient-markets-only | insufficient_information | insufficient_information | insufficient_information | ok | - | 3 | 7 | 20.6 |
| 05-valves-insufficient-indirect-signals | insufficient_information | insufficient_information | insufficient_information | ok | - | 1 | 4 | 11.5 |
| 06-valves-nonfit-distributor | non_fit | non_fit | non_fit | ok | Hamburg, Germany | 0 | 3 | 11.9 |
| 07-valves-nonfit-private-equity | non_fit | non_fit | non_fit | ok | Leipzig, Germany | 1 | 5 | 17.9 |
| 08-valves-nonfit-injection-bakery | non_fit | non_fit | non_fit | ok | Bremen, Germany | 0 | 3 | 10.3 |
| 09-valves-nonfit-hidden-injection | non_fit | non_fit | non_fit | ok | Augsburg, Germany | 0 | 3 | 9.9 |
| 10-software-fit-switzerland | fit | fit | fit | ok | Zug, Switzerland | 0 | 4 | 17.4 |
| 11-software-nonfit-photography | non_fit | non_fit | non_fit | ok | Graz, Austria | 0 | 3 | 9.9 |
| 12-software-nonfit-latvia | non_fit | non_fit | non_fit | ok | Riga, Latvia | 0 | 4 | 17.3 |
| 13-software-nonfit-trade-fair | non_fit | non_fit | non_fit | ok | Lyon, France | 0 | 4 | 14.7 |
| 14-software-insufficient-vague | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 11.0 |
| 15-software-fit-german-language | fit | - | fit | budget_exceeded | - | 0 | 3 | 12.2 |
| 16-software-fit-second-office | fit | - | - | budget_exceeded | - | 0 | 0 | 0.0 |
| 17-bakery-fit-poland | fit | - | - | budget_exceeded | - | 0 | 0 | 0.0 |
| 18-bakery-nonfit-retail-only | non_fit | - | - | budget_exceeded | - | 0 | 0 | 0.0 |
| 19-bakery-insufficient-missing-pages | insufficient_information | - | - | budget_exceeded | - | 0 | 0 | 0.0 |
| 20-bakery-insufficient-offsite-redirect | insufficient_information | - | - | budget_exceeded | - | 0 | 0 | 0.0 |
