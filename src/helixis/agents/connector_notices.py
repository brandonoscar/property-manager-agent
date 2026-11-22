"""The fourth state a connector specialist can be in: the integration itself.

The four Composio specialists (Gmail / Outlook / Calendar / Drive) each split
three ways on boot — tools present, connection lapsed, never connected — and
each of those is a fact about THE CUSTOMER'S account. There is a fourth state
that is a fact about the DEPLOYMENT: Composio is not configured here at all, so
every specialist boots with zero tools no matter what the customer has set up.

``create_orchestrator`` has logged that state loudly since it was written, and
the log line's own comment says why:

    "Loud-by-design: without this, an unset COMPOSIO__API_KEY in Render looked
    identical to 'Calendar not connected' on the frontend. Now the cause shows
    up in logs immediately."

The cause showed up in the LOGS. It never reached the person reading the reply,
who was told — in those words — *"Gmail isn't connected yet. Open Settings →
Connectors → Gmail and click Connect to grant access, then ask me again."* Their
Gmail may be connected and perfectly healthy; the blocker is an operator
credential they have no access to. They follow the instruction, the Connectors
page renders every Google row as disconnected (its own fallback log says
"rendering all as disconnected"), they click Connect, and the route answers 503.
Three surfaces telling one false story, and the one act that would fix it is the
one nothing suggests.

⚠ WHEN THIS ACTUALLY FIRES, stated precisely rather than overclaimed.
``ComposioClient.available`` is ``self._initialized and self._client is not
None`` — computed in ``__init__`` and never re-probed. So it is false for a
DEPLOYMENT that is missing ``HELIXIS_COMPOSIO__API_KEY``, missing the
``[connectors]`` extra, or whose client construction raised. It does NOT go
false when Composio's API has an outage mid-life; that surfaces later as tool
calls failing, which is a different path. The live population is therefore a
fresh white-label install and any environment where the key was never set —
gotcha 72/73's family, what breaks when the deployment is not the founder's own.

⚠ THE NOTICE GIVES NO INSTRUCTION THE READER CAN CARRY OUT, and that is the
point rather than an omission. "Connect it" and "reconnect it" are both acts
that cannot change this state, and an instruction that cannot work is worse than
none — the customer does it, nothing improves, and the product reads as broken
(gotchas 102/106). It says what is true: this is a setup problem on our side,
their own account is not the cause, and it needs an administrator.

⚠ ONE HOME, four callers. The wording is identical in substance for all four
specialists and only the product name differs, so a per-module copy is four
strings that drift — gotcha 73's ``"approved"`` typed out in three modules, and
the ``buildium/money.py::bill_amount`` precedent. The existing
NOT_CONNECTED/RECONNECT notices ARE per-module copies; they are left alone
deliberately (their wording genuinely differs per product, and rewriting four
live prompts is not this change).
"""

from __future__ import annotations

from typing import Final

#: Products this notice is written for, as the specialist calls them. Used by
#: the corpus guard to check every connector specialist takes the flag — a
#: fifth connector added without it is silently exempt, which is the shape this
#: whole module exists to close (gotchas 62/66/69/103).
CONNECTOR_PRODUCTS: Final[tuple[str, ...]] = ("Gmail", "Outlook", "Google Calendar", "Google Drive")


def integration_unavailable_notice(product: str) -> str:
    """The prompt block for "this deployment cannot reach Composio at all".

    ``product`` is the customer-facing name (``"Gmail"``, ``"Google Drive"``).
    """
    return f"""

## ⚠️ INTEGRATION UNAVAILABLE ON THIS DEPLOYMENT

{product} access is not available right now, and the cause is on OUR side —
this deployment is not configured to reach the {product} integration. You have
ZERO {product} tools. Do NOT pretend you can read, search, draft or send.

⚠ This is NOT a statement about the user's account. Their {product} may be
connected and perfectly healthy. Do NOT tell them to connect it, do NOT tell
them to reconnect it, and do NOT send them to Settings → Connectors — none of
those can change this, and asking them to try would waste their time and make
the product look broken.

When the user asks anything {product}-related, reply with exactly this shape
(one short paragraph, no list, no questions):

  "{product} isn't available on this account right now — that's a setup
  problem on our side, not something wrong with your connection. It needs an
  administrator to sort out; nothing for you to change. Everything else still
  works in the meantime."
"""
