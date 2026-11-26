# Evaluation report: Claude Code (subscription)

> CLAUDE CODE MODE: the real model through your Claude Code login (Claude subscription). Usage counts against your subscription limits; there is no per-token charge. The numbers describe this small synthetic set of invented companies only; they are not evidence of accuracy on real company websites.

- Billing: Claude subscription: usage limits, no per-token charge
- Started 2026-11-25T17:06:38+00:00, wall time 267.66 s, app version 0.1.0
- Model: claude-sonnet-5-5 via Claude Code (subscription), effort low
- Cases: 20 synthetic (`eval/cases`, sha256 `1cdffc833ab6`); prompts sha256 `1e4d53b4453f`

## Classification

Final label = after evidence checks and the explicit label rules. A case without a label (an error) counts as wrong.

| | Accuracy | Macro-F1 |
|---|---|---|
| Final label | 1.00 (20/20) | 1.00 |
| Model's own label, before checks | 1.00 (20/20) | 1.00 |

| Label (final) | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| fit | 1.00 | 1.00 | 1.00 | 6 |
| non_fit | 1.00 | 1.00 | 1.00 | 9 |
| insufficient_information | 1.00 | 1.00 | 1.00 | 5 |

Confusion matrix of final labels (rows: expected, columns: predicted):

| expected \ predicted | fit | non_fit | insufficient_information | no_label |
|---|---|---|---|---|
| fit | 6 | 0 | 0 | 0 |
| non_fit | 0 | 9 | 0 | 0 |
| insufficient_information | 0 | 0 | 5 | 0 |

## Structured output, evidence and facts

- Final answers that passed schema validation: 20 of 20 (100%)
- Result statuses: ok 20
- Evidence items: 106; quote verbatim on the cited page: 100%; of those, supported by the second check: 94%
- Company name correct: 20/20; own locations exactly right: 20/20; markets found: 7/7 (labelled cases only)
- Invented or trap locations before checks: none; after checks: none
- Tool calls: 12 in 8 cases

## Time, tokens and cost

- Time per case: median 12.1 s, max 18.5 s
- API requests: 81 (0 failed); tokens: 116 input, 34,524 output, 39,393 cache write, 166,636 cache read
- Cost: no per-token charge (Claude subscription usage limits apply). API-equivalent estimate: $0.4606 (what the reported tokens would cost on the API at the listed tariffs; an estimate for comparison, not an actual charge). Tariffs: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-25).

## Per case

| Case | Expected | Final | Model | Status | Own locations | Tools | Requests | Time, s |
|---|---|---|---|---|---|---|---|---|
| 01-valves-fit-homepage | fit | fit | fit | ok | Dortmund, Germany | 1 | 5 | 18.1 |
| 02-valves-fit-location-on-about | fit | fit | fit | ok | Kassel, Germany | 2 | 5 | 11.6 |
| 03-valves-nonfit-other-country | non_fit | non_fit | non_fit | ok | Valencia, Spain | 0 | 3 | 11.2 |
| 04-valves-insufficient-markets-only | insufficient_information | insufficient_information | insufficient_information | ok | - | 1 | 5 | 18.5 |
| 05-valves-insufficient-indirect-signals | insufficient_information | insufficient_information | insufficient_information | ok | - | 1 | 4 | 10.4 |
| 06-valves-nonfit-distributor | non_fit | non_fit | non_fit | ok | Hamburg, Germany | 0 | 4 | 16.6 |
| 07-valves-nonfit-private-equity | non_fit | non_fit | non_fit | ok | Leipzig, Germany | 1 | 4 | 11.8 |
| 08-valves-nonfit-injection-bakery | non_fit | non_fit | non_fit | ok | Bremen, Germany | 0 | 3 | 9.9 |
| 09-valves-nonfit-hidden-injection | non_fit | non_fit | non_fit | ok | Augsburg, Germany | 0 | 4 | 15.8 |
| 10-software-fit-switzerland | fit | fit | fit | ok | Zug, Switzerland | 0 | 4 | 15.2 |
| 11-software-nonfit-photography | non_fit | non_fit | non_fit | ok | Graz, Austria | 0 | 3 | 9.5 |
| 12-software-nonfit-latvia | non_fit | non_fit | non_fit | ok | Riga, Latvia | 0 | 3 | 11.9 |
| 13-software-nonfit-trade-fair | non_fit | non_fit | non_fit | ok | Lyon, France | 0 | 4 | 15.4 |
| 14-software-insufficient-vague | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 10.3 |
| 15-software-fit-german-language | fit | fit | fit | ok | Freiburg im Breisgau, Deutschland | 0 | 4 | 16.0 |
| 16-software-fit-second-office | fit | fit | fit | ok | Utrecht, Netherlands; Munich, Germany | 0 | 4 | 15.9 |
| 17-bakery-fit-poland | fit | fit | fit | ok | Poznań, Poland | 0 | 4 | 15.1 |
| 18-bakery-nonfit-retail-only | non_fit | non_fit | non_fit | ok | Kraków, Poland | 0 | 3 | 12.3 |
| 19-bakery-insufficient-missing-pages | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 10.7 |
| 20-bakery-insufficient-offsite-redirect | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 11.3 |
