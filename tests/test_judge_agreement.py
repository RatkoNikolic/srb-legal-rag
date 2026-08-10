"""Krippendorff's α (nominal) — the judge–judge agreement coefficient."""

from kl_core.evals import krippendorff_alpha


def test_perfect_agreement_is_one():
    units = [["correct", "correct"], ["incorrect", "incorrect"],
             ["partially_correct", "partially_correct"], ["correct", "correct"]]
    assert krippendorff_alpha(units) == 1.0


def test_systematic_disagreement_is_negative():
    # two coders flip every label → worse than chance
    units = [["correct", "incorrect"], ["incorrect", "correct"]] * 3
    alpha = krippendorff_alpha(units)
    assert alpha is not None and alpha < 0


def test_partial_agreement_between_zero_and_one():
    # balanced marginals + one disagreement → clearly above chance but below 1
    units = ([["correct", "correct"]] * 5
             + [["incorrect", "incorrect"]] * 5
             + [["correct", "incorrect"]])
    alpha = krippendorff_alpha(units)
    assert alpha is not None and 0.0 < alpha < 1.0


def test_undefined_cases_return_none():
    assert krippendorff_alpha([]) is None
    assert krippendorff_alpha([["correct"], ["incorrect"]]) is None  # no paired ratings
    # no disagreement is even possible (single category everywhere) → undefined
    assert krippendorff_alpha([["correct", "correct"], ["correct", "correct"]]) is None


def test_missing_ratings_are_skipped():
    units = [["correct", None], ["correct", "correct"], ["incorrect", "incorrect"]]
    # the first unit has only one rating → ignored; the rest agree perfectly
    assert krippendorff_alpha(units) == 1.0
