/**
 * Right-aligned user bubble for Chat (PR #75).
 *
 * Brandon's design rule: user messages get an iris-purple background
 * (the only place ``--iris`` is allowed as a fill in chat), white
 * text, right-aligned. The Inbox keeps using the typography-only
 * ``→ {text}`` line; this component is Chat-only and only emitted
 * when ``ConversationThread`` runs in ``variant="chat"``.
 *
 * No internal padding / line-height knobs are exposed — the layout
 * is fixed by design. Long messages wrap; the bubble's max-width
 * keeps it from spanning the full pane on a wide viewport.
 *
 * PR #129 — adds avatar + "You" label so user turns are visually
 * separable from assistant turns. The Claude extension's UX audit
 * flagged that the lack of role chrome made long threads hard to
 * scan. Optional ``initial`` prop accepts the workspace letter
 * (e.g. "B") so the avatar matches the sidebar's WorkspaceChip;
 * falls back to "Y" when absent.
 */

import { motion } from 'motion/react';

export interface UserBubbleProps {
  content: string;
  /** Workspace initial for the avatar (e.g. "B"). Defaults to "Y"
   *  ("You") when unsupplied — keeps the existing tests / fixtures
   *  working without changes. */
  initial?: string;
}

export function UserBubble({ content, initial }: UserBubbleProps) {
  const letter = (initial ?? 'Y').charAt(0).toUpperCase();
  return (
    <motion.div
      data-testid="user-bubble"
      // No `layout`: see AssistantBubble. A layout animation on the bubble
      // above a streaming answer re-ran every time the thread reflowed.
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className="mb-6 flex justify-end items-start gap-2.5"
    >
      <div className="flex flex-col items-end max-w-[680px]">
        <div
          className="text-[12px] font-medium mb-1"
          style={{
            color: 'var(--ink-muted)',
          }}
        >
          You
        </div>
        <div
          className="rounded-[var(--r-md)] px-4 py-2.5 text-[14px] leading-relaxed whitespace-pre-wrap"
          style={{
            background: 'var(--iris)',
            color: 'var(--canvas)',
          }}
        >
          {content}
        </div>
      </div>
      <div
        aria-hidden
        data-testid="user-avatar"
        className="shrink-0 flex items-center justify-center font-semibold"
        style={{
          width: 28,
          height: 28,
          borderRadius: 'var(--r-sm)',
          background: 'var(--iris)',
          color: 'var(--canvas)',
          fontSize: 12,
          marginTop: 18,
        }}
      >
        {letter}
      </div>
    </motion.div>
  );
}
