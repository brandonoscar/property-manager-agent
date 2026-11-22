"""Privacy primitives — per-subject erasure (the right an individual holds).

Company-level erasure lives elsewhere: ``buildium/purge.py`` (ToS §18, on
disconnect) and ``memory.manager.delete_company_memory``. This package is for
the narrower, harder case — one person, across a store that is partly ours and
partly a cache of somebody else's system of record.
"""

from helixis.privacy.erasure import SUBJECT_TYPES, ErasureReport, Subject, erase_subject

__all__ = ["ErasureReport", "Subject", "SUBJECT_TYPES", "erase_subject"]
