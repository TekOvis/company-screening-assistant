"""Metric formulas checked against small hand-computed examples."""

import pytest

from screener import metrics
from screener.pricing import Tariff, cost_usd
from screener.schemas import Usage

EXPECTED = ["fit", "fit", "non_fit", "insufficient_information", "non_fit"]
PREDICTED = ["fit", None, "non_fit", "non_fit", "non_fit"]  # None = the run ended in an error


def test_accuracy_counts_errors_as_wrong():
    assert metrics.accuracy(EXPECTED, PREDICTED) == pytest.approx(3 / 5)


def test_confusion_matrix_has_a_column_for_errors():
    matrix = metrics.confusion_matrix(EXPECTED, PREDICTED)
    assert matrix["fit"] == {"fit": 1, "non_fit": 0, "insufficient_information": 0, "no_label": 1}
    assert matrix["non_fit"]["non_fit"] == 2
    assert matrix["insufficient_information"]["non_fit"] == 1


def test_per_class_and_macro_f1_match_hand_computation():
    stats = metrics.per_class(EXPECTED, PREDICTED)
    # fit: TP 1, FP 0, FN 1 -> P 1.0, R 0.5, F1 2/3
    assert stats["fit"]["precision"] == 1.0 and stats["fit"]["recall"] == 0.5
    assert stats["fit"]["f1"] == pytest.approx(2 / 3)
    # non_fit: TP 2, FP 1, FN 0 -> P 2/3, R 1.0, F1 0.8
    assert stats["non_fit"]["precision"] == pytest.approx(2 / 3)
    assert stats["non_fit"]["f1"] == pytest.approx(0.8)
    # insufficient_information: TP 0, FN 1 -> F1 0
    assert stats["insufficient_information"]["f1"] == 0.0
    assert stats["insufficient_information"]["support"] == 1
    assert metrics.macro_f1(EXPECTED, PREDICTED) == pytest.approx((2 / 3 + 0.8 + 0.0) / 3)


def test_perfect_predictions():
    assert metrics.accuracy(EXPECTED, EXPECTED) == 1.0
    assert metrics.macro_f1(EXPECTED, EXPECTED) == 1.0


@pytest.mark.parametrize(
    ("predicted", "correct", "invented"),
    [
        (["Dortmund, Germany"], True, False),
        (["Dortmund", "Germany"], True, False),
        (["Dortmund"], False, False),  # Germany missing
        (["Dortmund", "Germany", "Europe"], False, True),  # an extra, unstated place
        ([], False, False),
    ],
)
def test_location_checks(predicted, correct, invented):
    expected = [["Dortmund"], ["Germany"]]
    assert metrics.locations_correct(expected, [], predicted) is correct
    assert metrics.invented_location(expected, [], predicted) is invented


def test_trap_places_and_unstated_locations_count_as_invented():
    assert metrics.invented_location([["Valencia"], ["Spain"]], ["Germany"], ["Germany"])
    assert metrics.invented_location([], [], ["Poland"])
    assert metrics.locations_correct([], [], [])  # nothing stated, nothing reported
    assert metrics.locations_correct([["Freiburg"]], [], ["Freiburg im Breisgau"])


def test_name_and_market_checks():
    assert metrics.name_correct(["Brightwater Valve Works"], "Brightwater Valve Works GmbH")
    assert metrics.name_correct(["Kestrel Flow Control"], None) is False
    assert metrics.name_correct([], "Anything") is None
    assert metrics.markets_found([["Germany"], ["Austria"]], ["Germany", "France"]) == 1


def test_cost_uses_every_token_type():
    usage = Usage(
        input_tokens=1_000_000,
        output_tokens=100_000,
        cache_creation_input_tokens=10_000,
        cache_read_input_tokens=200_000,
    )
    tariff = Tariff(input=2.0, output=10.0, cache_write=2.5, cache_read=0.1)
    assert cost_usd(usage, tariff) == pytest.approx(2.0 + 1.0 + 0.025 + 0.02)


# --- Facts before and after the evidence checks ---------------------------------------

MODEL_ANSWER = {
    "company_name": "Nordwerk Armaturen",
    "description": "Makes ball valves.",
    "industry": None,
    "company_locations": ["Kassel", "Germany", "kassel "],  # a repeat counts once
    "markets_served": ["Japan"],
}
FINAL_ANSWER = {
    "company_name": "Nordwerk Armaturen",
    "description": None,
    "industry": None,
    "company_locations": ["Kassel", "Germany"],
    "markets_served": [],
}


def _item(supports, quote, check="supported"):
    return {"supports": supports, "quote": quote, "check": check}


def test_fact_list_counts_each_value_once():
    assert metrics.fact_list(MODEL_ANSWER) == [
        ("company_name", "Nordwerk Armaturen"),
        ("description", "Makes ball valves."),
        ("company_locations", "Kassel"),
        ("company_locations", "Germany"),
        ("markets_served", "Japan"),
    ]


def test_unconfirmed_facts_are_those_missing_after_the_checks():
    assert metrics.unconfirmed_facts(MODEL_ANSWER, FINAL_ANSWER) == [
        ("description", "Makes ball valves."),
        ("markets_served", "Japan"),
    ]


def test_final_facts_need_a_supported_item_of_a_matching_category():
    evidence = [
        _item("company_name", "Welcome to Nordwerk Armaturen."),
        _item("company_location", "Our plant is in Kassel, Germany."),
    ]
    assert metrics.facts_without_support(FINAL_ANSWER, evidence) == []
    # Not supported, wrong category, or the value missing from the quote: not backed.
    weak = [
        _item("company_name", "Welcome to Nordwerk Armaturen.", check="unclear"),
        _item("market_served", "Our plant is in Kassel, Germany."),
        _item("company_location", "Our plant is in Kassel."),
    ]
    assert metrics.facts_without_support(FINAL_ANSWER, weak) == [
        ("company_name", "Nordwerk Armaturen"),
        ("company_locations", "Germany"),
    ]
    # The description may rest on a supported business quote.
    described = {"description": "Makes ball valves."}
    assert metrics.facts_without_support(described, [_item("business", "We make valves.")]) == []
