"""The Supabase URL a test's settings carry and the issuer its hand-minted tokens name.

`helixis.api.auth._decode_jwt` refuses a token whose ``iss`` is not this project's issuer,
``{supabase.url}/auth/v1`` (the form GoTrue writes, measured on helixis-test's edge logs on
2026-11-11), and refuses every token while ``supabase.url`` is unset. So a test that mints a
token puts ``TEST_SUPABASE_URL`` in its settings and ``TEST_ISSUER`` in its token.

``TEST_ISSUER`` is written out rather than derived with ``auth.expected_issuer``: a test that
asked the code under test for the expected value would agree with it whatever form it took.
"""

from __future__ import annotations

TEST_SUPABASE_URL = "https://test.supabase.co"
TEST_ISSUER = "https://test.supabase.co/auth/v1"
