"""Supabase Storage helpers — service-key upload / sign / delete for private
buckets.

Server-side only. The service key bypasses storage-level RLS, so per-tenant
isolation is enforced by (a) the object path being scoped by ``company_id`` and
(b) the company-scoped API routes that call these. Modeled on
``agents.browser_tools._upload_screenshot_to_storage`` (the existing, working
Supabase Storage path), generalized to any bucket / content type.
"""

from __future__ import annotations

import logging

import httpx

from helixis.settings import Settings

logger = logging.getLogger(__name__)


class StorageError(Exception):
    """Raised when a Supabase Storage operation cannot complete (missing config
    or a non-2xx response). Routes translate this into a 502/503 for the user."""


def _base_and_key(settings: Settings) -> tuple[str, str]:
    base = (settings.supabase.url or "").rstrip("/")
    key = settings.supabase.service_key or ""
    if not base or not key:
        raise StorageError("Supabase Storage is not configured (URL or service key missing).")
    return base, key


async def upload_object(
    *,
    bucket: str,
    path: str,
    content: bytes,
    content_type: str,
    settings: Settings,
) -> None:
    """Upload ``content`` to ``{bucket}/{path}`` (upsert). Raises StorageError on
    any failure."""
    base, key = _base_and_key(settings)
    headers = {
        "Authorization": f"Bearer {key}",
        "apikey": key,
        "Content-Type": content_type,
        "x-upsert": "true",
    }
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.post(
                f"{base}/storage/v1/object/{bucket}/{path}", content=content, headers=headers
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise StorageError(f"Storage upload transport failure: {exc}") from exc
    if resp.status_code >= 400:
        raise StorageError(f"Storage upload failed (status {resp.status_code}).")


async def sign_object_url(
    *,
    bucket: str,
    path: str,
    expires_in: int,
    settings: Settings,
) -> str:
    """Return a time-limited signed URL for ``{bucket}/{path}``. Raises
    StorageError on failure."""
    base, key = _base_and_key(settings)
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.post(
                f"{base}/storage/v1/object/sign/{bucket}/{path}",
                json={"expiresIn": expires_in},
                headers=headers,
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise StorageError(f"Storage sign transport failure: {exc}") from exc
    if resp.status_code >= 400:
        raise StorageError(f"Storage sign failed (status {resp.status_code}).")
    data = resp.json()
    signed = data.get("signedURL") or data.get("signedUrl") or ""
    if not signed:
        raise StorageError("Storage sign response missing signedURL.")
    # The API returns a relative ``/object/sign/...?token=`` path; make it absolute.
    return signed if signed.startswith("http") else f"{base}/storage/v1{signed}"


async def download_object(*, bucket: str, path: str, settings: Settings) -> bytes:
    """Fetch ``{bucket}/{path}`` and return its bytes. Raises StorageError on
    any failure (including a missing object)."""
    base, key = _base_and_key(settings)
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
            resp = await client.get(f"{base}/storage/v1/object/{bucket}/{path}", headers=headers)
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise StorageError(f"Storage download transport failure: {exc}") from exc
    if resp.status_code >= 400:
        raise StorageError(f"Storage download failed (status {resp.status_code}).")
    return resp.content


#: One list call's page size. Supabase caps this server-side; the walker pages
#: with ``offset`` rather than asking for everything at once.
_LIST_PAGE = 100

#: Total objects one walk may return. A company with more than this has a
#: storage problem of its own, and an unbounded walk inside a delete path is
#: how a transient API shape turns into a hang.
_LIST_CEILING = 20_000


async def list_objects(*, bucket: str, prefix: str, settings: Settings) -> list[str]:
    """Every object path under ``prefix`` in ``bucket``, walked recursively.

    ⚠ **THE WIRE SHAPE HERE IS NOT LIVE-PROBED.** Every other call in this
    module is — upload / sign / download / delete are the same requests
    ``browser_tools`` has been making against real Supabase for months — but
    `supabase.co` is egress-blocked from the container this was written in
    (CLAUDE.md gotcha 5, re-confirmed: ``CONNECT … 403``), and gotcha 79 is the
    standing record of what guessing a path costs: an SDK class name read as a
    REST route 404'd the entire A2P chain for as long as it existed.

    So this is built to be **correct under either response shape rather than
    correct under the one I believe in**, which is the only honest option when
    the endpoint cannot be probed:

    * If ``list`` returns files flat, no entry looks like a folder, the
      recursion finds nothing to descend into, and the walk is one pass.
    * If ``list`` returns *folder placeholders* for nested paths — the
      documented behaviour, and the trap, since
      ``browser_screenshots/{company}/{session}/{step}.png`` is two levels
      deep — the walk descends and finds the files underneath.

    A naive single flat call would, under the second shape, return three
    folder names, delete nothing, and report a clean sweep. That is the
    200-but-lie class (gotchas 20/70/75) arriving inside an erasure proof,
    which is the worst place for it — hence the recursion, and hence the
    caller treating a raised error as UNVERIFIED rather than as empty.

    Raises ``StorageError`` on any failure, including a 404 on the endpoint
    itself: callers must not read "could not enumerate" as "nothing there".
    """
    base, key = _base_and_key(settings)
    headers = {"Authorization": f"Bearer {key}", "apikey": key, "Content-Type": "application/json"}
    found: list[str] = []
    # Prefixes still to walk. Seeded with the caller's, grown by folder entries.
    pending: list[str] = [prefix.strip("/")]
    seen_prefixes: set[str] = set()

    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0)) as client:
        while pending:
            current = pending.pop()
            if current in seen_prefixes:
                continue
            seen_prefixes.add(current)
            offset = 0
            while True:
                body = {"prefix": f"{current}/" if current else "", "limit": _LIST_PAGE}
                body["offset"] = offset
                try:
                    resp = await client.post(
                        f"{base}/storage/v1/object/list/{bucket}", json=body, headers=headers
                    )
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    raise StorageError(f"Storage list transport failure: {exc}") from exc
                if resp.status_code >= 400:
                    raise StorageError(f"Storage list failed (status {resp.status_code}).")
                try:
                    entries = resp.json()
                except ValueError as exc:
                    raise StorageError(f"Storage list returned non-JSON: {exc}") from exc
                if not isinstance(entries, list):
                    raise StorageError("Storage list returned an unexpected body shape.")
                if not entries:
                    break
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("name") or "").strip("/")
                    if not name:
                        continue
                    full = f"{current}/{name}" if current else name
                    # ⚠ A FOLDER placeholder carries no id and no metadata. Test
                    # for the ABSENCE of both rather than for a truthy id: an
                    # API that starts returning an empty-string id for files
                    # would otherwise reclassify every file as a folder and the
                    # walk would return nothing while still reporting success.
                    is_folder = entry.get("id") is None and entry.get("metadata") is None
                    if is_folder:
                        pending.append(full)
                    else:
                        found.append(full)
                        if len(found) >= _LIST_CEILING:
                            raise StorageError(
                                f"Storage list exceeded {_LIST_CEILING} objects under "
                                f"{bucket}/{prefix} — refusing to continue."
                            )
                if len(entries) < _LIST_PAGE:
                    break
                offset += _LIST_PAGE
    return found


async def delete_object(*, bucket: str, path: str, settings: Settings) -> None:
    """Delete ``{bucket}/{path}``. Best-effort: raises StorageError on transport
    failure, but a 404 (already gone) is treated as success."""
    base, key = _base_and_key(settings)
    headers = {"Authorization": f"Bearer {key}", "apikey": key}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as client:
            resp = await client.request(
                "DELETE", f"{base}/storage/v1/object/{bucket}/{path}", headers=headers
            )
    except (httpx.TimeoutException, httpx.TransportError) as exc:
        raise StorageError(f"Storage delete transport failure: {exc}") from exc
    if resp.status_code >= 400 and resp.status_code != 404:
        raise StorageError(f"Storage delete failed (status {resp.status_code}).")
