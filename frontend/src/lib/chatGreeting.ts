/**
 * The empty chat's greeting: one of fifteen, picked at random per visit.
 *
 * Every greeting has a form WITH a first name and a form WITHOUT one, and the
 * nameless form is a complete sentence on its own. That is the guarantee the
 * founder asked for (2026-09-30): the screen can never say "Hello ," or
 * "Hello, undefined", because a missing name selects a different string
 * rather than blanking a slot in the same one (rule 14: unknown never renders
 * as empty).
 *
 * ⚠ Where the name comes from, and where it does not. Only a name the account
 * actually carries: Supabase `user_metadata.first_name`, else the first word
 * of `full_name` / `name` (Google sign-in fills those). Signup today collects
 * the COMPANY name, not a person's, so most accounts have none and get the
 * nameless forms. The email's local part is deliberately NOT used: "team@",
 * "info@" and "jane.q.public@" would each greet somebody by a word that is
 * not their name.
 */

export interface Greeting {
  named: (first: string) => string;
  plain: string;
}

type TimeOfDay = 'morning' | 'afternoon' | 'evening';

function timeOfDay(hour: number): TimeOfDay {
  if (hour < 12) return 'morning';
  if (hour < 17) return 'afternoon';
  return 'evening';
}

/** Built per call so the time-of-day greeting reads the clock it is shown at. */
export function greetings(hour: number): readonly Greeting[] {
  const tod = timeOfDay(hour);
  return [
    { named: (n) => `Good ${tod}, ${n}`, plain: `Good ${tod}` },
    { named: (n) => `Hello, ${n}`, plain: 'Hello' },
    { named: (n) => `Welcome back, ${n}`, plain: 'Welcome back' },
    { named: (n) => `What's on your plate today, ${n}?`, plain: "What's on your plate today?" },
    { named: (n) => `Where should we start, ${n}?`, plain: 'Where should we start?' },
    { named: (n) => `What can I look up for you, ${n}?`, plain: 'What can I look up for you?' },
    { named: (n) => `Ready when you are, ${n}`, plain: 'Ready when you are' },
    { named: (n) => `What do you need, ${n}?`, plain: 'What do you need?' },
    { named: (n) => `Let's clear the queue, ${n}`, plain: "Let's clear the queue" },
    {
      named: (n) => `What's happening at your properties, ${n}?`,
      plain: "What's happening at your properties?",
    },
    { named: (n) => `Hi, ${n}. What's first?`, plain: "What's first today?" },
    { named: (n) => `Good to see you, ${n}`, plain: 'Good to see you' },
    { named: (n) => `How can I help today, ${n}?`, plain: 'How can I help today?' },
    { named: (n) => `Which property is on your mind, ${n}?`, plain: 'Which property is on your mind?' },
    { named: (n) => `Ask me about your portfolio, ${n}`, plain: 'Ask me about your portfolio' },
  ];
}

/** A first name the account really carries, or null. Never guessed. */
export function firstNameFrom(metadata: Record<string, unknown> | null | undefined): string | null {
  if (!metadata) return null;
  const candidates = [metadata.first_name, metadata.full_name, metadata.name];
  for (const raw of candidates) {
    if (typeof raw !== 'string') continue;
    const first = raw.trim().split(/\s+/)[0] ?? '';
    // Letters, with an inner apostrophe or hyphen (O'Neil, Anne-Marie).
    // Anything else (an email pasted into the name field, digits, a single
    // stray character) is not something to greet a person by.
    if (/^\p{L}[\p{L}'’-]{0,29}$/u.test(first) && first.length > 1) {
      return first.charAt(0).toUpperCase() + first.slice(1);
    }
  }
  return null;
}

export function greetingText(index: number, firstName: string | null, hour: number): string {
  const list = greetings(hour);
  const g = list[((index % list.length) + list.length) % list.length];
  return firstName ? g.named(firstName) : g.plain;
}
