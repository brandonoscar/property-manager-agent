/**
 * Members get connectors; only admins get Buildium.
 *
 * ⚠ THIS IS A REGRESSION I SHIPPED (gotcha 85). Asked to make the Buildium
 * connection admin-only, I put `minRole: "admin"` on the whole Connectors
 * SECTION — so a member lost Gmail, Calendar and Drive too. Those are per-USER
 * OAuth grants: a member connecting their own mailbox is what a member account
 * is for, and there is nothing of the company's to leak in doing it. Buildium
 * is different in kind — the company's books, its API credentials, its webhook
 * secret.
 *
 * ⚠ And this filter is the EXPLANATION, not the enforcement. The admin floor
 * on GET /buildium/credentials and /webhook-secret is what actually holds
 * (test_buildium_connection_routes_are_admin_only.py); a UI gate cannot bind
 * an API, and shipping only the panel would be theatre (gotcha 67).
 */
import { describe, expect, it } from "vitest";
import type { Connector } from "./connectors";
import { visibleConnectorRows } from "./connectors";

const row = (id: string): Connector =>
  ({
    id,
    name: id,
    category: "property",
    description: "",
    capabilities: ["api"],
  }) as unknown as Connector;

const ROWS = [
  row("buildium"),
  row("rentvine"),
  row("gmail"),
  row("googlecalendar"),
  row("googledrive"),
];

describe("visibleConnectorRows", () => {
  it("gives an admin everything, both PMS rows included", () => {
    expect(visibleConnectorRows(ROWS, "admin").map((c) => c.id)).toEqual([
      "buildium",
      "rentvine",
      "gmail",
      "googlecalendar",
      "googledrive",
    ]);
  });

  it.each(["manager", "member"])(
    "keeps the per-user OAuth connectors for a %s and hides only the PMS rows",
    (role) => {
      const ids = visibleConnectorRows(ROWS, role).map((c) => c.id);
      expect(ids).not.toContain("buildium");
      // Rentvine is the same kind of row: the company's books and its keys,
      // behind the same admin floor (api/routes/rentvine.py::_require_admin).
      expect(ids).not.toContain("rentvine");
      // The half the over-fix removed. If this list ever empties, a member has
      // an account they cannot connect anything to.
      expect(ids).toEqual(["gmail", "googlecalendar", "googledrive"]);
    },
  );

  it("hides Buildium while the role probe is still in flight", () => {
    // companyRole is null until the probe resolves. A row that renders and
    // then vanishes is the same leak with worse timing.
    expect(visibleConnectorRows(ROWS, null).map((c) => c.id)).not.toContain("buildium");
    expect(visibleConnectorRows(ROWS, null).map((c) => c.id)).not.toContain("rentvine");
  });

  it("does not hide Buildium from a role that merely starts with 'admin'", () => {
    // Strict equality, not a prefix or a truthiness check — "administrator"
    // is not a role this product issues, and a loose check is how a future
    // role string quietly becomes an admin.
    expect(visibleConnectorRows(ROWS, "administrator").map((c) => c.id)).not.toContain("buildium");
  });
});
