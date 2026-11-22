"""Tests for the geocode-on-sync backfill orchestration (geo/backfill.py).

Mocks the geocoder + the repo seams — no live DB, no Maps API. Pins the
best-effort contract: no key ⇒ no-op; ungeocodable / addressless rows are
skipped; stored points count; a store failure doesn't abort the batch.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from helixis.geo import backfill
from helixis.geo.geocode import GeoPoint


def _settings(api_key: str = "", provider: str = "census") -> MagicMock:
    """⚠ The default is the SHIPPED state: census, keyless, and fully working.

    It used to be `api_key="k"` with the provider left as a bare MagicMock,
    which is truthy and matches nothing — so every test here silently asserted
    a state it never chose (gotcha 96's fake-pool trap, arriving as a
    fixture). Say which state the fixture means, always (gotchas 67/72/73).
    """
    settings = MagicMock()
    settings.maps.api_key = api_key
    settings.maps.provider = provider
    return settings


def _pending(*buildium_ids: int) -> list:
    from helixis.db.repositories.geo import UngeocodedProperty

    return [
        UngeocodedProperty(bid, f"{bid} Main St", "Austin", "TX", "78701") for bid in buildium_ids
    ]


@pytest.mark.asyncio
async def test_a_deployment_that_cannot_geocode_is_a_noop() -> None:
    """Right about the property, re-pointed off the mechanism.

    ⚠ This asserted `api_key=""` alone, which since the 2026-08-17 census
    migration describes the DEFAULT INSTALL — so it pinned the defect: the
    backfill returned 0 without looking, the geocode layer was dark on every
    keyless deployment, and nothing downstream (flood zone, Fair Market Rent,
    which state's law applies) had a point to key off. The real property is
    "a deployment that cannot geocode does not query", and only google/mapbox
    without a key are such a deployment.
    """
    pool = MagicMock()
    with patch.object(backfill.geo_repo, "list_ungeocoded", AsyncMock()) as lu:
        n = await backfill.geocode_company_properties(
            pool, settings=_settings(provider="google", api_key=""), company_id="co-1"
        )
    assert n == 0
    lu.assert_not_awaited()  # never even queries when it could not geocode


@pytest.mark.asyncio
async def test_the_keyless_census_default_does_the_work() -> None:
    """The other direction, and the one that was broken in production.

    A one-sided test passes against a predicate that refuses everything, so
    this is the half that proves the shipped configuration actually runs
    (gotchas 38/51: guard both directions).
    """
    pool = MagicMock()
    with (
        patch.object(
            backfill.geo_repo, "list_ungeocoded", AsyncMock(return_value=_pending(1))
        ) as lu,
        patch.object(backfill, "geocode", AsyncMock(return_value=GeoPoint(lat=1.0, lng=2.0))),
        patch.object(backfill.geo_repo, "set_property_point", AsyncMock()),
    ):
        n = await backfill.geocode_company_properties(pool, settings=_settings(), company_id="co-1")
    assert n == 1
    lu.assert_awaited_once()


@pytest.mark.asyncio
async def test_geocodes_and_stores_each_pending() -> None:
    pool = MagicMock()
    point = GeoPoint(lat=30.26, lng=-97.74)
    with (
        patch.object(backfill.geo_repo, "list_ungeocoded", AsyncMock(return_value=_pending(1, 2))),
        patch.object(backfill, "geocode", AsyncMock(return_value=point)),
        patch.object(backfill.geo_repo, "set_property_point", AsyncMock()) as store,
    ):
        n = await backfill.geocode_company_properties(pool, settings=_settings(), company_id="co-1")
    assert n == 2
    assert store.await_count == 2
    assert store.await_args.kwargs["lat"] == 30.26
    assert store.await_args.kwargs["lng"] == -97.74


@pytest.mark.asyncio
async def test_ungeocodable_rows_are_skipped_not_stored() -> None:
    pool = MagicMock()
    with (
        patch.object(backfill.geo_repo, "list_ungeocoded", AsyncMock(return_value=_pending(1, 2))),
        patch.object(backfill, "geocode", AsyncMock(return_value=None)),
        patch.object(backfill.geo_repo, "set_property_point", AsyncMock()) as store,
    ):
        n = await backfill.geocode_company_properties(pool, settings=_settings(), company_id="co-1")
    assert n == 0
    store.assert_not_awaited()


@pytest.mark.asyncio
async def test_missing_migration_degrades_to_zero() -> None:
    pool = MagicMock()
    with patch.object(
        backfill.geo_repo,
        "list_ungeocoded",
        AsyncMock(side_effect=Exception("column geog does not exist")),
    ):
        n = await backfill.geocode_company_properties(pool, settings=_settings(), company_id="co-1")
    assert n == 0


@pytest.mark.asyncio
async def test_store_failure_does_not_abort_batch() -> None:
    pool = MagicMock()
    point = GeoPoint(lat=1.0, lng=2.0)
    with (
        patch.object(backfill.geo_repo, "list_ungeocoded", AsyncMock(return_value=_pending(1, 2))),
        patch.object(backfill, "geocode", AsyncMock(return_value=point)),
        patch.object(
            backfill.geo_repo,
            "set_property_point",
            AsyncMock(side_effect=[Exception("boom"), None]),
        ),
    ):
        n = await backfill.geocode_company_properties(pool, settings=_settings(), company_id="co-1")
    # Second row still stored despite the first failing.
    assert n == 1
