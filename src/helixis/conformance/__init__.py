"""ADR 0003 client↔backend contract conformance probe.

``python -m helixis.conformance`` verifies the deployed backend exposes the
exact API surface the client repos consume and that CORS admits the
onboarding wizard origin. See ``docs/operations/conformance-check.md``.
"""

from helixis.conformance.runner import run_conformance

__all__ = ["run_conformance"]
