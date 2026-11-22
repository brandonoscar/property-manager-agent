-- Bill lines: backfill property_id from Lines[].AccountingEntity.Id.
--
-- WHY
-- Buildium sends ``PropertyId`` on every bill line as a KEY and on none of
-- them as a VALUE — live-verified 2026-08-27 across all 131 mirrored lines:
-- the key is present 131/131, populated 0/131, while
-- ``AccountingEntity.Id`` is populated 131/131. ``upsert_bill`` wrote the raw
-- ``PropertyId`` through, so ``helixis_bill_lines.property_id`` has been NULL
-- on 100% of rows since the table was created, and every property-scoped
-- payables read silently returned nothing.
--
-- The ingest is fixed (BillLine.resolved_property_id), which handles rows
-- from here on. This backfills what is already mirrored — the same shape as
-- 0073, which backfilled bill totals after the equivalent field-shape bug.
--
-- ⚠ Only ``AccountingEntityType = 'Rental'`` resolves to a property. A
-- ``Company`` line is overhead and belongs to no property; attributing it to
-- whatever property happens to share that id would invent a fact.
--
-- ⚠ Idempotent and non-destructive: only NULL property_id rows are touched,
-- so re-running cannot overwrite a value the API later starts sending.
UPDATE helixis_bill_lines bl
   SET property_id = (line.value -> 'AccountingEntity' ->> 'Id')::bigint
  FROM helixis_bills b,
       LATERAL jsonb_array_elements(COALESCE(b.raw -> 'Lines', '[]'::jsonb))
               WITH ORDINALITY AS line(value, ord)
 WHERE bl.company_id = b.company_id
   AND bl.bill_id    = b.buildium_id
   AND bl.line_index = line.ord - 1
   AND bl.property_id IS NULL
   AND line.value -> 'AccountingEntity' ->> 'Id' IS NOT NULL
   AND lower(COALESCE(line.value -> 'AccountingEntity' ->> 'AccountingEntityType', '')) = 'rental';
