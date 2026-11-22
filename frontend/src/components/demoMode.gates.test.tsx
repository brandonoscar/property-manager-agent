/**
 * The two things a demo build has to get right, and neither is cosmetic.
 *
 * A demo build has NO backend by design — every page seam in `crm.ts`,
 * `inbox.ts`, `properties.ts` and `activity.ts` returns fixtures. Two
 * components did not know that, and each one broke the demo in a way that
 * looked like a broken product rather than a missing branch:
 *
 *   1. `LeasingComplianceGate` probes the backend for A2P approval, fails
 *      closed (correctly, for production) and shows a setup checklist. So the
 *      Leasing section — the one part of the app that is entirely fixtures,
 *      right down to reply and call — was the one part a demo could not show.
 *   2. `ServiceBanner` reads the health probe, which fails forever, and puts
 *      "Occupella can't reach its server right now" across the top of every
 *      screen. In a build with no server that is a FALSE statement, not a
 *      degraded one.
 *
 * ⚠ Neither branch weakens anything in production. `demoMode` is build-gated
 * behind `VITE_DEMO_MODE` (`resolveDemoMode`), which production never sets, so
 * `?demo=1` on a prod bundle cannot reach either path. And the gate was never
 * the enforcement — the backend refuses the send in `POST /crm/leads/{id}/reply`
 * via `outbound_channel_block`, untouched here. The panel is the explanation.
 *
 * ⚠ Almost every other suite mocks `demoMode` false and therefore exercises
 * the live path — which is right, and is why the demo branch had no coverage
 * at all until this file existed. The one other file that runs with it true is
 * `tabOrder.test.tsx`, which needs a signed-in app with no backend in order to
 * render `App` at all.
 */

import { render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('../services/auth', async () => {
  const actual = await vi.importActual<typeof import('../services/auth')>('../services/auth');
  return { ...actual, demoMode: true };
});

// The health probe reports DOWN, which is the honest answer in a demo build:
// there is no server. The banner still must not say so.
//
// ⚠ Every field is stated, including the ones this file does not care about.
// A partial fixture leaves the new `offline` flag undefined, which reads as
// "has a network" by accident — so the banner would be suppressed for a
// reason nobody chose, and the test would pass without testing anything.
vi.mock('../services/serviceHealth', () => ({
  getServiceHealth: () => ({ down: true, offline: false, failures: 2 }),
  onServiceHealthChange: () => () => {},
}));

import { LeasingComplianceGate } from './LeasingComplianceGate';
import { ServiceBanner } from './ServiceBanner';

describe('demo mode', () => {
  it('renders the Leasing pipeline instead of the setup checklist', () => {
    render(
      <LeasingComplianceGate>
        <div data-testid="pipeline">the lead pipeline</div>
      </LeasingComplianceGate>,
    );
    expect(screen.getByTestId('pipeline')).toBeTruthy();
  });

  it('never claims the server is unreachable, even when the probe says it is', () => {
    // ⚠ The mocked probe returns down:true. Asserting on a probe that said
    // "up" would pass against the unfixed component and prove nothing — the
    // whole bug is what this component does with a FAILING probe.
    const { container } = render(<ServiceBanner />);
    expect(container.querySelector('[data-testid="service-banner"]')).toBeNull();
  });
});
