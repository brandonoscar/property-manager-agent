/**
 * Streaming assistant turn. While ``streaming`` is true, the trailing
 * cursor pulses; once closed (i.e. ASSISTANT_MESSAGE_END landed), the
 * cursor disappears and the text settles.
 *
 * PR #75 — Chat needs proper bubble chrome (a soft surface) while the
 * Inbox keeps its typography-only layout. ``variant`` toggles between
 * the two; the default (``"inbox"``) preserves the existing look.
 *
 * PR #125 — assistant output is now piped through react-markdown so
 * ``**bold**`` / ``*italic*`` / list / heading / code / link syntax
 * renders properly. Before this, the model's output was dumped raw
 * onto the canvas via ``whitespace-pre-wrap`` and users saw literal
 * asterisks and the word "bold" wrapped in two stars (the Claude
 * extension's UI audit flagged this as the most visible bug). We
 * stay deliberately conservative — no remark-gfm tables / strike /
 * autolinks for now, just the markdown syntax react-markdown ships
 * out-of-the-box. ``remark-gfm`` can be added later if needed.
 *
 * During streaming the markdown render runs on every partial buffer, with
 * unclosed bold and code closed first (settleStreamingMarkdown) so the
 * partial text renders the way the finished text will.
 */

import { useEffect, useMemo, useRef } from 'react';
import { useTypewriter } from '../../hooks/useTypewriter';
import { motion } from 'motion/react';
import ReactMarkdown from 'react-markdown';

export type AssistantBubbleVariant = 'chat' | 'inbox';

/** Follow the typing only when the reader is this close to the bottom. */
const FOLLOW_WITHIN_PX = 240;

/**
 * Close markdown a streaming answer has opened but not yet closed, for the
 * RENDER only (the stored text is untouched).
 *
 * Without it, "**Apex Plumb" renders as literal asterisks until the closing
 * pair arrives, then the whole phrase snaps to bold: a visible flicker on
 * every bold phrase. An unfinished code fence is closed the same way. A
 * marker with nothing after it yet is dropped rather than closed, because
 * "****" is not bold, it is four asterisks.
 */
