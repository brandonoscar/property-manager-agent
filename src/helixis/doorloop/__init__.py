"""The DoorLoop connector (design/doorloop-connector.md).

Built on the shared toolkit in ``helixis.pms``. Slice A0 is the connect
surface only: one company's API key, stored encrypted (``credentials``), and a
client that can make the one read the connection test needs (``client``).
There is no ``sync`` module yet, so every shared reader takes DoorLoop as
"fills nothing, says nothing" (``pms.registry.connector_attr``). The mapping
and the sync wait for DoorLoop's own OpenAPI file; read this package's
``CLAUDE.md`` first.
"""
