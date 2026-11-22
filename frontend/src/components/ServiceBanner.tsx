/**
 * "Occupella is briefly unreachable" — or "your connection is."
 *
 * A Render deploy takes the backend away for thirty to sixty seconds. Before
 * this, that showed up as three or four different error states at once — the
 * Inbox said one thing, the grid another, the composer a third — each a
 * local guess about a single global fact. One strip says it once.
 *
 * ⚠ **WHOSE outage it is changes the sentence.** `fetch` fails identically
 * whether our server is missing or the user's wifi is, so this strip used to
 * tell somebody on a dying hotel connection that Occupella could not reach
 * its server — a claim about our product, made on their network's evidence.
 * `serviceHealth.offline` is the browser's own report and it WINS: while the
 * device has no network we cannot know whether the backend is up, and the
 * honest sentence is the one the user can check by glancing at their wifi.
 *
 * ⚠ **A strip, not a takeover.** Everything already on screen is real: it was
 * fetched while the backend was up, and it is still worth reading. Replacing
 * the app with a full-page maintenance screen would throw that away and make
 * a sixty-second deploy look like an outage.
 *
 * ⚠ **The backend arm never appears on one failed request** — `serviceHealth`
 * requires consecutive failures, and any served response clears it. A banner
 * that fires on a blip is one people learn to ignore. The OFFLINE arm has no
 * such bar and does not need one: it is a reported fact, not an inference.
 */

import { useEffect, useRef, useState } from 'react';
import { toast } from 'sonner';

import { demoMode } from '../services/auth';
import {
  getServiceHealth,
  onServiceHealthChange,
  type ServiceHealth,
} from '../services/serviceHealth';

type Reachability = 'ok' | 'offline' | 'down';

/**
 * The ONE place the two facts are combined into a sentence.
 *
 * Offline outranks down for the reason in the header: a failure that never
 * left the device is not evidence about our server.
 */
function reachability(health: ServiceHealth): Reachability {
  if (health.offline) return 'offline';
  if (health.down) return 'down';
  return 'ok';
}

const MESSAGE: Record<Exclude<Reachability, 'ok'>, string> = {
  // Names the device, not the product. Somebody reading this can confirm it
  // in one glance and fix it themselves, which is the whole point of telling
  // them apart.
  offline: "You’re offline. What’s on screen is still accurate; new changes won’t save until your connection is back.",
  down: "Occupella can’t reach its server right now. What’s on screen is still accurate; new changes won’t save until it’s back.",
};

const RECOVERY: Record<Exclude<Reachability, 'ok'>, string> = {
  offline: "You’re back online — your changes will save again.",
  down: "Occupella’s server is back — your changes will save again.",
};

export function ServiceBanner() {
  // ⚠ A demo build has no backend BY DESIGN — every page seam returns
  // fixtures — so the health probe fails forever and this strip sits across
  // the top of every screen saying the product is broken. It is a FALSE
  // statement there, not a degraded one: nothing is unreachable, because
  // nothing was meant to be reached.
  //
  // Suppressed rather than "fixed" in the probe, because the probe is right:
  // the request genuinely fails. It is this banner's premise — that a failed
  // request means an outage — that does not hold in a build with no server.
  const [state, setState] = useState<Reachability>(() =>
    demoMode ? 'ok' : reachability(getServiceHealth()),
  );

  // ⚠ LOAD-BEARING NOW, and it was deliberately absent before. The old
  // version fired its toast on any healthy emission, because the bus emitted
  // only on a `down` transition — so arriving healthy WAS the recovery and
  // there was nothing to distinguish. With two causes there are two
  // recoveries and two different sentences, and only the previous state says
  // which one just ended. A test pins each.
  const previous = useRef<Reachability>('ok');

  useEffect(() => {
    // Read again here as well as in the initializer above. Listeners are
    // told about CHANGES only, so a banner that never reads the current
    // value stays silent through an outage that began before it mounted.
    //
    // ⚠ The two reads are belt-and-braces for the render→commit window: the
    // initializer runs during render, this runs after commit, and the health
    // can flip in between with no subscription attached yet. Removing EITHER
    // one alone changes nothing a test can observe (verified by mutation);
    // removing both fails. So this is recorded rather than pinned — a test
    // asserting a distinction that does not exist would only teach the next
    // reader that it does.
    if (demoMode) return;
    const initial = reachability(getServiceHealth());
    previous.current = initial;
    setState(initial);

    return onServiceHealthChange((health) => {
      const next = reachability(health);
      const was = previous.current;
      previous.current = next;

      // ⚠ Recovery has to be ANNOUNCED, not merely un-announced. Before this
      // the strip simply vanished, which is the same thing that happens when
      // you scroll it out of view or reload — so somebody who stopped working
      // because their changes would not save had no signal telling them to
      // start again. They find out by trying, which is the thing the banner
      // existed to save them from.
      if (next === 'ok' && was !== 'ok') {
        toast.success(RECOVERY[was], { duration: 5000 });
      }
      setState(next);
    });
  }, []);

  if (state === 'ok') return null;

  return (
    <div
      role="status"
      data-testid="service-banner"
      data-reachability={state}
      className="flex items-center justify-center gap-2 px-4 py-2 text-center"
      style={{
        background: 'var(--caution-bg)',
        borderBottom: '1px solid var(--line)',
        color: 'var(--ink)',
        fontSize: 'var(--text-body)',
      }}
    >
      <span>{MESSAGE[state]}</span>
    </div>
  );
}
