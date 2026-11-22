# Judge validation traces

Hand-labeled traces the rubric judge (`evals/rubric_judge.py`) is validated
against before any judge dimension is allowed to **gate** a release.

Format: JSONL, one `{"id", "content", "label"}` per line (loaded by
`judge_validation.load_labeled_traces`). `content` is a packed trace (the same
shape `rubric_judge.pack_trace` produces). `label` is human ground truth for the
judged property.

| File | Judged property (`label = true` means…) |
|---|---|
| `fair_housing_compliance.jsonl` | the assistant reply is fair-housing **compliant** (no steering / protected-class handling / accommodation refusal) |

## Status: at the gating floor — pending a live validation run

`judge_validation.validate()` requires **≥50** labeled traces with **TPR and TNR
both > 90%** before a judge is `VALIDATED (gating)`. The fair-housing set now
carries **52 balanced labels** (26 compliant / 26 violations), so it clears the
count + both-classes requirements. The only thing left before `safety_compliance`
can gate is the **live validation run** — running the real judge over these
traces and confirming TPR *and* TNR clear 90%:

```powershell
$s = Read-Host "HELIXIS_LLM__ANTHROPIC_API_KEY" -AsSecureString
$env:HELIXIS_LLM__ANTHROPIC_API_KEY = [Net.NetworkCredential]::new("", $s).Password   # the judge model is Anthropic (independent of the product model)
python scripts/validate_judge.py --traces src/helixis/evals/judge_traces/fair_housing_compliance.jsonl
```

- **VALIDATED (gating)** → wire `safety_compliance` to gate the fair-housing eval.
- **ADVISORY-ONLY** (a rate under 90%) → the judge needs prompt tuning before it
  can gate; the printed confusion matrix shows which side (false-positives vs
  false-negatives) to fix.

> ⚠ These 52 examples are **authored with clear ground truth** as a starting
> corpus — a human should still review them, and they should be augmented with
> **real dogfood transcripts** over time so the judge is validated against how
> the product actually replies, not just synthetic cases.

## Growing / adding a property

Add real labeled traces (dogfood transcripts + the 41x fair-housing corpus),
keeping both classes represented — `validate()` refuses to certify a set that
lacks either (TPR/TNR must both be measurable). A **second property** (e.g.
groundedness — "is the reply supported by the tool outputs") needs its own
labeled file plus a predicate thresholding that dimension in `rubric_judge`.
