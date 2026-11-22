/**
 * Store-row → ConversationEvent[] projection (PR #71, updated PR #75).
 *
 * Pins the contract the live SSE path relies on:
 *
 * - **PR #75 change**: every user message emits a ``user_bubble``
 *   event (was a fake assistant turn with ``→ {text}``). The Chat
 *   renderer maps these to right-aligned iris-purple ``<UserBubble>``
 *   nodes; the Inbox path never sees ``user_bubble`` events.
 * - Every assistant message with tool events emits ``narration``
 *   lines before the bubble; in chat the ConversationThread collapses
 *   those into one "Used N tools" pill.
 * - The latest assistant turn renders ``streaming`` while the run is
 *   in flight and ``settled`` once it's done.
 */

import { describe, expect, it } from 'vitest';

import { __test__ } from './ChatThread';
import type { HelixisEvent } from '../services/agent';

const { foldMessagesToConversation } = __test__;

interface StoredMessageInput {
  id: string;
  role: 'user' | 'agent';
  content: string;
  timestamp: number;
  events?: HelixisEvent[];
}

describe('foldMessagesToConversation', () => {
  it('renders a single user message as a user_bubble event', () => {
    const input: StoredMessageInput[] = [
      {
        id: 'm-1',
        role: 'user',
        content: 'what leases are about to end',
        timestamp: 0,
      },
    ];
    const out = foldMessagesToConversation(input, false);
    // PR #75 — the user's input is now its own event type. The
    // ConversationThread (variant="chat") renders this as a
    // right-aligned UserBubble; the Inbox path never receives this
    // event so it never had to be backwards-compatible.
    expect(out.map((e) => e.type)).toEqual(['user_bubble']);
    expect(out[0].data).toMatchObject({ content: 'what leases are about to end' });
  });

  it('emits ONE narration row per tool call, not one per event', () => {
    const input: StoredMessageInput[] = [
      {
        id: 'a-1',
        role: 'agent',
        content: 'Sure — here are the leases.',
        timestamp: 0,
        events: [
          {
            type: 'tool_start',
            data: { tool: 'buildium_list_leases', description: 'Calling…' },
            timestamp: 0,
          },
          {
            type: 'tool_result',
            data: { tool: 'buildium_list_leases', result: '[]', duration_ms: 12 },
            timestamp: 0,
          },
        ],
      },
    ];
    // A start + its result is ONE action, so it is ONE row. This used to be
    // two ("Calling X" then "Got the result from X"), which read as twice the
    // work and told the user nothing either time.
    const out = foldMessagesToConversation(input, false);
    expect(out.map((e) => e.type)).toEqual([
      'narration',
      'assistant_message_start',
      'assistant_message_token',
      'assistant_message_end',
    ]);
    // The legacy `description: 'Calling…'` is IGNORED (it says less than the
    // fallback), so this falls through to the humanised name.
    expect(out[0].data.content).toContain('buildium list leases');
    expect(out[0].data.content).not.toContain('Got the result');
  });

  it('resolves a call whose result never arrived once the turn is over', () => {
    // ⚠ A dropped or shed ``tool_result`` must not leave a row spinning for
    // the life of the transcript. It says it had NO RESULT rather than
    // claiming a clean finish -- a missing result laundered as success is the
    // failure this codebase keeps re-learning.
    //
    // ⚠ And "no result" is NOT "nothing came back". A step that ran and
    // returned no rows is evidence about the RECORD; a result we never
    // received is evidence about the STREAM. The wire now carries
    // ``status: 'empty'`` for the first, so the two must not share wording --
    // a shed event reading as a completed empty lookup is gotcha 26 arriving
    // through the UI.
    const input: StoredMessageInput[] = [
      {
        id: 'a-2',
        role: 'agent',
        content: 'Done.',
        timestamp: 0,
        events: [{ type: 'tool_start', data: { tool: 'buildium_get_rent' }, timestamp: 0 }],
      },
    ];
    const finished = foldMessagesToConversation(input, false);
    expect(finished[0].data.content).toContain('no result');
    expect(finished[0].data.content).not.toContain('nothing came back');

    // Still streaming: it is legitimately in flight, so it reads as running.
    const running = foldMessagesToConversation(input, true);
    expect(running[0].data.content).toContain('buildium get rent');
    expect(running[0].data.content).toMatch(/…$/);
    expect(running[0].data.content).not.toContain('no result');
  });

  // ── correlation by id, and the three terminal states ────────────────────

  it('pairs concurrent calls by call_id, so the failure lands on the right row', () => {
    // ⚠ THE WHOLE POINT. The SDK runs a turn's tool calls CONCURRENTLY
    // (gotcha 25). Here calls 1, 2 and 4 return and call 3 does not. Under
    // name+arrival-order pairing the first three rows resolve and the LAST
    // one carries the failure -- the wrong row, stated confidently.
    const ev = (type: string, id: string, extra: Record<string, unknown> = {}) => ({
      type,
      data: { tool: 'buildium_agent', call_id: id, ...extra },
      timestamp: 0,
    });
    const input: StoredMessageInput[] = [
      {
        id: 'a-3',
        role: 'agent',
        content: 'Here you go.',
        timestamp: 0,
        events: [
          ev('tool_start', 'c1'),
          ev('tool_start', 'c2'),
          ev('tool_start', 'c3'),
          ev('tool_start', 'c4'),
          ev('tool_result', 'c1', { status: 'ok' }),
          ev('tool_result', 'c2', { status: 'ok' }),
          ev('tool_result', 'c4', { status: 'ok' }),
        ],
      },
    ];
    const out = foldMessagesToConversation(input, false)
      .filter((e) => e.type === 'narration')
      .map((e) => String(e.data.content));
    // One header + four rows, in start order. c3 is the third row.
    const rows = out.filter((c) => !c.startsWith('Checking 4 things'));
    expect(rows).toHaveLength(4);
    expect(rows[2]).toContain('no result');
    expect(rows[0]).not.toContain('no result');
    expect(rows[1]).not.toContain('no result');
    expect(rows[3]).not.toContain('no result');
  });

  it('renders empty and error results differently from a clean one', () => {
    const row = (status: string) =>
      String(
        foldMessagesToConversation(
          [
            {
              id: `a-${status}`,
              role: 'agent',
              content: 'x',
              timestamp: 0,
              events: [
                { type: 'tool_start', data: { tool: 'unpaid_bills', call_id: 'z' }, timestamp: 0 },
                {
                  type: 'tool_result',
                  data: { tool: 'unpaid_bills', call_id: 'z', status },
                  timestamp: 0,
                },
              ],
            },
          ],
          false,
        )[0].data.content,
      );
    expect(row('ok')).not.toContain('—');
    // ⚠ Says what was empty and does NOT call it an answer (gotcha 26).
    expect(row('empty')).toContain('nothing came back');
    expect(row('empty')).not.toMatch(/\bno (bills|records|results)\b/i);
    expect(row('error')).toContain("didn't work");
  });

  it('announces a parallel burst with the count it can actually see', () => {
    // ⚠ Counted on the CLIENT. The backend sees one stream event at a time
    // and would say "2 things" while the third and fourth were still
    // arriving; a wrong number in a status line is worse than no line.
    const ev = (type: string, id: string) => ({
      type,
      data: { tool: 'buildium_agent', call_id: id },
      timestamp: 0,
    });
    const out = foldMessagesToConversation(
      [
        {
          id: 'a-burst',
          role: 'agent',
          content: 'x',
          timestamp: 0,
          events: [ev('tool_start', 'c1'), ev('tool_start', 'c2'), ev('tool_start', 'c3')],
        },
      ],
      true,
    ).map((e) => String(e.data.content));
    expect(out[0]).toBe('Checking 3 things at the same time');
  });

  it('says nothing about concurrency when it cannot count it', () => {
    // A replayed pre-call_id stream has no identities, so the burst is
    // unknowable. Silence, never a guess.
    const out = foldMessagesToConversation(
      [
        {
          id: 'a-legacy',
          role: 'agent',
          content: 'x',
          timestamp: 0,
          events: [
            { type: 'tool_start', data: { tool: 'buildium_agent' }, timestamp: 0 },
            { type: 'tool_start', data: { tool: 'gmail_agent' }, timestamp: 0 },
          ],
        },
      ],
      true,
    ).map((e) => String(e.data.content));
    expect(out.some((c) => c.includes('at the same time'))).toBe(false);
  });

  it('does not let one call resolve another that only shares its name', () => {
    // The name+count fallback must answer ONLY for starts carrying no id --
    // otherwise an id-paired result would be counted twice and a genuinely
    // unresolved legacy row would render as finished.
    const out = foldMessagesToConversation(
      [
        {
          id: 'a-mix',
          role: 'agent',
          content: 'x',
          timestamp: 0,
          events: [
            { type: 'tool_start', data: { tool: 'buildium_agent' }, timestamp: 0 },
            { type: 'tool_start', data: { tool: 'buildium_agent', call_id: 'c9' }, timestamp: 0 },
            {
              type: 'tool_result',
              data: { tool: 'buildium_agent', call_id: 'c9', status: 'ok' },
              timestamp: 0,
            },
          ],
        },
      ],
      false,
    )
      .filter((e) => e.type === 'narration')
      .map((e) => String(e.data.content));
    expect(out[0]).toContain('no result'); // the id-less start is still open
    expect(out[1]).not.toContain('no result'); // c9 resolved by identity
  });

  it('passes the model reasoning through, in stream order', () => {
    // ⚠ The only signal that exists during a nested specialist consult, which
    // is most of a long turn (gotcha 37). It has to reach the renderer as its
    // OWN type: folding it into ``thinking`` would put paragraphs of raw
    // working-out at body size where the answer goes.
    const out = foldMessagesToConversation(
      [
        {
          id: 'a-cot',
          role: 'agent',
          content: '',
          timestamp: 0,
          events: [
            { type: 'reasoning', data: { content: 'Lease ends in March.' }, timestamp: 0 },
            { type: 'tool_start', data: { tool: 'buildium_get_lease' }, timestamp: 0 },
            { type: 'reasoning', data: { content: '   ' }, timestamp: 0 },
          ],
        },
      ],
      true,
    );
    // (an in-flight turn also opens its reply bubble — only the trail
    // events are asserted here)
    expect(out.map((e) => e.type).slice(0, 2)).toEqual(['reasoning', 'narration']);
    expect(out.filter((e) => e.type === 'reasoning')).toHaveLength(1);
    expect(out[0].data.content).toBe('Lease ends in March.');
  });

  it('renders the model prose as an assistant bubble, not a step line', () => {
    // ``thinking`` is the model's OWN text between tool calls. It was being
    // folded into the collapsed "N steps" pill at 11.5px next to the call log
    // -- prose rendered as telemetry.
    const input: StoredMessageInput[] = [
      {
        id: 'a-3',
        role: 'agent',
        content: 'Bennett owes $1,240.',
        timestamp: 0,
        events: [
          {
            type: 'thinking',
            data: { content: 'Checking both spellings of Bennett.' },
            timestamp: 0,
          },
          { type: 'tool_start', data: { tool: 'buildium_agent' }, timestamp: 0 },
          { type: 'tool_result', data: { tool: 'buildium_agent', result: 'ok' }, timestamp: 0 },
        ],
      },
    ];
    const out = foldMessagesToConversation(input, false);
    expect(out.map((e) => e.type)).toEqual([
      'assistant_message_start',
      'assistant_message_token',
      'assistant_message_end',
      'narration',
      'assistant_message_start',
      'assistant_message_token',
      'assistant_message_end',
    ]);
    expect(out[1].data.content).toBe('Checking both spellings of Bennett.');
    // And it is NOT a narration line -- that is what buried it in the pill.
    expect(out.filter((e) => e.type === 'narration')).toHaveLength(1);
  });

  it('drops empty prose rather than emitting an empty bubble', () => {
    const input: StoredMessageInput[] = [
      {
        id: 'a-4',
        role: 'agent',
        content: 'x',
        timestamp: 0,
        events: [{ type: 'thinking', data: { content: '   ' }, timestamp: 0 }],
      },
    ];
    const out = foldMessagesToConversation(input, false);
    expect(out.map((e) => e.type)).toEqual([
      'assistant_message_start',
      'assistant_message_token',
      'assistant_message_end',
    ]);
  });

  it('leaves the latest in-flight bubble open (no end event yet)', () => {
    const input: StoredMessageInput[] = [
      { id: 'u-1', role: 'user', content: 'hi', timestamp: 0 },
      { id: 'a-1', role: 'agent', content: 'Working on', timestamp: 0 },
    ];
    const out = foldMessagesToConversation(input, true);
    // PR #75 — the user turn emits ``user_bubble`` (no end event)
    // and the live assistant turn emits start+token without a
    // closing end. So we expect zero ``assistant_message_end``s.
    const endEvents = out.filter((e) => e.type === 'assistant_message_end');
    expect(endEvents).toHaveLength(0);
    // And exactly one user_bubble at the top.
    expect(out[0].type).toBe('user_bubble');
  });

  it('closes the trailing bubble when the run has finished', () => {
    const input: StoredMessageInput[] = [
      { id: 'u-1', role: 'user', content: 'hi', timestamp: 0 },
      { id: 'a-1', role: 'agent', content: 'all done', timestamp: 0 },
    ];
    const out = foldMessagesToConversation(input, false);
    const endEvents = out.filter((e) => e.type === 'assistant_message_end');
    // PR #75 — only the agent turn emits ``assistant_message_end``.
    expect(endEvents).toHaveLength(1);
    // And we still have the user_bubble row.
    expect(out[0].type).toBe('user_bubble');
  });
});


