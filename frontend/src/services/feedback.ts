/**
 * In-app feedback (BR #19). One POST; the backend stores a durable row
 * and mirrors a PostHog event for triage. Demo mode never sends.
 *
 * ⚠ Every report carries the diagnostic context automatically. It used to
 * carry the category, the message and the view — enough to know somebody was
 * unhappy, not enough to find out why. When chat fails the response is already
 * a 200, so the user reads "something went wrong" while the traceback stays in
 * Render's logs (gotcha 28), and a report without the thread costs a round trip
 * to the customer before diagnosis can start.
 *
 * ⚠ The thread is sent; the RUN ID is not. The server resolves the run from the
 * thread, because the report matters most when the turn failed and a failed
 * turn is the least likely to have handed this bundle a run id.
 */

import { apiFetch } from './api';
import { demoMode } from './auth';

export type FeedbackCategory = 'bug' | 'idea' | 'other';

/**
 * The bounds the SERVER enforces, mirrored here.
 *
 * ⚠ A second copy of a rule, which is the shape this repo has been bitten by —
 * TypeScript cannot import `api/routes/feedback.py`, so these two numbers have
 * to be kept honest by a test that reads both files
 * (`test_feedback_limits_match_the_client.py`). Changing either side alone
 * fails it.
 *
 * ⚠ Mirroring them is not belt-and-braces. Without the cap on this side, a long
 * report — the most valuable kind, and the one somebody spent ten minutes
 * writing — is composed happily, rejected by the server, and lost. The point of
 * knowing the number here is to stop them BEFORE they lose the text, not to
 * validate twice.
 */
export const MAX_MESSAGE_CHARS = 5000;
export const MIN_MESSAGE_CHARS = 3;

/**
 * The server refused the CONTENT — too long, too short, malformed.
 *
 * Separate from every other failure because the advice is opposite: "try again"
 * is correct for a dropped connection and actively wrong here, where trying
 * again does exactly the same thing forever.
 */
export class FeedbackRejected extends Error {
  constructor() {
    super(
      `That message can't be sent as written — it has to be between ` +
        `${MIN_MESSAGE_CHARS} and ${MAX_MESSAGE_CHARS.toLocaleString()} characters.`,
    );
    this.name = 'FeedbackRejected';
  }
}

/**
 * What the browser can say about itself. Read at call time rather than module
 * load: `navigator` is absent in a non-DOM test environment, and a module-level
 * read would throw at import and take the whole service down with it.
 */
function clientContext(): { app_version: string; user_agent: string } {
  return {
    app_version: typeof __APP_VERSION__ === 'string' ? __APP_VERSION__ : '',
    user_agent: typeof navigator === 'undefined' ? '' : navigator.userAgent.slice(0, 400),
  };
}

export async function submitFeedback(
  category: FeedbackCategory,
  message: string,
  view: string,
  /** The chat thread open when they hit report, when there is one. */
  sessionId?: string | null,
): Promise<void> {
  if (demoMode) {
    await new Promise((r) => setTimeout(r, 400));
    return;
  }
  const res = await apiFetch('/api/v1/feedback', {
    method: 'POST',
    body: JSON.stringify({
      category,
      message,
      view,
      session_id: sessionId ?? null,
      ...clientContext(),
    }),
  });

  // ⚠ THIS CHECK WAS MISSING, and it is the worst kind of missing. `apiFetch`
  // resolves with the Response whatever the status — every other caller in
  // this directory tests `res.ok` — so a 422 or a 500 came back as a resolved
  // promise, the dialog ran its success path, and the person was told
  // "Sent — thanks, we reply by the next business day" for a report that
  // never arrived. In the ONLY support channel inside the product. A failure
  // nobody can perceive, wearing the costume of a success.
  if (!res.ok) {
    // 422 is the content itself (length bounds, a category the enum does not
    // know). Nothing about retrying changes it.
    if (res.status === 422) throw new FeedbackRejected();
    throw new Error(`Feedback POST failed with ${res.status}`);
  }
}
