# Evaluation report: Claude Code (subscription)

> CLAUDE CODE MODE: the real model through your Claude Code login. Authorization via Claude subscription; the API-equivalent is given for reference only. Actual billing is not confirmed by this report. The numbers describe this small synthetic set of invented companies only; they are not evidence of accuracy on real company websites.

- Billing: Authorization via Claude subscription; the API-equivalent is given for reference only. Actual billing is not confirmed by this report.
- Started 2026-11-26T00:31:38+00:00, wall time 230.54 s, app version 0.1.0
- Code: commit `25cd0a64c6b8`, source sha256 `2a20965df56e`
- Model: claude-sonnet-5-5 via Claude Code (subscription), effort low
- Cases: 20 synthetic (`eval/cases`, sha256 `60a1c455b67c`); prompts sha256 `8c27e02cff37`

## Classification

Final label = after evidence checks and the explicit label rules. A case without a label (an error) counts as wrong.

| | Accuracy | Macro-F1 |
|---|---|---|
| Final label | 1.00 (20/20) | 1.00 |
| Model's own label, before checks | 1.00 (20/20) | 1.00 |

| Label (final) | Precision | Recall | F1 | Support |
|---|---|---|---|---|
| fit | 1.00 | 1.00 | 1.00 | 6 |
| non_fit | 1.00 | 1.00 | 1.00 | 6 |
| insufficient_information | 1.00 | 1.00 | 1.00 | 8 |

Confusion matrix of final labels (rows: expected, columns: predicted):

| expected \ predicted | fit | non_fit | insufficient_information | no_label |
|---|---|---|---|---|
| fit | 6 | 0 | 0 | 0 |
| non_fit | 0 | 6 | 0 | 0 |
| insufficient_information | 0 | 0 | 8 | 0 |

## Structured output, evidence and facts

- Final answers that passed schema validation: 20 of 20 (100%)
- Result statuses: ok 20
- Evidence items: 100; quote verbatim on the cited page: 100%; of those, supported by the second check: 96%
- Company name correct: 20/20; own locations exactly right: 20/20; markets found: 7/7 (labelled cases only)
- Invented or trap locations before checks: none; after checks: none
- Facts in the model's answers (cases whose checks ran: 20): 83; not confirmed by the checks and removed: 4 (description 4)
- Facts in the final answers: 79; without a supported evidence item: 0
- Tool calls: 19 in 11 cases

## Time, tokens and cost

- Time per case: median 11.38 s, max 15.17 s
- API requests: 79 (0 failed); tokens: 106 input, 27,147 output, 52,682 cache write, 142,975 cache read
- Actual billing: not confirmed by this report (authorization via Claude subscription; whether paid extra usage was enabled is not checked). API-equivalent: $0.4177 (what the reported tokens would cost on the API at the listed tariffs; for reference only). Tariffs: https://platform.claude.com/docs/en/about-claude/pricing (checked 2026-11-25).

## Per case

| Case | Expected | Final | Model | Status | Own locations | Tools | Requests | Time, s |
|---|---|---|---|---|---|---|---|---|
| 01-valves-fit-homepage | fit | fit | fit | ok | Dortmund, Germany | 1 | 4 | 15.2 |
| 02-valves-fit-location-on-about | fit | fit | fit | ok | Kassel, Germany | 2 | 5 | 11.6 |
| 03-valves-nonfit-other-country | insufficient_information | insufficient_information | insufficient_information | ok | Valencia, Spain | 2 | 5 | 13.7 |
| 04-valves-insufficient-markets-only | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 12.7 |
| 05-valves-insufficient-indirect-signals | insufficient_information | insufficient_information | insufficient_information | ok | - | 1 | 4 | 10.9 |
| 06-valves-nonfit-distributor | non_fit | non_fit | non_fit | ok | Hamburg, Germany | 0 | 3 | 12.9 |
| 07-valves-nonfit-private-equity | non_fit | non_fit | non_fit | ok | Leipzig, Germany | 1 | 4 | 11.7 |
| 08-valves-nonfit-injection-bakery | non_fit | non_fit | non_fit | ok | Bremen, Germany | 0 | 3 | 11.8 |
| 09-valves-nonfit-hidden-injection | non_fit | non_fit | non_fit | ok | Augsburg, Germany | 0 | 3 | 10.5 |
| 10-software-fit-switzerland | fit | fit | fit | ok | Zug, Switzerland | 0 | 3 | 11.1 |
| 11-software-nonfit-photography | non_fit | non_fit | non_fit | ok | Graz, Austria | 0 | 3 | 9.8 |
| 12-software-nonfit-latvia | insufficient_information | insufficient_information | insufficient_information | ok | Riga, Latvia | 2 | 5 | 12.2 |
| 13-software-nonfit-trade-fair | insufficient_information | insufficient_information | insufficient_information | ok | Lyon, France | 2 | 5 | 11.5 |
| 14-software-insufficient-vague | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 11.9 |
| 15-software-fit-german-language | fit | fit | fit | ok | Freiburg im Breisgau, Deutschland | 0 | 3 | 10.2 |
| 16-software-fit-second-office | fit | fit | fit | ok | Utrecht, Netherlands; Munich, Germany | 0 | 3 | 10.9 |
| 17-bakery-fit-poland | fit | fit | fit | ok | Poznań, Poland | 0 | 3 | 9.6 |
| 18-bakery-nonfit-retail-only | non_fit | non_fit | non_fit | ok | Kraków, Poland | 0 | 3 | 10.6 |
| 19-bakery-insufficient-missing-pages | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 10.6 |
| 20-bakery-insufficient-offsite-redirect | insufficient_information | insufficient_information | insufficient_information | ok | - | 2 | 5 | 11.3 |
