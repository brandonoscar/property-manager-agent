/**
 * One italic line of narration ("Reading the lease record…"). Renders
 * a NARRATION event between assistant turns. The line fades in over
 * ~180ms so the texture reads as live agent activity, not a flicker.
 */

import { motion } from 'motion/react';

export interface NarrationLineProps {
  content: string;
}

export function NarrationLine({ content }: NarrationLineProps) {
  return (
    <motion.div
      data-testid="narration-line"
      initial={{ opacity: 0, y: 2 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className="italic mb-3 text-[13px]"
      style={{ color: 'var(--ink-muted)' }}
    >
      {content}
    </motion.div>
  );
}
