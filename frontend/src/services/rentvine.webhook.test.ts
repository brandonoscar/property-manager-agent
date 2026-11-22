/**
 * The Rentvine webhook calls the Settings card makes. The route side, and the
 * check that the URL path matches the receiver, are in
 * tests/unit/test_rentvine_routes.py (the signing-key section).
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('./auth', () => ({ demoMode: false }));
vi.mock('./api', () => ({ apiFetch: vi.fn() }));

import { apiFetch } from './api';
import {
  deleteRentvineWebhookSecret,
  rentvineWebhookUrl,
  saveRentvineWebhookSecret,
} from './rentvine';

const fetchMock = vi.mocked(apiFetch);

function answer(body: unknown): Response {
  return { json: async () => body } as Response;
}

describe('Rentvine webhook calls', () => {
  beforeEach(() => {
    fetchMock.mockReset();
  });
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('saves the key with a PUT whose body is only the key', async () => {
    fetchMock.mockResolvedValue(answer({ configured: true, webhook_secret_saved: true }));

    const status = await saveRentvineWebhookSecret('rv-signing-key');

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/rentvine/webhook-secret', {
      method: 'PUT',
      body: JSON.stringify({ secret: 'rv-signing-key' }),
    });
    expect(status.webhook_secret_saved).toBe(true);
  });

  it('removes the key with a DELETE on the same route', async () => {
    fetchMock.mockResolvedValue(answer({ configured: true, webhook_secret_saved: false }));

    await deleteRentvineWebhookSecret();

    expect(fetchMock).toHaveBeenCalledWith('/api/v1/rentvine/webhook-secret', { method: 'DELETE' });
  });

  it('builds the URL on the backend host, not the page origin (gotcha 135)', () => {
    expect(rentvineWebhookUrl('examplepm')).toBe(
      'https://your-api.onrender.com/webhooks/rentvine/examplepm',
    );
    vi.stubEnv('VITE_RENDER_BACKEND_URL', 'https://preview-api.example.com/');
    expect(rentvineWebhookUrl('examplepm')).toBe(
      'https://preview-api.example.com/webhooks/rentvine/examplepm',
    );
  });
});
