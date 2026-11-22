"""Agent code-execution — the "compute, never act" capability.

The design invariant (see the ADR): the sandbox has ZERO authority. It holds
no credentials, no database handle, no network, and no write path. It reads
data passed IN, computes, and returns results/artifacts OUT. Any real write
happens LATER, through the existing confirm-gated tools — never from here. So
a code run can, at worst, produce a wrong answer the PM reviews; it can never
move money or delete records, because it was never given the power to.

Ships dark (``settings.codeexec.enabled``). Increment 1 is the substrate
(``executor.run_code``) only — no agent tool is wired yet.
"""

from helixis.codeexec.executor import CodeResult, run_code

__all__ = ["CodeResult", "run_code"]
