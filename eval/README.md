# Synthetic evaluation set

**Every case in this folder is synthetic.** The companies, websites, people and investors
are invented; any resemblance to real organisations is unintended. All URLs use the
reserved `.example` top-level domain and are never requested from the internet: pages
are served from these files by `FixtureFetcher`, in mock mode and in live mode alike.

## How the expected answers were made

- Cases and expected answers were written by the AI coding assistant (Claude) during
  development on 2026-11-25, applying the label definitions below to the page texts.
- They were committed **before any live evaluation run** (a run with a real LLM). The git
  history shows the order.
- **Label review (2026-11-26).** The assistant prepared a review table: for each case the
  criteria, the relevant page text, the expected label and facts, and the reasoning. Model
  results were left out of it. The author went through it together with remarks from a
  second AI assistant (Codex), agreed with the conclusions and decided the geography rule
  below. That changed three labels. This was **not** an independent manual check of all
  source material by the author or by anyone else.
- Expected answers must not be changed after seeing model results without recording the
  change and the reason in `docs/evaluation.md`. Changed so far: cases 03, 12 and 13, on
  2026-11-26, after the first two full model runs had been seen.
- Case ids stay as first written, so they still match saved runs. The ids of 03, 12 and 13
  still say `nonfit`, their first label.
- Case 01 was used during development: the first real-model check, which led to a change
  in the evidence rules. All 20 cases have since been seen in two full runs, kept in
  [`results/`](results). The set is no longer blind; see the change log in
  `docs/evaluation.md`.

## Label definitions

The same rules the app applies after evidence checks (`screener.verify.decide_label`):

| Label | When |
|---|---|
| `non_fit` | the text explicitly shows that an exclusion applies, or that a criterion is not met |
| `fit` | the text explicitly shows that industry, geography and target business are all met, and no exclusion is shown to apply |
| `insufficient_information` | anything else: information is missing, vague or only indirect |

Geography means where the company **itself** is located (headquarters, offices, plants
or other own facilities). Markets it serves, trade fairs, phone codes, currencies,
languages or domains do not count. Exclusions are judged only on what the text states.

**Geography rule (agreed 2026-11-26).**
- **Met:** the text confirms at least one own site in the target region. Any one is
  enough; it need not be the headquarters.
- **Not met:** only if the text confirms that the company has no own site there. Own
  sites elsewhere, such as a headquarters abroad, do not show that.
- **Otherwise unknown.** Negative statements are not added to the pages just to keep a
  `non_fit` label.

The app applies the same rule since commit `87084cc`: in the agent prompt, the evidence
check and the mock. Of the saved model runs in [`results/`](results), the two from
2026-11-25 predate it; the run from 2026-11-26 uses it.

## Case format

One JSON file per case, `<id>.json`:

| Field | Meaning |
|---|---|
| `synthetic` | always `true` |
| `criteria` | the screening criteria, as a user would enter them |
| `start_url` | the URL a user would paste |
| `pages` | URL → `{"html": [lines]}`, or `{"status": 404}`, or `{"status": 302, "location": ...}` |
| `expected.fit` | expected label |
| `expected.company_name` | accepted spellings of the name |
| `expected.company_locations` | one list per own location, each with accepted spellings |
| `expected.forbidden_locations` | places that must not be reported as an own location |
| `expected.markets_served` | markets the text states (used only for a recall figure) |
| `label_rationale` | why the expected label follows from the text |

## Limitations

20 small, hand-made cases cannot show how well the tool works on real company websites.
The same assistant wrote the prompts and these cases, so results may be optimistic. Use
this set to catch regressions and to compare settings, not to claim real-world accuracy.
