"""Rent Manager connector: the read side (slice 1).

Design: ``design/rentmanager-connector.md``. Binding rules:
``design/rentmanager-terms.md``. Shared code finds ``sync`` lazily, through
``pms.registry.connector_attr``; nothing imports this package at load time.
"""
