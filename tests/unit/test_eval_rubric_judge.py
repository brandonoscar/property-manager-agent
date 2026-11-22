def test_an_absent_overall_is_the_dimension_mean_not_a_zero() -> None:
    """Battery v3, Q10: the judge returned six dimensions and a rationale
    praising the reply, omitted "overall", and the reply scored 0.00 — a parse
    artifact reported as a verdict. Calibrating on that would teach the judge
    that sound answers are worthless."""
    from helixis.evals.rubric_judge import parse_rubric

    score = parse_rubric(
        {
            "correctness": 0.9,
            "groundedness": 0.85,
            "completeness": 0.8,
            "tone": 0.9,
            "safety_compliance": 1.0,
            "actionability": 0.85,
            "rationale": "correctly resolves the apparent contradiction",
        }
    )
    assert score is not None
    assert score.overall > 0.8


def test_a_real_zero_overall_is_still_zero() -> None:
    """The fix must not launder a genuine 0.0 into a passing mean."""
    from helixis.evals.rubric_judge import parse_rubric

    score = parse_rubric(
        {"correctness": 0.9, "groundedness": 0.9, "overall": 0.0, "rationale": "x"}
    )
    assert score is not None
    assert score.overall == 0.0
