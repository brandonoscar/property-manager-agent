"""Company knowledge MD (ADR 0006) — a short, standing per-company profile.

Learned in the background from what Helixis already sees (mirrored Buildium data,
webhook activity, uploaded documents) and refreshed at most once per day. This is
deliberately SEPARATE from Mem0 memory: one curated markdown doc, not vector
facts. See ``docs/adr/0006-company-knowledge-md.md``.

Public surface:
- ``gather_company_facts`` / ``build_profile_markdown`` — the source rollups + the
  one-LLM-call distiller (in :mod:`generator`).
- ``generate_for_company`` — generate + persist one company's profile (idempotent
  per day).
- ``run_company_knowledge_cycle`` — the daily tick the worker calls: iterate
  companies and generate for any not yet done today.
"""

from helixis.company_knowledge.generator import (
    generate_for_company,
    run_company_knowledge_cycle,
)

__all__ = ["generate_for_company", "run_company_knowledge_cycle"]
