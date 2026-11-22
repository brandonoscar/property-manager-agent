/**
 * The four failures here are all silent, and three of them look like success.
 *
 *   1. An UNVERIFIED factor counted as protection — the row says "On" and
 *      nothing is enforcing anything.
 *   2. Re-enrolment blocked forever after one abandoned attempt, because
 *      Supabase refuses a duplicate friendly name. The user can never turn
 *      2FA on again without support, and the error names nothing they did.
 *   3. `unenroll` called without re-checking the password — anyone at an
 *      unlocked laptop strips the second factor in one click, which is
 *      precisely the attack it was bought to stop.
 *   4. A partial enrol response accepted — the setup key is the ONLY way back
 *      from a lost phone, so enrolling without one arms a lock with no key.
 */

import { beforeEach, describe, expect, it, vi } from 'vitest';

const mfa = vi.hoisted(() => ({
  listFactors: vi.fn(),
  enroll: vi.fn(),
  unenroll: vi.fn(),
  challenge: vi.fn(),
  verify: vi.fn(),
}));
const auth = vi.hoisted(() => ({
  getUser: vi.fn(),
  signInWithPassword: vi.fn(),
}));
const net = vi.hoisted(() => ({ apiFetch: vi.fn() }));

vi.mock('./api', () => ({ apiFetch: net.apiFetch }));

vi.mock('./auth', async (importOriginal) => ({
  ...(await importOriginal<typeof import('./auth')>()),
  supabase: { auth: { ...auth, mfa } },
}));

import {
  BadBackupCode,
  confirmEnrolment,
  disableTwoFactor,
  fetchBackupCodeStatus,
  generateBackupCodes,
  listVerifiedFactors,
  redeemBackupCode,
  startEnrolment,
  WrongCode,
} from './mfa';
import { CurrentPasswordWrong } from './auth';

beforeEach(() => {
  vi.clearAllMocks();
  auth.getUser.mockResolvedValue({ data: { user: { email: 'sam@blueridge.com' } } });
  auth.signInWithPassword.mockResolvedValue({ error: null });
  mfa.unenroll.mockResolvedValue({ error: null });
  mfa.challenge.mockResolvedValue({ data: { id: 'chal-1' }, error: null });
  mfa.verify.mockResolvedValue({ error: null });
});

describe('an unverified factor is not protection', () => {
  it('is excluded from the list', async () => {
    mfa.listFactors.mockResolvedValue({
      data: {
        all: [
          { id: 'f1', status: 'unverified', friendly_name: 'abandoned' },
          { id: 'f2', status: 'verified', friendly_name: 'Authenticator app' },
        ],
      },
      error: null,
    });
    const factors = await listVerifiedFactors();
    expect(factors.map((f) => f.id)).toEqual(['f2']);
  });

  it('an account with ONLY an abandoned attempt reads as unprotected', async () => {
    // The direction that matters: this must be empty, or the settings row
    // says "On" over a factor nobody ever finished setting up.
    mfa.listFactors.mockResolvedValue({
      data: { all: [{ id: 'f1', status: 'unverified', friendly_name: 'x' }] },
      error: null,
    });
    expect(await listVerifiedFactors()).toEqual([]);
  });
});

describe('an abandoned attempt does not lock you out of enrolling', () => {
  it('sweeps unverified factors before enrolling', async () => {
    mfa.listFactors.mockResolvedValue({
      data: {
        all: [
          { id: 'stale-1', status: 'unverified' },
          { id: 'stale-2', status: 'unverified' },
        ],
      },
      error: null,
    });
    mfa.enroll.mockResolvedValue({
      data: { id: 'new', totp: { qr_code: 'data:image/svg+xml,x', secret: 'ABC123' } },
      error: null,
    });

    await startEnrolment();

    expect(mfa.unenroll).toHaveBeenCalledWith({ factorId: 'stale-1' });
    expect(mfa.unenroll).toHaveBeenCalledWith({ factorId: 'stale-2' });
  });

  it('NEVER sweeps a verified one — that would silently disable 2FA', async () => {
    mfa.listFactors.mockResolvedValue({
      data: { all: [{ id: 'live', status: 'verified' }] },
      error: null,
    });
    mfa.enroll.mockResolvedValue({
      data: { id: 'new', totp: { qr_code: 'q', secret: 's' } },
      error: null,
    });

    await startEnrolment();

    expect(mfa.unenroll).not.toHaveBeenCalled();
  });
});

describe('enrolment refuses to arm a lock with no key', () => {
  it.each([
    ['no secret', { id: 'new', totp: { qr_code: 'q' } }],
    ['no qr code', { id: 'new', totp: { secret: 's' } }],
    ['no totp at all', { id: 'new' }],
  ])('throws on a partial response: %s', async (_label, data) => {
    mfa.listFactors.mockResolvedValue({ data: { all: [] }, error: null });
    mfa.enroll.mockResolvedValue({ data, error: null });
    await expect(startEnrolment()).rejects.toThrow(/Could not start setup/);
  });
});

describe('a wrong code is its own error', () => {
  it('reports WrongCode rather than a generic failure', async () => {
    // Codes rotate every 30 seconds, so this is the likeliest outcome by far.
    // "Something went wrong" sends somebody to support for a mistyped digit.
    mfa.verify.mockResolvedValue({ error: { message: 'invalid' } });
    await expect(confirmEnrolment('f1', '000000')).rejects.toBeInstanceOf(WrongCode);
  });

  it('strips spaces, because authenticator apps display "123 456"', async () => {
    await confirmEnrolment('f1', '123 456');
    expect(mfa.verify).toHaveBeenCalledWith(
      expect.objectContaining({ code: '123456' }),
    );
  });
});

