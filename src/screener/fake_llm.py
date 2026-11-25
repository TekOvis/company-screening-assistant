"""Deterministic stand-in for the Anthropic client — used in mock mode and in tests.

This is NOT a language model. It lets the whole application (request building, the
tool-calling loop, schema validation, evidence checks, UI, exports and evaluation code) run
in development and CI with no API key and no paid calls. Its answers come from simple
keyword rules, so mock results say nothing about the quality of a real model.

It mimics the one SDK method the app uses — ``client.messages.create(**params)`` — and
returns real ``anthropic.types.Message`` objects, so the app code path is the same as with
the real client. It answers two kinds of requests: the screening agent and the evidence
verifier (recognised by the <claims_to_check> block).
"""

from __future__ import annotations

import itertools
import json
import math
import re
from collections.abc import Callable, Iterable
from typing import Any

from anthropic.types import Message

from .schemas import Assessment, CriterionStatus, EvidenceItem, FitLabel, ScreeningCriteria

MOCK_MODEL_NAME = "mock-heuristic"
_message_ids = itertools.count(1)

_CRITERIA_RE = re.compile(r"<screening_criteria>\n(.*?)\n</screening_criteria>", re.S)
_CLAIMS_RE = re.compile(r"<claims_to_check>\n(.*?)\n</claims_to_check>", re.S)
_PAGE_RE = re.compile(r'<page url="([^"]+)">\n(.*?)\n</page>', re.S)
_LINKS_RE = re.compile(r'<site_links page="[^"]*">\n(.*?)\n</site_links>', re.S)
_WORD_RE = re.compile(r"[a-z][a-z\-]+")
_SENTENCE_END_RE = re.compile(r"(?<=[.!?])\s+")
_PLACE = r"([A-Z][\w'-]*(?:(?:,\s*(?:and\s+)?|\s+and\s+|\s+)[A-Z][\w'-]*)*)"
_OWN_LOCATION_RE = re.compile(
    r"\b(?:headquartered|headquarters|head office|based|located|registered|offices?|plants?|"
    r"factor(?:y|ies)|facilit(?:y|ies)|warehouses?|production sites?)\b[^.]*?\bin\s+" + _PLACE
)
_MARKET_RE = re.compile(
    r"\b(?:serv(?:e|es|ing)|customers?|clients?|sell(?:s|ing)?|export(?:s|ing)?|"
    r"ship(?:s|ping)?|deliver(?:s|ing)?|distribut\w*)\b[^.]*?\b(?:in|to|across|throughout)\s+"
    + _PLACE
)
_MARKET_WORD_RE = re.compile(
    r"\b(?:customers?|clients?|serv(?:e|es|ing)|sell(?:s|ing)?|deliver\w*|export\w*|"
    r"ship(?:s|ping)?|partners?|distributors?|resellers?|fairs?|exhibitions?)\b",
    re.IGNORECASE,
)
_HELPFUL_PATH_RE = re.compile(
    r"about|company|contact|location|office|who-we-are|team|imprint|impressum", re.I
)
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "are", "our", "you", "your", "its",
    "their", "has", "have", "was", "were", "will", "can", "all", "any", "not", "but", "into",
    "about", "more", "than", "other", "such", "also", "who", "which", "what", "how", "they",
    "them", "these", "those", "there", "here", "out", "off", "per", "via", "etc",
    "company", "companies", "firm", "firms", "business", "businesses", "provider",
    "providers", "solution", "solutions", "industry", "industries", "sector", "leading",
    "global", "world", "small", "medium", "large", "sized", "size", "quality", "focus",
    "focused", "exclude", "excluding", "excluded", "type", "types", "based", "owned",
    "description", "works",
}  # fmt: skip
_REGIONS = {
    "europe": {
        "europe", "austria", "belgium", "bulgaria", "croatia", "cyprus", "czechia",
        "czech republic", "denmark", "estonia", "finland", "france", "germany", "greece",
        "hungary", "ireland", "italy", "latvia", "lithuania", "luxembourg", "malta",
        "netherlands", "poland", "portugal", "romania", "slovakia", "slovenia", "spain",
        "sweden", "norway", "switzerland", "united kingdom", "uk", "iceland", "ukraine",
    },
    "dach": {"germany", "austria", "switzerland"},
    "nordic": {"denmark", "finland", "iceland", "norway", "sweden"},
    "benelux": {"belgium", "netherlands", "luxembourg"},
    "north america": {"united states", "usa", "canada", "mexico"},
}  # fmt: skip

ScriptItem = Message | Exception | Callable[[dict[str, Any]], Message]


