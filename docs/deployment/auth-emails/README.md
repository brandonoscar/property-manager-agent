# Auth emails

The emails Supabase sends on this product's behalf. They were the stock
Supabase templates — plain text, no product name, no way for a recipient to
tell an invite from a phishing attempt — and they are the **first thing** a new
customer sees, before they ever open the app.

These are the branded replacements. They are files here rather than code
because Supabase stores them in its own console; nothing in this repo can
deploy them.

| File | Console template | Sent when |
|---|---|---|
| `confirm-signup.html` | Confirm signup | `supabase.auth.signUp` — `services/auth.ts`. Carries the 6-digit code (`{{ .Token }}`) the wizard asks for |
| `magic-link.html` | Magic Link | "Email me a code" on the sign-in screen: `sendSignInCode` in `services/auth.ts`. Carries the code and the link |
| `invite.html` | Invite user | `POST /company/invite` → GoTrue admin invite |
| `reset-password.html` | Reset password | `supabase.auth.resetPasswordForEmail` |
| `change-email.html` | Change Email Address | `supabase.auth.updateUser({ email })` |

## Applying them

Supabase dashboard → **Authentication → Emails → Templates**, one at a time.
Paste the file's body (everything from the outer `<div>`; the leading HTML
comment carries the subject line and can be dropped). The console wraps its own
document, so these deliberately have no `<html>`, `<head>` or `<body>` —
`tests/unit/test_auth_email_templates.py` fails if one appears.

## The four things that will break them

- **`{{ .Token }}` in the two code emails.** Confirm signup and Magic Link are
  answered on a screen that asks for a 6-digit code. Without the token the
  email carries only the link, and the screen waits for a code that never
  comes. The test reads the markup with the instruction comment stripped,
  since the comment names the token too.

- **`{{ .ConfirmationURL }}` is the whole email.** Every template uses it twice
  — the button and a paste-it-yourself fallback, because a plain-text client or
  a corporate scanner can eat the button. A template that loses it still sends
  and still looks fine. The test requires both.
- **No external CSS, no webfonts.** Email clients strip `<link>` and most
  strip `<style>`, so everything is an inline `style=`. Geist is not available
  in an inbox; these use the system stack. The app's tokens are copied as
  literal hex — `--iris` etc. do not exist here.
- **Absolute URLs only.** `/legal/privacy.html` means nothing in an inbox. The
  footer links `https://app.occupella.com/legal/…`, which is the same static set
  the app links (`vercel.json` excludes `legal/` from the SPA rewrite, so those
  are real files and not the login screen — see `legalLinks.test.tsx` for the
  bug that taught us the difference).

## Not done, and it matters more than the templates

**The sender.** Supabase's built-in mail service sends from a
`supabase.io` address and is rate-limited to a handful of messages per hour on
the default configuration. At launch that means invites and password resets can
be *silently undelivered* — a much worse failure than an ugly email, and one no
error surfaces anywhere. A custom SMTP provider (Resend, Postmark, SES) on a
domain with SPF/DKIM/DMARC is what fixes both deliverability and the
from-address.

That needs a provider account and a credential, so it is a founder decision.
**It was not measurable from this session** — the Supabase MCP exposes no auth
configuration, so whether custom SMTP is already set is unknown here rather
than known-absent.

**Secure email change.** `change-email.html` is written to read correctly
whether Supabase's "Secure email change" setting is on or off. It should be
**on**: with it off, a single confirmation from the *new* address completes the
change, so a stolen session can move an account to an attacker's address
without the real owner ever being told.
