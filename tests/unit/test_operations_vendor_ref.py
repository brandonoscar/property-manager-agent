"""A work order's vendor is read only under its own system's key.

``vendor_ref_sql`` read Rentvine's key for Rentvine and Buildium's
``VendorId`` for every other system, so a Rent Manager or Propertyware payload
that happened to carry a ``VendorId`` would be attributed to that vendor. Now a
system that keeps no vendor key in the mirror reads NULL, and the key counts
as absent: its vendor is unknown (rule 14). Checked on the SQL these build;
the same expressions ran on a real Postgres 16 (one row per system, a
Rent Manager row carrying ``VendorId`` read as NULL).
"""

from __future__ import annotations

from helixis.db.repositories.operations import (
    WORK_ORDER_VENDOR_KEY,
    vendor_key_present_sql,
    vendor_ref_sql,
)
from helixis.pms import registry


def test_only_a_listed_system_reads_a_vendor_key() -> None:
    ref = vendor_ref_sql("w")
    present = vendor_key_present_sql("w")
    assert ref.endswith("ELSE NULL END")
    assert present.endswith("ELSE false END")
    for pms, key in WORK_ORDER_VENDOR_KEY.items():
        assert f"WHEN w.pms = '{pms}' THEN w.raw->>'{key}'" in ref
        assert f"WHEN w.pms = '{pms}' THEN w.raw ? '{key}'" in present


def test_the_listed_systems_are_the_ones_whose_key_was_measured() -> None:
    """A new system joins only with a key read off a live row (rule 8)."""
    assert set(WORK_ORDER_VENDOR_KEY) == {registry.BUILDIUM, registry.RENTVINE}