class FakeAnthropicClient:
    """Drop-in for ``anthropic.AsyncAnthropic`` (only ``messages.create`` is implemented).

    * Without a script it answers with keyword heuristics (mock mode).
    * With a script it returns the given messages / raises the given errors in order
      (unit tests); a callable item is called with the request parameters.
    """

    def __init__(self, script: Iterable[ScriptItem] | None = None) -> None:
        self.messages = _FakeMessages(list(script) if script is not None else None)

    @property
    def requests(self) -> list[dict[str, Any]]:
        """Every request the app sent, for assertions in tests."""
        return self.messages.requests


class _FakeMessages:
    def __init__(self, script: list[ScriptItem] | None) -> None:
        self._script = script
        self.requests: list[dict[str, Any]] = []

    async def create(self, **params: Any) -> Message:
        # Copy the message list: the app keeps appending to it after the call.
        self.requests.append({**params, "messages": list(params.get("messages", []))})
        if self._script is None:
            return heuristic_reply(params)
        if not self._script:
            raise AssertionError("FakeAnthropicClient: script exhausted")
        item = self._script.pop(0)
        if isinstance(item, Exception):
            raise item
        if isinstance(item, Message):
            return item
        return item(params)


# --- Message builders (also used by tests) --------------------------------------------


def make_message(
    content: list[dict[str, Any]],
    *,
    stop_reason: str = "end_turn",
    input_tokens: int = 100,
    output_tokens: int = 50,
    model: str = MOCK_MODEL_NAME,
) -> Message:
    return Message.model_validate(
        {
            "id": f"msg_mock_{next(_message_ids)}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": content,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    )


def text_message(text: str, **kwargs: Any) -> Message:
    return make_message([{"type": "text", "text": text}], **kwargs)


def tool_use_message(
    tool_input: dict[str, Any], *, tool_id: str | None = None, **kwargs: Any
) -> Message:
    block = {
        "type": "tool_use",
        "id": tool_id or f"toolu_mock_{next(_message_ids)}",
        "name": "fetch_company_page",
        "input": tool_input,
    }
    return make_message([block], stop_reason="tool_use", **kwargs)


# --- Heuristic "brain" -----------------------------------------------------------------


def heuristic_reply(params: dict[str, Any]) -> Message:
    input_tokens = _estimate_tokens(params)
    first_text = next(
        (text for m in params.get("messages", []) for text, _ in _iter_blocks(_get(m, "content"))),
        "",
    )
    if match := _CLAIMS_RE.search(first_text or ""):
        verdicts = [_judge(item) for item in json.loads(match.group(1))]
        text = json.dumps({"verdicts": verdicts})
        return text_message(text, input_tokens=input_tokens, output_tokens=_tokens(text))

    context = _read_context(params.get("messages", []))
    assessment = _assess(context)
    tool_choice = (params.get("tool_choice") or {}).get("type", "auto")
    if params.get("tools") and tool_choice != "none":
        if "unknown" in (assessment.industry_match, assessment.geography_match):
            path = _next_helpful_path(context)
            if path:
                return tool_use_message({"path": path}, input_tokens=input_tokens, output_tokens=30)
    text = assessment.model_dump_json()
    return text_message(text, input_tokens=input_tokens, output_tokens=_tokens(text))


class _Context:
    def __init__(self) -> None:
        self.criteria: ScreeningCriteria | None = None
        self.pages: dict[str, str] = {}  # url -> text
        self.links: list[str] = []  # candidate paths, in order of appearance
        self.requested_paths: set[str] = set()


def _read_context(messages: list[Any]) -> _Context:
    context = _Context()
    for message in messages:
        role = _get(message, "role")
        for block_text, tool_path in _iter_blocks(_get(message, "content")):
            if tool_path is not None:
                context.requested_paths.add(tool_path)
            if not block_text or role != "user":
                continue
            if context.criteria is None and (match := _CRITERIA_RE.search(block_text)):
                context.criteria = ScreeningCriteria.model_validate(json.loads(match.group(1)))
            for url, text in _PAGE_RE.findall(block_text):
                context.pages[url] = text
            for links_block in _LINKS_RE.findall(block_text):
                for line in links_block.splitlines():
                    path = line.split(" | ", 1)[0].strip()
                    if path.startswith("/") and path not in context.links:
                        context.links.append(path)
    if context.criteria is None:
        raise ValueError("mock client: no <screening_criteria> block in the request")
    return context


def _iter_blocks(content: Any) -> Iterable[tuple[str | None, str | None]]:
    """Yield (text, requested_tool_path) for every block of a message's content."""
    if isinstance(content, str):
        yield content, None
        return
    for block in content or []:
        block_type = _get(block, "type")
        if block_type == "text":
            yield _get(block, "text"), None
        elif block_type == "tool_result":
            inner = _get(block, "content")
            if isinstance(inner, str):
                yield inner, None
            else:
                for part in inner or []:
                    if _get(part, "type") == "text":
                        yield _get(part, "text"), None
        elif block_type == "tool_use":
            tool_input = _get(block, "input") or {}
            yield None, str(tool_input.get("path", ""))


def _next_helpful_path(context: _Context) -> str | None:
    fetched_paths = {re.sub(r"^https?://[^/]+", "", url) or "/" for url in context.pages}
    for path in context.links:
        if path in context.requested_paths or path in fetched_paths:
            continue
        if _HELPFUL_PATH_RE.search(path):
            return path
    return None


def _assess(context: _Context) -> Assessment:
    criteria = context.criteria
    if criteria is None:
        raise ValueError("mock client: screening criteria are missing")
    sentences = [(url, s) for url, text in context.pages.items() for s in _sentences(text)]
    content_sentences = [
        (url, s) for url, s in sentences if not s.startswith(("Title: ", "Meta description: "))
    ]
    evidence: list[EvidenceItem] = []

    # Company name: first part of the homepage title.
    company_name = None
    for url, sentence in sentences:
        if sentence.startswith("Title: "):
            title = sentence.removeprefix("Title: ")
            company_name = re.split(r"\s+[|\-–—:]\s+", title)[0].strip()[:200] or None
            if company_name:
                claim = f"The company is called {company_name}."
                evidence.append(_evidence("company_name", claim, url, title))
            break

    # Business description: the sentence that best overlaps the target description.
    business_words = _content_words(criteria.target_business)
    best = max(
        ((len(business_words & _content_words(s)), url, s) for url, s in content_sentences),
        default=(0, None, None),
    )
    needed = max(1, math.ceil(0.4 * len(business_words)))
    business_match: CriterionStatus = "met" if best[0] >= needed else "unknown"
    description = best[2][:300] if best[2] else None
    if description and best[1]:
        evidence.append(_evidence("description", description, best[1], description))
        if business_match == "met":
            evidence.append(_evidence("business", description, best[1], description))

    # Industry: a sentence containing enough of the industry words.
    industry_words = _content_words(criteria.industry)
    industry, industry_match = None, "unknown"
    for url, sentence in sentences:
        hits = industry_words & _content_words(sentence)
        if industry_words and len(hits) >= max(1, math.ceil(0.5 * len(industry_words))):
            industry, industry_match = criteria.industry, "met"
            claim = f"The company works in {criteria.industry}."
            evidence.append(_evidence("industry", claim, url, sentence))
            break

    # Own locations and markets served are kept apart.
    locations: list[str] = []
    markets: list[str] = []
    for url, sentence in content_sentences:
        if own := _own_locations(sentence):
            locations.extend(p for p in own if p not in locations)
            claim = f"The company is located in {', '.join(own)}."
            evidence.append(_evidence("company_location", claim, url, sentence))
        if served := _markets(sentence):
            markets.extend(p for p in served if p not in markets)
            claim = f"The company serves customers in {', '.join(served)}."
            evidence.append(_evidence("market_served", claim, url, sentence))
    allowed = _allowed_places(criteria.geography)
    geography_match: CriterionStatus = "unknown"
    if locations:
        geography_match = "met" if any(_place_matches(p, allowed) for p in locations) else "not_met"

    # Exclusions: every content word of one exclusion item appears in one sentence.
    exclusion_triggered = False
    for item in re.split(r"[,;\n]|\bor\b", criteria.exclusions or ""):
        words = _content_words(item)
        if not words:
            continue
        for url, sentence in content_sentences:
            if words <= _content_words(sentence):
                exclusion_triggered = True
                evidence.append(_evidence("exclusion", sentence[:300], url, sentence))
                break

    statuses = (industry_match, geography_match, business_match)
    fit: FitLabel
    if "not_met" in statuses or exclusion_triggered:
        fit = "non_fit"
    elif all(s == "met" for s in statuses):
        fit = "fit"
    else:
        fit = "insufficient_information"

    exclusion_text = "triggered" if exclusion_triggered else "not triggered"
    rationale = (
        f"[mock heuristic] industry {industry_match}, geography {geography_match}, "
        f"business {business_match}, exclusion {exclusion_text}."
    )
    return Assessment(
        company_name=company_name,
        description=description,
        industry=industry,
        company_locations=locations[:20],
        markets_served=markets[:20],
        industry_match=industry_match,
        geography_match=geography_match,
        business_match=business_match,
        exclusion_triggered=exclusion_triggered,
        fit=fit,
        rationale=rationale,
        evidence=evidence[:30],
    )


def _judge(item: dict[str, Any]) -> dict[str, Any]:
    """Mock verifier: keyword rules standing in for a semantic check."""
    field = item.get("field")
    value = str(item.get("value") or "")
    claim = str(item.get("claim") or "")
    quotes = [str(q) for q in item.get("quotes") or []]
    joined = " ".join(quotes)

    def verdict(result: str, reason: str) -> dict[str, Any]:
        return {"id": item.get("id"), "verdict": result, "reason": f"[mock] {reason}"[:300]}

    if field == "company_location":
        own = [p for q in quotes for s in _sentences(q) for p in _own_locations(s)]
        if any(_same_place(value, p) for p in own):
            return verdict("supported", f"the quote states an own site in {value}")
        served = [p for q in quotes for s in _sentences(q) for p in _markets(s)]
        if any(_same_place(value, p) for p in served):
            return verdict("not_supported", f"serving customers in {value} is not a location")
        return verdict("not_supported", f"no statement that the company is located in {value}")
    if field == "market_served":
        served = [p for q in quotes for s in _sentences(q) for p in _markets(s)]
        if any(_same_place(value, p) for p in served):
            return verdict("supported", f"the quote says the company serves {value}")
        return verdict("not_supported", f"no statement about customers in {value}")
    if field == "company_name":
        found = value.lower() in joined.lower()
        return verdict(
            "supported" if found else "not_supported",
            "name in quote" if found else "name not in quote",
        )
    share = {"industry": 0.5, "description": 0.4}.get(str(field), 0.5)
    words = _content_words(value if field in ("industry", "description") else claim)
    hits = words & _content_words(joined)
    if words and len(hits) >= max(1, math.ceil(share * len(words))):
        return verdict("supported", f"{len(hits)} of {len(words)} key words found in the quotes")
    return verdict("not_supported", f"only {len(hits)} of {len(words)} key words found")


def _own_locations(sentence: str) -> list[str]:
    places: list[str] = []
    for match in _OWN_LOCATION_RE.finditer(sentence):
        before = sentence[max(0, match.start() - 30) : match.start()]
        # "Our customers are located in X" is about customers, not about the company.
        if _MARKET_WORD_RE.search(before) or _MARKET_WORD_RE.search(match.group(0)):
            continue
        places.extend(_split_places(match.group(1)))
    return places


def _markets(sentence: str) -> list[str]:
    return [p for match in _MARKET_RE.finditer(sentence) for p in _split_places(match.group(1))]


def _split_places(text: str) -> list[str]:
    return [p.strip() for p in re.split(r",\s*(?:and\s+)?|\s+and\s+", text) if p.strip()]


def _same_place(a: str, b: str) -> bool:
    return bool(a) and a.strip().lower() == b.strip().lower()


def _evidence(supports: str, claim: str, url: str, quote: str) -> EvidenceItem:
    return EvidenceItem(supports=supports, claim=claim[:500], url=url, quote=quote[:300])


def _sentences(text: str) -> list[str]:
    """Split into sentences that are exact substrings of ``text`` (needed for quotes)."""
    result: list[str] = []
    for line in text.splitlines():
        result.extend(part for part in _SENTENCE_END_RE.split(line) if part.strip())
    return result


def _stem(word: str) -> str:
    for suffix in ("ing", "ers", "er", "es", "s"):
        if word.endswith(suffix) and len(word) - len(suffix) >= 4:
            word = word[: -len(suffix)]
            break
    if word.endswith("e") and len(word) >= 5:
        word = word[:-1]
    return word


def _content_words(text: str) -> set[str]:
    return {_stem(w) for w in _WORD_RE.findall(text.lower()) if len(w) >= 3 and w not in _STOPWORDS}


def _allowed_places(geography: str) -> set[str]:
    wanted = geography.lower()
    allowed = {p.strip() for p in re.split(r"[,;/]|\band\b|\bor\b", wanted) if p.strip()}
    for region, members in _REGIONS.items():
        if re.search(rf"\b{re.escape(region)}", wanted):
            allowed |= members
    return allowed


def _place_matches(place: str, allowed: set[str]) -> bool:
    lowered = place.lower()
    return any(re.search(rf"\b{re.escape(name)}\b", lowered) for name in allowed)


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


def _tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _estimate_tokens(params: dict[str, Any]) -> int:
    """Rough size estimate (~4 characters per token). Mock only — never used for costs."""
    total = len(str(params.get("system", "")))
    for message in params.get("messages", []):
        for text, _ in _iter_blocks(_get(message, "content")):
            total += len(text or "")
    return max(1, total // 4)
