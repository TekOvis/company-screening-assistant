"""Evaluation metrics, written out by hand so every formula is easy to read and test.

A case that ends without a label (an error such as invalid output or a budget stop) is
always counted as a wrong prediction: it lowers accuracy and the recall of the expected
label, but it is not a false positive for any label.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

LABELS: tuple[str, ...] = ("fit", "non_fit", "insufficient_information")
NO_LABEL = "no_label"


def confusion_matrix(
    expected: Sequence[str], predicted: Sequence[str | None]
) -> dict[str, dict[str, int]]:
    """Rows: expected label. Columns: predicted label, plus NO_LABEL for errors."""
    columns = (*LABELS, NO_LABEL)
    matrix = {label: dict.fromkeys(columns, 0) for label in LABELS}
    for exp, pred in zip(expected, predicted, strict=True):
        matrix[exp][pred if pred in LABELS else NO_LABEL] += 1
    return matrix


def accuracy(expected: Sequence[str], predicted: Sequence[str | None]) -> float:
    if not expected:
        return 0.0
    hits = sum(exp == pred for exp, pred in zip(expected, predicted, strict=True))
    return hits / len(expected)


def per_class(
    expected: Sequence[str], predicted: Sequence[str | None]
) -> dict[str, dict[str, float]]:
    """Precision, recall, F1 and support for each label."""
    pairs = list(zip(expected, predicted, strict=True))
    stats: dict[str, dict[str, float]] = {}
    for label in LABELS:
        tp = sum(exp == label and pred == label for exp, pred in pairs)
        fp = sum(exp != label and pred == label for exp, pred in pairs)
        fn = sum(exp == label and pred != label for exp, pred in pairs)
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
        stats[label] = {"precision": precision, "recall": recall, "f1": f1, "support": tp + fn}
    return stats


def macro_f1(expected: Sequence[str], predicted: Sequence[str | None]) -> float:
    """Unweighted mean F1 over the labels that occur in the expected or predicted labels."""
    stats = per_class(expected, predicted)
    used = [label for label in LABELS if label in expected or label in predicted]
    return sum(stats[label]["f1"] for label in used) / len(used) if used else 0.0


# --- Facts -----------------------------------------------------------------------------


def normalize_place(value: str) -> str:
    return " ".join(re.sub(r"[^\w\s]", " ", value.casefold()).split())


def _atomic_places(values: Sequence[str]) -> list[str]:
    """'Dortmund, Germany' counts as two places."""
    return [p for value in values for part in value.split(",") if (p := normalize_place(part))]


def _same(a: str, b: str) -> bool:
    # Containment lets "Freiburg" match "Freiburg im Breisgau".
    return bool(a) and bool(b) and (a == b or a in b or b in a)


def locations_correct(
    expected_groups: Sequence[Sequence[str]], forbidden: Sequence[str], predicted: Sequence[str]
) -> bool:
    """Every expected place found, nothing extra, no trap place; empty when none is stated."""
    places = _atomic_places(predicted)
    if invented_location(expected_groups, forbidden, predicted):
        return False
    groups = [[normalize_place(v) for v in group] for group in expected_groups]
    return all(any(_same(alt, p) for alt in group for p in places) for group in groups)


def invented_location(
    expected_groups: Sequence[Sequence[str]], forbidden: Sequence[str], predicted: Sequence[str]
) -> bool:
    """True if a reported own location is a trap place or is not stated in the text."""
    places = _atomic_places(predicted)
    traps = [normalize_place(f) for f in forbidden]
    if any(_same(trap, p) for trap in traps for p in places):
        return True
    alternatives = [normalize_place(v) for group in expected_groups for v in group]
    return any(not any(_same(alt, p) for alt in alternatives) for p in places)


def name_correct(expected_names: Sequence[str], predicted: str | None) -> bool | None:
    """None when the case does not define an expected name."""
    if not expected_names:
        return None
    if not predicted:
        return False
    value = normalize_place(predicted)
    return any(_same(normalize_place(name), value) for name in expected_names)


def markets_found(expected_groups: Sequence[Sequence[str]], predicted: Sequence[str]) -> int:
    places = _atomic_places(predicted)
    return sum(
        any(_same(normalize_place(alt), p) for alt in group for p in places)
        for group in expected_groups
    )
