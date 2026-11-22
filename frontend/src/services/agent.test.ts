/**
 * Agent SSE client — page-context attachment (Electron host, slice 2).
 *
 * When the chat UI runs inside the Occupella Electron browser, the preload
 * bridge exposes ``window.helixis.page.context()``. ``runAgent`` pulls it and
 * attaches the active tab's content to the request as the ADR 0003
 * ``context{current_page_url, current_page_title, current_page_text}`` block.
 * Everywhere else the bridge is absent and the request carries no context.
 */

import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';

vi.mock('./api', () => ({
  apiFetch: vi.fn(),
  ApiError: class extends Error {
    constructor(
      public status: number,
      msg: string,
    ) {
      super(msg);
    }
  },
  ApiAuthError: class extends Error {},
}));
// Force the real (non-demo) path so the request body is actually built.
vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./analytics', () => ({
  captureConfirmation: vi.fn(),
  captureTaskCompleted: vi.fn(),
  captureTaskFailed: vi.fn(),
  captureTaskSubmitted: vi.fn(),
}));

import { apiFetch } from './api';
import { runAgent } from './agent';

const apiFetchMock = apiFetch as unknown as ReturnType<typeof vi.fn>;

/** A minimal SSE response that immediately ends the stream. */
function doneStream(): Response {
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(new TextEncoder().encode('data: [DONE]\n'));
      c.close();
    },
  });
  return new Response(body, { status: 200 });
}

/** Decoded JSON body of the (single) apiFetch call. */
function sentBody(): Record<string, unknown> {
  return JSON.parse(apiFetchMock.mock.calls[0][1].body as string);
}

/** Run the agent once and resolve when the stream completes. */
function runOnce(): Promise<void> {
  return new Promise<void>((resolve) => {
    runAgent({
      task: 'whats on my screen',
      companyId: 'c1',
      sessionId: 's1',
      token: 't',
      onEvent: () => {},
      onError: () => resolve(),
      onDone: () => resolve(),
    });
  });
}

beforeEach(() => {
  apiFetchMock.mockReset();
  apiFetchMock.mockResolvedValue(doneStream());
});

afterEach(() => {
  delete (globalThis as { helixis?: unknown }).helixis;
});

describe('runAgent page context', () => {
  it('omits context when no Electron bridge is present', async () => {
    await runOnce();
    const body = sentBody();
    expect(body.task).toBe('whats on my screen');
    expect(body.company_id).toBe('c1');
    expect(body).not.toHaveProperty('context');
  });

  it('attaches the active tab as ADR 0003 context when the bridge is present', async () => {
    (globalThis as { helixis?: unknown }).helixis = {
      page: {
        context: async () => ({
          url: 'https://en.wikipedia.org/wiki/Electron_(software_framework)',
          title: 'Electron (software framework)',
          text: 'Electron is a free and open-source software framework…',
        }),
      },
    };
    await runOnce();
    expect(sentBody().context).toEqual({
      current_page_url: 'https://en.wikipedia.org/wiki/Electron_(software_framework)',
      current_page_title: 'Electron (software framework)',
      current_page_text: 'Electron is a free and open-source software framework…',
    });
  });

  it('omits context when the bridge returns null (non-web page)', async () => {
    (globalThis as { helixis?: unknown }).helixis = {
      page: { context: async () => null },
    };
    await runOnce();
    expect(sentBody()).not.toHaveProperty('context');
  });

  it('degrades quietly when the bridge throws', async () => {
    (globalThis as { helixis?: unknown }).helixis = {
      page: {
        context: async () => {
          throw new Error('extraction blew up');
        },
      },
    };
    await runOnce();
    expect(sentBody()).not.toHaveProperty('context');
  });
});