// ── PR #115 Phase A — chat landing hero ─────────────────────────────


describe('chat landing: starter questions (brief, 2026-09-29)', () => {
  // RE-POINTED from the PR #115 wordmark-size and tagline pins. The wordmark
  // and the tagline were removed on purpose; what the empty screen now
  // promises is these four questions, so they are what is pinned.
  it('offers the four starters the brief names, in order', () => {
    expect(__test__.STARTER_QUESTIONS).toEqual([
      "Who's behind on rent?",
      "What's open at my properties?",
      'Which leases end in the next 90 days?',
      'What do we owe vendors?',
    ]);
  });

  it('makes no approval or automation claim', () => {
    // The old tagline ended "you approve the work", an approval claim held
    // until the autonomous-notes flag is confirmed off, and before that
    // "automatically", which was false. Neither may come back as a starter.
    for (const q of __test__.STARTER_QUESTIONS) {
      expect(q).not.toMatch(/approv|automatic/i);
      expect(q).not.toMatch(/—|→/);
    }
  });
});


describe('the composer offers no quick-action chips', () => {
  it('exports no chip list at all', async () => {
    // The three prefill chips ("Late rent" / "Maintenance" / "Tenant emails")
    // were REMOVED, not hidden (founder direction, 2026-08-30) — they typed a
    // sentence into a box the user was already looking at, and three of them
    // made the empty screen read as though Occupella did three things.
    //
    // ⚠ This inverts the old "Owner report" pin rather than deleting it. That
    // test existed to stop a removed chip quietly coming back; the same rule
    // now covers ALL of them, and the strongest form is that the module
    // exports no chip constant to render. Four sibling tests that pinned chip
    // LABELS and PROMPT COPY went with the feature they described — they were
    // testing content that no longer exists, not behaviour that regressed.
    const mod = await import('./InputBar');
    expect('QUICK_ACTIONS' in mod).toBe(false);
  });

  it('renders no chip row under the composer', async () => {
    // The testid both render sites used. Asserting on the DOM as well as the
    // module means a future re-add cannot slip back in as inline JSX.
    const src = (await import('./InputBar?raw')).default as string;
    expect(src).not.toContain('chat-composer-quick-actions');
    expect(src).not.toContain('chat-quick-action-');
  });
});