export function settleStreamingMarkdown(text: string): string {
  const fences = (text.match(/```/g) ?? []).length;
  if (fences % 2 === 1) return `${text}\n\`\`\``;
  let t = text;
  const closeWith = (marker: string, count: number) => {
    if (count % 2 === 0) return;
    const body = t.trimEnd();
    const tail = t.slice(body.length);
    t = body.endsWith(marker) ? body.slice(0, -marker.length) + tail : body + marker + tail;
  };
  closeWith('**', (t.match(/\*\*/g) ?? []).length);
  closeWith('`', (t.match(/`/g) ?? []).length);
  return t;
}

export interface AssistantBubbleProps {
  /** Accumulated text for this turn. Parent assembles from
   *  ASSISTANT_MESSAGE_TOKEN events in order. */
  content: string;
  /** Whether the turn is still streaming. When false the cursor is
   *  hidden and the bubble reads as settled prose. */
  streaming: boolean;
  /** ``"chat"`` renders a left-aligned prose column under an avatar;
   *  ``"inbox"`` (default) is typography-only. */
  variant?: AssistantBubbleVariant;
  /** This turn continues one directly above it (the model narrating between
   *  its own tool calls), so the avatar and the "Occupella" label are
   *  suppressed and the spacing tightens. Without this, a five-tool run
   *  stamps six identical name badges down the page and the transcript reads
   *  as six separate messages rather than one train of thought. */
  continuation?: boolean;
}

export function AssistantBubble({
  content,
  streaming,
  variant = 'inbox',
  continuation = false,
}: AssistantBubbleProps) {
  const ref = useRef<HTMLDivElement>(null);

  // ⚠ Types the answer out instead of dropping it in slabs. The backend
  // cannot publish per token — `StreamScrubber` only cuts where an
  // identifier cannot straddle the seam, which is a safety property, and
  // its own docstring says the typing feel belongs here. A settled turn
  // reveals instantly, so re-reading history never replays an animation.
  const shown = useTypewriter(content, streaming);

  // Follow the answer as it types, but only while the reader is already at
  // the bottom of the thread.
  //
  // ⚠ This was `scrollIntoView({ block: 'end' })` on every reveal tick (60 a
  // second). It scrolled EVERY scrollable ancestor, the page included, and it
  // ran whether or not the reader had scrolled up to read something above,
  // so the view kept being yanked down under them. It now moves only the
  // thread's own scroller (marked `data-follow-scroll`) and only when that
  // scroller is within a screenful of the end.
  //
  // Keyed on what is VISIBLE, not on what has arrived: the reveal runs on its
  // own clock, so following `content` would scroll to text that is not there
  // yet and then type the rest below the fold.
  useEffect(() => {
    if (!streaming) return;
    const box = ref.current?.closest<HTMLElement>('[data-follow-scroll]');
    if (!box) return;
    const gap = box.scrollHeight - box.scrollTop - box.clientHeight;
    if (gap > FOLLOW_WITHIN_PX) return;
    box.scrollTop = box.scrollHeight;
  }, [shown, streaming]);

  const isChat = variant === 'chat';

  // PR #125 — memoize the rendered markdown so we don't re-parse on
  // every parent re-render once streaming has settled. While
  // streaming, content changes per-token so the memo busts naturally
  // — that's the work we're already paying for. It now keys on the REVEALED
  // text, which is what the user is looking at.
  //
  // While streaming, an opened `**` or backtick is closed for the render
  // (settleStreamingMarkdown), so a half-typed bold phrase shows as bold
  // instead of flashing literal asterisks and then snapping into bold.
  const rendered = useMemo(
    () => <MarkdownBody content={streaming ? settleStreamingMarkdown(shown) : shown} />,
    [shown, streaming],
  );

  return (
    <motion.div
      ref={ref}
      data-testid="assistant-bubble"
      data-variant={variant}
      // ⚠ No `layout`. Motion's layout animation answers every size change
      // with a scale transform, and a streaming bubble changes size on every
      // line it wraps: the text visibly squashed and stretched as it grew.
      // That was the "answer glitches in". The fade-in on mount stays.
      initial={{ opacity: 0, y: 4 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.18, ease: [0.2, 0.8, 0.2, 1] }}
      className={
        isChat
          ? `${continuation ? 'mb-3' : 'mb-6'} flex justify-start items-start gap-2.5`
          : 'mb-6 max-w-[680px] text-[14px] leading-relaxed'
      }
      style={isChat ? undefined : { color: 'var(--ink)' }}
    >
      {isChat ? (
        <>
          {/* PR #129 — Occupella avatar mirrors the UserBubble's
              workspace chip on the right: the Occupella mark in an
              iris-soft square, so the role identity reads at a glance even
              on long threads.

              ⚠ This was a Sparkles glyph. DESIGN.md forbids "sparkle icons
              / 'AI-powered' badges on AI features" on sight — it is the
              single most reliable AI-generated tell, and it was sitting on
              the assistant's own avatar, the most repeated element in the
              product. A letter mark says whose turn this is without
              decorating the fact that a model wrote it.

              A continuation keeps the SPACE but drops the mark, so the
              prose stays in one column instead of stepping left when the
              badge disappears. */}
          <div
            aria-hidden
            data-testid={continuation ? 'assistant-avatar-spacer' : 'assistant-avatar'}
            className="shrink-0 flex items-center justify-center"
            style={{
              width: 28,
              height: 28,
              borderRadius: 'var(--r-sm)',
              background: continuation ? 'transparent' : 'var(--iris-soft)',
              border: continuation ? 'none' : '0.5px solid var(--iris-ring)',
              marginTop: continuation ? 0 : 18,
            }}
          >
            {!continuation && (
              <span
                style={{
                  fontSize: 13,
                  fontWeight: 600,
                  lineHeight: 1,
                  letterSpacing: '-0.01em',
                  color: 'var(--iris-hover)',
                }}
              >
                O
              </span>
            )}
          </div>
          <div className="flex flex-col items-start max-w-[680px]">
            {!continuation && (
              <div
                className="text-[12px] font-medium mb-1"
                style={{ color: 'var(--ink-muted)' }}
              >
                Occupella
              </div>
            )}
            {/* No surface. The model's prose is the page, not a message in a
                container -- a run that narrates between five tool calls used
                to stack five bordered cards down the thread, which reads as a
                chat log rather than something being written. Type, spacing
                and the avatar column carry the structure instead. */}
            <div className="text-[14px] leading-relaxed" style={{ color: 'var(--ink)' }}>
              {rendered}
              {streaming && <StreamingCursor />}
            </div>
          </div>
        </>
      ) : (
        <>
          {rendered}
          {streaming && <StreamingCursor />}
        </>
      )}
    </motion.div>
  );
}


// PR #125 — markdown body renderer. Each markdown node maps to a
// styled element that fits the surrounding bubble typography:
// paragraphs keep the bubble's relaxed leading, lists indent inside
// the bubble, ``**bold**`` becomes ``<strong>`` with semibold
// weight, code uses canvas-elevated chip backgrounds, links pick up
// the iris accent. No raw HTML allowed (default react-markdown
// behavior). External links open in a new tab.
function MarkdownBody({ content }: { content: string }) {
  return (
    <ReactMarkdown
      // Conservative component override map. Keep the markdown
      // syntax react-markdown handles by default; override only the
      // nodes that render visually awkward inside our chat bubble
      // chrome (overly-large headings, raw block-spacing, etc.).
      components={{
        p: ({ children }) => (
          <p className="m-0 mb-2 last:mb-0" data-testid="md-p">
            {children}
          </p>
        ),
        strong: ({ children }) => (
          <strong className="font-semibold" data-testid="md-strong">
            {children}
          </strong>
        ),
        em: ({ children }) => <em className="italic">{children}</em>,
        ul: ({ children }) => (
          <ul
            className="m-0 mb-2 last:mb-0 pl-5 list-disc"
            data-testid="md-ul"
          >
            {children}
          </ul>
        ),
        ol: ({ children }) => (
          <ol
            className="m-0 mb-2 last:mb-0 pl-5 list-decimal"
            data-testid="md-ol"
          >
            {children}
          </ol>
        ),
        li: ({ children }) => <li className="mb-1 last:mb-0">{children}</li>,
        code: ({ children, ...rest }) => {
          // Inline ``code`` vs fenced ```code``` — react-markdown
          // passes ``className`` for fenced (e.g. ``language-ts``)
          // and nothing for inline. We render inline as a tinted
          // chip; fenced as a pre-wrapped block so long lines wrap
          // instead of horizontally scrolling out of the bubble.
          const className = (rest as { className?: string }).className;
          if (className) {
            return (
              <pre
                className="m-0 mb-2 last:mb-0 p-3 rounded-md overflow-auto whitespace-pre-wrap text-[13px]"
                style={{
                  background: 'var(--canvas)',
                  border: '1px solid var(--line)',
                  color: 'var(--ink)',
                }}
              >
                <code>{children}</code>
              </pre>
            );
          }
          return (
            <code
              className="px-1 py-[1px] rounded text-[13px]"
              style={{
                background: 'var(--canvas-elevated)',
                border: '1px solid var(--line)',
                color: 'var(--ink)',
              }}
            >
              {children}
            </code>
          );
        },
        a: ({ children, href }) => (
          <a
            href={href}
            target="_blank"
            rel="noopener noreferrer"
            className="underline underline-offset-2"
            style={{ color: 'var(--iris)' }}
          >
            {children}
          </a>
        ),
        // Headings inside chat bubbles are usually noise — render at
        // body weight to avoid breaking the bubble's visual rhythm.
        h1: ({ children }) => (
          <p className="m-0 mb-2 last:mb-0 font-semibold">{children}</p>
        ),
        h2: ({ children }) => (
          <p className="m-0 mb-2 last:mb-0 font-semibold">{children}</p>
        ),
        h3: ({ children }) => (
          <p className="m-0 mb-2 last:mb-0 font-semibold">{children}</p>
        ),
      }}
    >
      {content}
    </ReactMarkdown>
  );
}

function StreamingCursor() {
  return (
    <span
      aria-hidden
      className="inline-block align-baseline ml-[2px] w-[7px] h-[14px] rounded-[1px]"
      style={{
        background: 'var(--iris)',
        animation: 'pulse-dot 1.1s var(--ease) infinite',
        verticalAlign: '-2px',
      }}
    />
  );
}
