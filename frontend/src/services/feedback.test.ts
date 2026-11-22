/**
 * `submitFeedback` against the real seam — the Response `apiFetch` hands back.
 *
 * ⚠ THE DIALOG'S TESTS CANNOT COVER THIS AND NEVER WILL. They mock
 * `submitFeedback`, so they exercise how the UI reacts to an error that has
 * already been classified; they say nothing about whether the classification
 * happens. A mutation proved it: deleting the 422 branch left all eleven of
 * them green. Producer and consumer, each well covered, with the boundary
 * between them untested — gotcha 28's rule, arriving at a status code.
 *
 * The defect this file is the floor for: `apiFetch` resolves with the Response
 * whatever the status, and `submitFeedback` never read it. So a discarded
 * report resolved successfully and the person was thanked for it.
 */

import { describe, expect, it, vi, beforeEach } from 'vitest';

vi.mock('./api', () => ({ apiFetch: vi.fn() }));
vi.mock('./auth', () => ({ demoMode: false }));

import { apiFetch } from './api';
import { FeedbackRejected, submitFeedback } from './feedback';

const fetchMock = vi.mocked(apiFetch);

/** Only the two fields `submitFeedback` reads. */
function response(status: number): Response {
  return { ok: status >= 200 && status < 300, status } as Response;
}

beforeEach(() => vi.clearAllMocks());

describe('submitFeedback', () => {
  it('resolves on a 2xx', async () => {
    fetchMock.mockResolvedValue(response(200));
    await expect(submitFeedback('bug', 'the header does not stick', 'chat')).resolves
      .toBeUndefined();
  });

  it('THROWS on a 500 rather than resolving a report that was discarded', async () => {
    fetchMock.mockResolvedValue(response(500));
    await expect(submitFeedback('bug', 'something broke', 'chat')).rejects.toThrow();
  });

  it('throws on a 401 too — any non-2xx means it did not land', async () => {
    fetchMock.mockResolvedValue(response(401));
    await expect(submitFeedback('bug', 'something broke', 'chat')).rejects.toThrow();
  });

  it('classifies a 422 as rejected content, not as a transient failure', async () => {
    // The distinction is the whole point: one deserves "try again" and the
    // other deserves "trim it or email us", and getting it backwards sends
    // somebody to retry a thing that cannot succeed.
    fetchMock.mockResolvedValue(response(422));
    await expect(submitFeedback('bug', 'x', 'chat')).rejects.toBeInstanceOf(FeedbackRejected);
  });

  it('does not classify a 500 as rejected content', async () => {
    // The other direction. A server fault reported as "your message is
    // unacceptable" makes somebody rewrite a report that was fine.
    fetchMock.mockResolvedValue(response(500));
    await expect(submitFeedback('bug', 'x'.repeat(50), 'chat')).rejects.not.toBeInstanceOf(
      FeedbackRejected,
    );
  });

  it('sends the message and the diagnostic context', async () => {
    fetchMock.mockResolvedValue(response(200));
    await submitFeedback('idea', 'sort order should stick', 'properties', 'thread-7');

    const [path, init] = fetchMock.mock.calls[0];
    expect(path).toBe('/api/v1/feedback');
    const body = JSON.parse(String(init?.body));
    expect(body).toMatchObject({
      category: 'idea',
      message: 'sort order should stick',
      view: 'properties',
      session_id: 'thread-7',
    });
    // The context is what makes a report diagnosable, and it has to arrive
    // without any caller remembering to pass it.
    expect(body).toHaveProperty('app_version');
    expect(body).toHaveProperty('user_agent');
  });
});