describe('turning 2FA off requires the password', () => {
  it('verifies BEFORE unenrolling', async () => {
    const order: string[] = [];
    auth.signInWithPassword.mockImplementation(async () => {
      order.push('verify');
      return { error: null };
    });
    mfa.unenroll.mockImplementation(async () => {
      order.push('unenroll');
      return { error: null };
    });

    await disableTwoFactor('f1', 'correct horse battery staple');

    expect(order).toEqual(['verify', 'unenroll']);
  });

  it('does not unenroll at all when the password is wrong', async () => {
    // The whole point. Supabase's unenroll needs only a live session, so
    // without this an unlocked laptop strips the second factor in one click.
    auth.signInWithPassword.mockResolvedValue({ error: { message: 'bad' } });

    await expect(disableTwoFactor('f1', 'guess')).rejects.toBeInstanceOf(
      CurrentPasswordWrong,
    );
    expect(mfa.unenroll).not.toHaveBeenCalled();
  });

  it('refuses when there is no signed-in email to verify against', async () => {
    auth.getUser.mockResolvedValue({ data: { user: null } });
    await expect(disableTwoFactor('f1', 'x')).rejects.toThrow(/not signed in/i);
    expect(mfa.unenroll).not.toHaveBeenCalled();
  });
});

// ── Backup codes ─────────────────────────────────────────────────────

const reply = (status: number, body: unknown) =>
  ({ ok: status >= 200 && status < 300, status, json: async () => body }) as Response;

describe('backup codes', () => {
  describe('the count', () => {
    it('reports what the server said, and how many a batch issues', async () => {
      net.apiFetch.mockResolvedValue(reply(200, { remaining: 3, issued_per_batch: 10 }));
      expect(await fetchBackupCodeStatus()).toEqual({ remaining: 3, issuedPerBatch: 10 });
    });

    it('throws rather than reporting zero when the request fails', async () => {
      // ⚠ THE DIRECTION THAT MATTERS. A failed check rendered as "0 codes
      // left" is a false claim that pushes somebody to regenerate — killing
      // a sheet that was working perfectly well (gotcha 55: a guard that
      // cannot see must decline to assert, not assert the alarming value).
      net.apiFetch.mockResolvedValue(reply(500, {}));
      await expect(fetchBackupCodeStatus()).rejects.toThrow();
    });

    it('reads zero as zero, which is a real state', async () => {
      net.apiFetch.mockResolvedValue(reply(200, { remaining: 0, issued_per_batch: 10 }));
      expect((await fetchBackupCodeStatus()).remaining).toBe(0);
    });
  });

  describe('generating', () => {
    it('returns the plaintext codes', async () => {
      const codes = ['AAAAA-BBBBB-CCCCC-DDDDD', 'EEEEE-FFFFF-GGGGG-HHHHH'];
      net.apiFetch.mockResolvedValue(reply(200, { codes }));
      expect(await generateBackupCodes()).toEqual(codes);
    });

    it('POSTs — a GET that returned codes would make every stolen session a bypass', async () => {
      net.apiFetch.mockResolvedValue(reply(200, { codes: ['X'] }));
      await generateBackupCodes();
      expect(net.apiFetch.mock.calls[0][1]).toMatchObject({ method: 'POST' });
    });

    it('refuses an empty list instead of showing an empty box', async () => {
      // An empty panel reads as a finished step. Telling somebody they are
      // covered while they hold nothing is worse than an error.
      net.apiFetch.mockResolvedValue(reply(200, { codes: [] }));
      await expect(generateBackupCodes()).rejects.toThrow();
    });

    it('refuses a response with no codes field at all', async () => {
      net.apiFetch.mockResolvedValue(reply(200, {}));
      await expect(generateBackupCodes()).rejects.toThrow();
    });
  });

  describe('redeeming', () => {
    it('resolves when the server accepts the code', async () => {
      net.apiFetch.mockResolvedValue(reply(200, { factors_removed: 1 }));
      await expect(redeemBackupCode('AAAAA-BBBBB')).resolves.toBeUndefined();
    });

    it('sends the code exactly as typed, without client-side tidying', async () => {
      // The server normalises case, spaces and dashes. Tidying here as well
      // would mean two normalisers that can disagree — and the one that
      // rejects a correct code does it on the screen where somebody has
      // already lost their phone.
      net.apiFetch.mockResolvedValue(reply(200, {}));
      await redeemBackupCode('  aaaaa bbbbb  ');
      const init = net.apiFetch.mock.calls[0][1] as { body: string };
      expect(JSON.parse(init.body)).toEqual({ code: '  aaaaa bbbbb  ' });
    });

    it('calls a 400 a wrong code', async () => {
      net.apiFetch.mockResolvedValue(reply(400, { detail: 'nope' }));
      await expect(redeemBackupCode('nope')).rejects.toBeInstanceOf(BadBackupCode);
    });

    it('does NOT call a server error a wrong code', async () => {
      // ⚠ The distinction this class exists for. Somebody at this screen is
      // holding a finite sheet of paper; reporting an outage as "wrong code"
      // makes them burn a second one, then a third.
      net.apiFetch.mockResolvedValue(reply(500, {}));
      const err = await redeemBackupCode('AAAAA-BBBBB').catch((e) => e);
      expect(err).toBeInstanceOf(Error);
      expect(err).not.toBeInstanceOf(BadBackupCode);
      expect(String(err.message)).not.toMatch(/check the sheet/i);
    });

    it('does not call a 503 a wrong code either', async () => {
      net.apiFetch.mockResolvedValue(reply(503, {}));
      const err = await redeemBackupCode('AAAAA-BBBBB').catch((e) => e);
      expect(err).not.toBeInstanceOf(BadBackupCode);
    });
  });
});
