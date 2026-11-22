"""Thin repositories over the Helixis-side Postgres tables.

Each module here owns a single table family. Keep them free of business
logic — they should just translate domain objects to SQL.
"""

from helixis.db.repositories.activity import ActivityEntry, write_activity
from helixis.db.repositories.entities import upsert_lease, upsert_work_order
from helixis.db.repositories.leads import (
    LeadMetrics,
    LeadRow,
    MessageRow,
    clear_lead_assist,
    count_leads_by_stage,
    find_lead_by_email,
    find_lead_by_phone,
    get_lead,
    insert_lead,
    insert_message,
    lead_metrics,
    list_leads,
    list_messages,
    recent_leads_by_stage,
    set_lead_draft,
    update_stage,
)

__all__ = [
    "ActivityEntry",
    "LeadMetrics",
    "LeadRow",
    "MessageRow",
    "count_leads_by_stage",
    "find_lead_by_email",
    "find_lead_by_phone",
    "get_lead",
    "insert_lead",
    "clear_lead_assist",
    "insert_message",
    "lead_metrics",
    "list_leads",
    "list_messages",
    "recent_leads_by_stage",
    "set_lead_draft",
    "update_stage",
    "upsert_lease",
    "upsert_work_order",
    "write_activity",
]
