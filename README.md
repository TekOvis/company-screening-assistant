# Company Screening Assistant

Screens up to ten company websites against your criteria (industry, geography, target
business, exclusions) with an LLM, and shows only facts that are backed by verbatim quotes
from the fetched pages.

> **Educational prototype — not financial or investment advice.** The evaluation data is
> synthetic; nothing in this repository measures accuracy on real companies.

## What it does

1. You enter up to 10 URLs and the criteria, in a web form or through a JSON API.
2. The app fetches each homepage over plain HTTP(S), with SSRF protection.
3. Claude (one model, `claude-sonnet-5-5` by default) reads the page text. It may call one
   tool, `fetch_company_page`, to read other pages of the **same** site, and must answer
   with a JSON object that matches a schema (structured output).
4. Every fact is checked. Code verifies that each quote is verbatim on a fetched page; a
   second, batched check confirms that the quote actually states the claim.
5. Unsupported facts are removed, and the label — `fit`, `non_fit` or
   `insufficient_information` — is recomputed by explicit rules.
6. Results appear in a table and can be exported as JSON or CSV.

```mermaid
flowchart LR
  U[Form or JSON API] --> V[URL rules]
  V --> F[Safe fetcher: own DNS lookup,<br/>public-IP check, pinned connection]
  F --> A[Agent loop with<br/>structured output]
  A -- "fetch_company_page<br/>(same site only)" --> F
  A --> S1[Stage 1: cited page fetched,<br/>quote verbatim]
  S1 --> S2[Stage 2: one batched<br/>semantic check]
  S2 --> R[Explicit label rules]
  R --> T[Table, JSON, CSV]
```

More detail: [docs/architecture.md](docs/architecture.md).

## Quick start

