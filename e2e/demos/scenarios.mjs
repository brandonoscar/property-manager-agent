/**
 * Demo catalogue — the landing-page clips.
 *
 * ⚠ **A scenario is a SCRIPT, not a prompt.** The first version held one
 * sentence per clip and the footage was somebody typing. Three structural
 * rules came out of the rewrites, and all three are encoded here rather than
 * left to whoever records:
 *
 *   1. `preroll` runs BEFORE the camera, and the camera does not roll until
 *      work is VISIBLE (`startWhen: 'working'`). Not "after submit" — after
 *      the agent has something on screen. Frame one is mid-task.
 *
 *   2. Steps END ON AN ARTIFACT — a confirm card, a populated inbox, a table.
 *      A clip that ends on prose looks like a chatbot; one that ends on a
 *      rendered card looks like software doing a job.
 *
 *   3. Captions say what it MEANS, never what it is. "Leasing: leads, texts
 *      and calls" is an inventory. "Every text and call with a lead —
 *      remembered" is a reason to buy. Founder call, and the right one:
 *      nobody watches a demo to learn your nav labels.
 *
 * Step verbs, interpreted by record.mjs:
 *   { act: 'ask',     text }      type into the composer and submit
 *   { act: 'await'   }            wait for the reply to finish streaming
 *   { act: 'nav',     label }     click a left-nav item by name
 *   { act: 'click',   testId }    click the first match, cursor visible
 *   { act: 'hold',    s }         hold still so a viewer can read
 *   { act: 'caption', text, sub, s }   lower-third caption
 *   { act: 'gate',    hold }      wait for the confirm card, hold, decline
 *
 * ⚠ Pacing: under 2 minutes holds 88% of viewers, past 3 drops to ~50%, and a
 * landing-page clip should carry its point in well under 90 seconds. The tour
 * previously held 5s on every surface and dragged; 2-2.5s cuts read as
 * confident. When in doubt, cut sooner.
 */

export const SCENARIOS = [
  {
    id: 'leak-to-work-order',
    title: 'From a tenant text to a dispatched plumber',
    hook: '"There\'s water coming through my ceiling." The next twenty minutes are usually yours.',
    shows:
      'It drafts the work order, picks the vendor, and then STOPS and asks. Nothing reaches Buildium without a person saying yes.',
    target: '30-40s',
    // Question typed off camera; the camera waits for visible work.
    preroll: [
      {
        act: 'ask',
        text: "A tenant at 200 E Colfax Ave just reported water coming through the bedroom ceiling. Raise a work order and assign it to a plumber.",
      },
    ],
    startWhen: 'working',
    steps: [
      { act: 'caption', text: 'It reads the property, the history, and picks a vendor', s: 3.5 },
      { act: 'await' },
      { act: 'caption', text: 'Then it stops and asks', sub: 'Nothing reaches Buildium without you', s: 4 },
      { act: 'gate', hold: 6 },
      { act: 'hold', s: 2.5 },
    ],
  },

  {
    id: 'inbox-workflow',
    title: 'It reads the day before you do',
    hook: 'Things happen in Buildium all day. Most of them need a decision, not a notification.',
    shows:
      'Every event arrives already read: what it is, what Occupella noticed, and what it would do next.',
    target: '30-40s',
    preroll: [{ act: 'nav', label: 'Inbox' }, { act: 'hold', s: 1 }],
    steps: [
      { act: 'caption', text: 'Every Buildium event, already read', s: 3 },
      { act: 'hold', s: 2 },
      { act: 'click', testId: 'inbox-row-item' },
      { act: 'caption', text: 'It tells you what it noticed', sub: 'Not just what happened', s: 4 },
      { act: 'hold', s: 7 },
      // ⚠ No draft editing and no approval — founder call. Approving would
      // file a real record into a live Buildium account, on camera.
      { act: 'hold', s: 3 },
    ],
  },

  {
    id: 'product-tour',
    title: 'The whole thing in forty seconds',
    hook: "Everything you run the building with, in one place that remembers.",
    shows: 'What Occupella is — and that it remembers every interaction, not just stores them.',
    target: '35-45s',
    preroll: [{ act: 'hold', s: 0.8 }],
    steps: [
      { act: 'caption', text: 'One question instead of five reports', s: 2.5 },
      { act: 'hold', s: 1.5 },
      { act: 'nav', label: 'Inbox' },
      { act: 'caption', text: 'It reads every event before you do', s: 2.5 },
      { act: 'hold', s: 1.5 },
      { act: 'nav', label: 'Operations' },
      { act: 'caption', text: 'Your whole portfolio, always current', s: 2.5 },
      { act: 'hold', s: 1.5 },
      // ⚠ Leasing is behind the A2P compliance gate until the carriers
      // approve. Filming it before then records the setup screen, not the
      // CRM. Re-enable this pair once the campaign clears.
      // { act: 'nav', label: 'Leasing' },
      // { act: 'caption', text: 'Every text and call with a lead — remembered',
      //   sub: 'The AI recalls the whole conversation', s: 3 },
      { act: 'nav', label: 'Team Activity' },
      { act: 'caption', text: 'Every action logged, to a person', s: 2.5 },
      { act: 'hold', s: 2 },
    ],
  },
];

export const BY_ID = new Map(SCENARIOS.map((s) => [s.id, s]));
