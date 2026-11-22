# Synthetic document fixtures

Every file in this directory is **synthetic**. No real customer
documents, no real PII. Generated for deterministic eval cases that
exercise the `extract_document_fields` tool.

## Mime type

These are `text/plain` fixtures. The `extract_document_fields` tool's
`text/plain` mode is the eval-only deterministic path — production
calls send images / PDFs as base64 data URLs to Gemini's multimodal
endpoint through the same code path.

Real-image / real-PDF extraction is exercised by running the agent
against a real document locally with `HELIXIS_LLM__GOOGLE_API_KEY`
set. Committing binary image / PDF fixtures was deferred to keep the
repo light and to avoid any risk of real-document contamination.

## Adding a fixture

1. Hand-write the document text in a new `.txt` file under this
   directory. Use fictional names, fictional addresses, fictional
   amounts. Never copy from a real document — even paraphrased.
2. Add a golden case under `src/helixis/evals/golden/NNN_*.json`
   pointing at the new fixture via `kwargs.file_path`.
3. Set `requires_env: ["HELIXIS_LLM__GOOGLE_API_KEY"]` on the case
   so CI without secrets SKIPs cleanly.

## Identity rule

No fixture text contains a Buildium-shaped ID (`Task #1234`,
`Tenant #5678`, etc.). The activity-log sanitizer would redact them
anyway, but keeping fixtures clean means a regression in the
sanitizer doesn't accidentally surface a synthetic ID in eval output.