Requires Python 3.12 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run pytest                                              # no network, no API key
uv run uvicorn screener.web:create_app --factory --reload  # http://127.0.0.1:8000
```

The default is **mock mode**: a deterministic stand-in for the model built from keyword
rules. It needs no API key and costs nothing. It exists for development and CI and says
nothing about the quality of a real model; the UI shows a "Mock mode" badge.

To use the real model, put the settings in a local `.env` file (git-ignored; see
`.env.example`) or in environment variables:

```bash
LLM_BACKEND=anthropic
ANTHROPIC_API_KEY=...        # never commit this
LLM_MODEL=claude-sonnet-5-5  # optional, this is the default
```

Live mode calls a paid API. Every request counts against the limits described below.

## Example

`POST /api/screen` with a synthetic company from the evaluation set:

```json
{
  "urls": ["https://marisol-valvulas.example/"],
  "criteria": {
    "industry": "industrial valves",
    "geography": "Germany",
    "target_business": "Manufacturer of industrial valves or other flow-control equipment",
    "exclusions": "pure distributors or resellers; companies owned by private equity firms"
  }
}
```

The page says: *"Headquartered in Valencia, Spain, we export to customers across Europe,
including Germany and France."* Response, shortened. It was produced in mock mode, so it
shows the format, not model quality:

```json
{
  "status": "ok",
  "company_name": "Marisol Valvulas",
  "fit": "non_fit",
  "model_fit": "non_fit",
  "decision_note": "Rules after evidence checks: industry met, geography not_met, business met, exclusion not triggered -> non_fit.",
  "company_locations": ["Valencia", "Spain"],
  "markets_served": ["Europe"],
  "evidence": [
    {
      "supports": "description",
      "quote": "Marisol Valvulas manufactures industrial gate and globe valves for the oil and gas industry.",
      "url": "https://marisol-valvulas.example/",
      "check": "supported"
    }
  ],
  "warnings": [],
  "usage": {"llm_requests": 2, "failed_requests": 0, "input_tokens": 1801, "output_tokens": 514}
}
```

Exports: `GET /runs/{id}/export.json` and `GET /runs/{id}/export.csv`. The CSV is
protected against formula injection.

## Configuration

All settings are environment variables; `.env.example` lists every one with its default.

| Setting | Default | Meaning |
|---|---|---|
| `LLM_BACKEND` | `mock` | `mock` (free stand-in) or `anthropic` (real API) |
| `LLM_MODEL` | `claude-sonnet-5-5` | one model for the agent and the evidence check |
| `LLM_EFFORT` | `low` | effort level sent to the API |
| `MAX_TOOL_CALLS` / `MAX_LLM_CALLS` | 3 / 4 | tool calls and agent turns per company |
| `MAX_LLM_REQUESTS_PER_COMPANY` | 8 | every API attempt, including retries and the check |
| `MAX_TOKENS_PER_COMPANY` / `_PER_RUN` | 80,000 / 600,000 | token budgets, reserved before each call |
| `MAX_URLS_PER_REQUEST` | 10 | URLs per screening |
| `MAX_ACTIVE_RUNS` / `MAX_STORED_RUNS` / `RUN_TTL_S` | 2 / 50 / 7200 | in-memory job limits |

## Evaluation

The synthetic set in [`eval/cases`](eval/cases) has 20 invented companies on reserved
`.example` domains (6 `fit`, 9 `non_fit`, 5 `insufficient_information`), including traps
such as markets mistaken for locations, trade fairs, indirect signals (phone codes,
currency), visible and hidden prompt injection, and an off-site redirect. Expected answers
were committed before any live run and still need human review. See
[eval/README.md](eval/README.md).

```bash
uv run python -m screener.evaluate --mode mock                                        # free pipeline check
uv run python -m screener.evaluate --mode live --only 01-valves-fit-homepage --yes    # one case, real model
uv run python -m screener.evaluate --mode live --yes                                  # whole set, real model
```

Pages always come from the saved files, also in live mode. A live run prints its hard
spending cap first and does not start without `--yes`. Reports include schema-valid rate,
accuracy, macro-F1, a confusion matrix, fact checks, time, tokens and cost at explicitly
listed tariffs.

**Current results.** Live evaluation has **not been run yet**; no API key is connected.
The mock run scores 13/20, but it only shows that the pipeline works end to end; keyword
rules are not a model. Methodology and details: [docs/evaluation.md](docs/evaluation.md).

## Security and cost controls

- **SSRF.** http/https on default ports only; no IP literals, `localhost`-style or reserved
  names. The app resolves DNS itself, requires every address to be public, and connects
  to the checked IP while TLS still verifies the host name. Redirects are re-checked on
  every hop, and proxy variables are ignored.
- **Prompt injection.** Page text is treated as untrusted data. The `<page>` delimiters
  are not a security boundary, so the real limits are elsewhere: one narrow tool whose
  arguments are validated by code, same-site fetching after redirects, hard limits on
  turns, tool calls and tokens, verbatim quote checks, and labels computed by code.
- **Budgets.** Every API attempt counts against per-company and per-run budgets: agent
  turns, the evidence check and retries. The worst case is reserved before each call.
  Temporary errors are retried by the app, not the SDK, so each attempt is visible. A
  provider spend-limit error is never retried and stops the run.
- **Output.** HTML is escaped, only `http(s)` URLs become links, and CSV cells that look
  like formulas are neutralised.
- **Jobs.** Results live in memory only, with caps on stored jobs, their lifetime and
  jobs running at once.

A provider-side spend limit is useful as a last line of defence. The documentation does
not say how quickly it takes effect, so it does not replace the app's own limits.

## Limitations

- Plain HTTP only: pages that need JavaScript show little text. A browser-based fetcher
  could be added as another `PageFetcher` implementation; it is not part of this MVP.
- Only HTML and plain text, no PDFs. `www.` and the bare domain count as one site; other
  subdomains are blocked for tool calls.
- The second check is an LLM too and can be wrong. Quotes are verified by code, but their
  meaning is not proven.
- Geography means where the company itself is located. Whether a place belongs to a
  region (for example "DACH") is judged by the model.
- Exclusions are judged only on what the pages state.
- Single process; runs are lost on restart.
- Costs are computed from reported usage. Failed requests report none, and a request that
  timed out on the client side may still have been billed.
- The evaluation set is small and synthetic, and the same assistant wrote it and the
  prompts, so results may be optimistic.

## AI-assisted development

This project was built with an AI coding assistant, Claude Code by Anthropic.

- **The author (Ihnat Hordiienko)** defined the task, requirements and constraints, chose
  the provider and model, and decided between options at each review checkpoint. For
  example: keep company locations separate from markets served; no fallback to another
  model; batch the evidence check and do not treat its verdicts as proof; keep tool
  calls on the same site after redirects; count every request, including retries,
  against the budgets.
- **The assistant** wrote the code, tests, synthetic evaluation cases with their expected
  answers, and this documentation, and ran the local checks: tests, linting, mock
  evaluation and manual smoke tests against public websites. Commits carry a
  `Co-Authored-By: Claude` trailer.
- **Not done yet:** an independent code review, a human review of the evaluation labels,
  and a live evaluation with the real model.

## License

The project's own code and documentation are MIT-licensed ([LICENSE](LICENSE)).
Dependencies keep their own licenses: see [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)
and [`third_party_licenses/`](third_party_licenses).
