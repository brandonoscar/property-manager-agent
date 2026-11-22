-- Give work orders a real link to their backing task, and backfill the
-- property that link resolves to.
--
-- GET /workorders carries NO property of its own — neither a nested Property
-- object nor a usable flat PropertyId; both are null on every production row
-- (live-verified 2026-08-24 over helixis_work_orders.raw, which preserves
-- undeclared keys, so an absent nested object is real and not a model artifact).
-- So property_id landed NULL on all 60 mirrored work orders while title, status
-- and priority filled in fine, and every surface that groups maintenance by
-- building — cockpit, operations, morning brief, turnovers, the reconciliation
-- analyst — saw the entire book as property-less.
--
-- The only portfolio reference a work order carries is WorkOrder.Task.Id. Its
-- property is whatever the backing task's is, and the task mirror already has
-- that right (90 of 90 rows). Promoting the task id from a JSON path to a real
-- indexed column makes the lookup work in both directions without any query
-- needing to know the payload's shape.
--
-- Verified before writing this, on the live account:
--   60 work orders, 60 carry a Task.Id, 60 resolve in helixis_tasks,
--   60 would gain a property.
--
-- Unit is deliberately NOT backfilled. It already flows straight from the
-- payload when Buildium sends it (WorkOrder.effective_unit_id: top-level
-- UnitId, else Task.UnitId) and other companies' rows do carry it. It is
-- absent on THIS account specifically — 0 of 90 mirrored tasks carry a UnitId
-- and the key is not even present in the raw body — so there is nothing to
-- resolve it from and nothing to repair. NULL keeps meaning "unknown", which
-- here is the truth.

ALTER TABLE helixis_work_orders
  ADD COLUMN IF NOT EXISTS task_id bigint;

COMMENT ON COLUMN helixis_work_orders.task_id IS
  'Buildium id of the backing to-do task (WorkOrder.Task.Id). The work order''s '
  'only link to a property — GET /workorders sends no property of its own.';

CREATE INDEX IF NOT EXISTS helixis_work_orders_task_idx
  ON helixis_work_orders (company_id, task_id);

-- Backfill the link from the raw payload already stored.
UPDATE helixis_work_orders
   SET task_id    = (raw -> 'Task' ->> 'Id')::bigint,
       updated_at = now()
 WHERE task_id IS NULL
   AND raw -> 'Task' ->> 'Id' ~ '^[0-9]+$';

-- Backfill the property the link resolves to. Only fills NULLs: a work order
-- that somehow already has a property keeps it, and one whose task is not
-- mirrored (or whose task has no property) stays NULL, because NULL means
-- "unknown" and guessing would be worse than admitting it.
UPDATE helixis_work_orders w
   SET property_id = t.property_id,
       updated_at  = now()
  FROM helixis_tasks t
 WHERE t.company_id  = w.company_id
   AND t.buildium_id = w.task_id
   AND w.property_id IS NULL
   AND t.property_id IS NOT NULL;
