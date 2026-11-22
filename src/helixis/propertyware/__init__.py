"""The Propertyware connector (design/propertyware-connector.md).

Built on the shared toolkit in ``helixis.pms``. Slice 1 is the read side: a
full read of each list into the existing mirror tables (``sync.sync_company``),
through a client that can call only the documented, non-beta,
non-payment reads in an index generated from Propertyware's official spec.
No writes yet. ``design/propertyware-terms.md`` binds everything here; read
this package's ``CLAUDE.md`` first.

Nothing is imported here: ``pms.registry`` finds ``sync.SYNC_STATE_STAGE``,
``sync.DERIVED_TABLES``, ``sync.RUN_NOTE`` and ``sync.sync_company`` lazily.
"""
