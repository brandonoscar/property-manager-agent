/**
 * useStillLoading (PR #117 Phase D).
 *
 * Time-bounded skeleton hint. Brief stake-in-the-ground rule:
 * "Skeletons are time-bounded. If a skeleton has been on screen for
 * >2s with no data, swap to a 'still loading…' state with a
 * cancel/retry affordance."
 *
 * Usage:
 *
 *   const stillLoading = useStillLoading(isLoading, 2000);
 *   if (isLoading) return stillLoading ? <StillLoading … /> : <Skeleton … />;
 *
 * Returns ``true`` once ``loading`` has been continuously truthy for
 * ``thresholdMs``. Resets to ``false`` whenever ``loading`` flips
 * back to falsy.
 */

import { useEffect, useState } from 'react';

export function useStillLoading(loading: boolean, thresholdMs = 2000): boolean {
  const [crossed, setCrossed] = useState(false);

  useEffect(() => {
    if (!loading) {
      setCrossed(false);
      return;
    }
    const t = window.setTimeout(() => setCrossed(true), thresholdMs);
    return () => window.clearTimeout(t);
  }, [loading, thresholdMs]);

  return crossed && loading;
}
