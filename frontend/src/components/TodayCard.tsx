/**
 * Today — the attention strip at the top of the Inbox rail.
 *
 * Surfaces the morning brief (services/reports fetchMorningBrief) and
 * due reminders (services/reminders). Additive only — it sits above the
 * webhook cards and never blocks them:
 *
 *   - each fetch fails QUIETLY and independently (an error renders
 *     nothing, never an error state in front of the inbox),
 *   - an empty morning is an empty section, not invented urgency,
 *   - every item is dismissible: brief per-day (localStorage, the
 *     service's own model), reminders via the existing dismiss action.
 *
 * Detector findings NO LONGER render as their own strip rows: the
 * risk→Inbox bridge (backend, 2026-08-20) projects each finding group
 * through the enrichment→planner pipeline, so findings arrive as REAL
 * inbox rows in the full message format. The noticed digest survives
 * here only as the brief's "risk_flags" rollup line — its detail pane
 * lists the current open flags.
 *
 * Every row opens its detail IN the Inbox's right pane (onOpenItem) —
 * clicking a sidebar card never navigates away from the Inbox.
 */

import { useEffect, useState } from 'react';
import { ChevronRight, X } from 'lucide-react';
import { toast } from 'sonner';

import {
  dismissBriefForToday,
  fetchMorningBrief,
  fetchNoticed,
  isBriefDismissedToday,
  type MorningBriefItem,
  type NoticedItem,
} from '../services/reports';
import {
  dismissReminder,
  fetchReminders,
  handledReminder,
  reminderDeskNote,
  snoozeReminder,
  type Reminder,
} from '../services/reminders';
import { titleCase } from '../lib/text';

/** What a clicked Today row hands the Inbox to show in the detail pane —
 *  clicking a sidebar card opens INTO the item, right there in the Inbox
 *  (founder direction 2026-08-20; the earlier page-navigation behavior
 *  bounced users to Operations/home and read as broken). */
export interface TodayDetailItem {
  kind: 'brief' | 'noticed' | 'reminder';
  title: string;
  detail: string;
  /** Dollar amount, when the item is about money. */
  amount?: number | null;
  /** The individual findings behind a grouped "noticed" item — rendered
   *  as the detail pane's "What Occupella noticed" card. */
  findings?: Array<{ title: string; detail: string }>;
  /** Present when the item is dismissible from the detail pane too. */
  onDismiss?: () => void;
  /**
   * "Not now — ask me again in N days." Reminders only.
   *
   * ⚠ Dismiss is deliberately PERMANENT (`reminders/aging.py`: the sweep
   * never brings a dismissed card back), so without this the only verb a PM
   * had for a card they could not deal with today was the one that stops
   * tracking it for good. The backend has shipped the route, a 30-day cap
   * and a default since the feature landed; nothing ever called it.
   */
  onSnooze?: (days: number) => void;
  /**
   * "I dealt with this outside Occupella." Reminders only.
   *
   * ⚠ This is what makes gotcha 88's cooldown reachable. `handled` suppresses
   * the card for `HANDLED_COOLDOWN_DAYS` and the sweep REOPENS it if the
   * condition still holds — and `reminderDeskNote` renders "Marked handled N
   * days ago — still open" when it does. With no caller, `handled_at` could
   * never be set, so that whole ladder was unreachable in production.
   */
  onHandled?: () => void;
}

/**
 * ⚠ `Reminder.link_path` is NOT rendered, and that is a finding rather than an
 * omission — it cannot be rendered as a link today.
 *
 * The producer writes it in the STORE's vocabulary (`/properties/700`,
 * `/crm`), and `lib/routing.ts` deliberately speaks the PRODUCT's
 * (`/operations`, `/leasing`) — its own docstring says why: "leaking either
 * into a URL would give customers a link naming an internal history they have
 * no idea about". On top of that `routeFromPath` REFUSES extra segments on
 * purpose, so that adding a real `/operations/:id` later does not find every
 * already-shared link pointing somewhere plausible and wrong.
 *
 * So both live fixture values resolve to `null` — "leave the app where it
 * is" — and an `<a href>` would full-page-load, hit the SPA rewrite, and land
 * the reader on Chat with no sign anything had failed. That is the same class
 * of defect as the swallowed dismiss above, so shipping it to "use the field"
 * would have traded one silent lie for another.
 *
 * Closing it needs two things that do not exist: a store-path → product-path
 * translation, and a per-entity deep route. Both are real work and neither is
 * a reminder fix (gotcha 58 — say which gaps are decisions).
 */
import { Skeleton } from './Skeleton';

/** How many desk cards the strip shows before collapsing the rest.
 *
 * Founder direction 2026-08-30: "there should only be 3-5 Occupella noticed
 * reminders a day". Five, because the tail is one click away rather than
 * gone — a tighter number costs nothing here and hides nothing either. */
export const REMINDER_VISIBLE_LIMIT = 5;

/**
 * The "remind me later" choices, in days.
 *
 * ⚠ NOT invented here. `SnoozeIn.days` is `Field(default=2, gt=0,
 * le=MAX_SNOOZE_DAYS)` and the route's own docstring reads "remind me in 2
 * days", so the short option IS the product's stated default rather than a
 * number picked to fill a button. Both sit well inside the 30-day cap; a value
 * above it is a 422, which is why these are fixed choices and not a free field.
 */
export const SNOOZE_CHOICES: Array<{ days: number; label: string }> = [
  { days: 2, label: '2 days' },
  { days: 7, label: '1 week' },
];

export function TodayCard({ onOpenItem }: { onOpenItem?: (item: TodayDetailItem) => void }) {
  const [loading, setLoading] = useState(true);
  const [brief, setBrief] = useState<MorningBriefItem[]>([]);
  const [briefHidden, setBriefHidden] = useState(() => isBriefDismissedToday());
  const [noticed, setNoticed] = useState<NoticedItem[]>([]);
  const [reminders, setReminders] = useState<Reminder[]>([]);
  const [showAllReminders, setShowAllReminders] = useState(false);

  useEffect(() => {
    let cancelled = false;
    // Quiet-failure fetches: the Today strip is an enhancement, never
    // load-bearing for the inbox — any backend miss degrades to an
    // absent section (same rule fetchReminders applies to bad shapes).
    const briefP = fetchMorningBrief().catch(() => [] as MorningBriefItem[]);
    const noticedP = fetchNoticed().catch(() => null);
    const remindersP = fetchReminders().catch(() => [] as Reminder[]);
    void Promise.all([briefP, noticedP, remindersP]).then(([b, n, r]) => {
      if (cancelled) return;
      setBrief(b);
      // Urgent findings are delivered uncapped; the capped digest follows.
      setNoticed(n ? [...n.urgent, ...n.noticed] : []);
      setReminders(r);
      setLoading(false);
    });
    return () => {
      cancelled = true;
    };
  }, []);

  /**
   * Apply one of the three reminder verbs, optimistically — and PUT THE CARD
   * BACK if the server refused.
   *
   * ⚠ Every one of these is a real write to `helixis_reminders.status`, and
   * before this the failure was swallowed (`.catch(() => {})`). So a dismiss
   * that 500'd removed the card, told the user nothing, and the card returned
   * on the next load with no sign anything had gone wrong — an action that
   * appears to work and did not.
   *
   * `InboxPage.dismissRow` already reasons this out one screen away, for the
   * strictly LESS destructive case (that dismissal is localStorage-only and
   * reversible, and it still gets an undo toast): "a destructive-feeling
   * action that cannot be taken back is one people stop using, which quietly
   * costs the feature rather than the row." The rule was written down and
   * never reached this caller (gotchas 25/67).
   *
   * The card is restored AT ITS OLD POSITION. Appending it would silently
   * reorder the desk, and the strip caps at `REMINDER_VISIBLE_LIMIT` — so on a
   * full strip a restored card could land in the collapsed tail and read as
   * still gone, which is the failure this exists to make visible.
   *
   * ⚠ `describeSuccess` exists because ALL THREE VERBS LOOK IDENTICAL FROM THE
   * OUTSIDE: the card vanishes. Vanishing correctly describes a dismiss and
   * MISDESCRIBES the other two, which promise a return — snooze on a date,
   * handled if the condition still holds after the cooldown. Without a word,
   * clicking "2 days" is indistinguishable from deleting the card, which
   * defeats the reason for offering snooze at all.
   *
   * So the rule is narrow and deliberate: **say something only when the
   * outcome is not what the vanishing implies.** Dismiss stays silent — the
   * card is gone and that is the whole truth — and a toast on every action
   * would be three interruptions teaching the user to dismiss all of them.
   */
  const applyReminderAction = (
    rem: Reminder,
    run: (id: string) => Promise<Reminder>,
    failureMessage: string,
    describeSuccess?: (updated: Reminder) => string | null,
  ) => {
    const wasAt = reminders.findIndex((r) => r.id === rem.id);
    setReminders((prev) => prev.filter((r) => r.id !== rem.id));
    // ⚠ TWO-ARGUMENT `then`, NOT `.then(...).catch(...)`. The chained form
    // routes a throw from the SUCCESS handler into the failure handler, so a
    // formatter or a toast that blew up would restore the card and tell the
    // user the write failed — on a write that succeeded. The notification is
    // an enhancement and must never be able to report the action as failed
    // (gotcha 7). A mutation battery caught this: the chained version failed a
    // test because the mocked `toast` had no `success` method, which is
    // exactly the shape of the production accident.
    void run(rem.id).then(
      (updated) => {
        try {
          const said = describeSuccess?.(updated);
          if (said) toast.success(said);
        } catch {
          // Saying nothing is the correct degradation: the write landed and
          // the card is gone, which is what the user asked for.
        }
      },
      () => {
        setReminders((prev) => {
          // ⚠ THIS BRANCH CANNOT FIRE TODAY and is kept deliberately, which
          // is a different thing from being dead by accident.
          // `fetchReminders` runs exactly once, in the mount effect — there is
          // no poll — so nothing can put the card back between the optimistic
          // removal and this handler. A mutation battery confirmed it:
          // dropping the guard changes no output for any input the component
          // can produce (gotcha 58).
          //
          // It stays because the day this strip gains a refresh — the Inbox
          // beside it already polls — the absence is two rows with the same
          // key, which React renders as a silent duplicate rather than an
          // error. One line of insurance against a change nobody would connect
          // to this file.
          if (prev.some((r) => r.id === rem.id)) return prev;
          const next = [...prev];
          next.splice(wasAt < 0 ? next.length : Math.min(wasAt, next.length), 0, rem);
          return next;
        });
        toast.error(failureMessage);
      },
    );
  };

  const reminderActions = (rem: Reminder) => ({
    onDismiss: () =>
      applyReminderAction(rem, dismissReminder, "Couldn't dismiss that — it's still on your desk."),
    onSnooze: (days: number) =>
      applyReminderAction(
        rem,
        (id) => snoozeReminder(id, days),
        "Couldn't snooze that — it's still on your desk.",
        // ⚠ The date comes from the SERVER's own answer, never from
        // `now + days` computed here. The route builds `snoozed_until` itself,
        // so recomputing it client-side is a second source of truth that
        // drifts the moment the route changes how it rounds — and it would be
        // this screen, not the sweep, that told the user the wrong day.
        (updated) =>
          updated.snoozed_until
            ? `Back on ${new Date(updated.snoozed_until).toLocaleDateString(undefined, {
                weekday: 'long',
                month: 'short',
                day: 'numeric',
              })}`
            : 'Snoozed',
      ),
    onHandled: () =>
      applyReminderAction(
        rem,
        (id) => handledReminder(id),
        "Couldn't mark that handled — it's still on your desk.",
        // States the COOLDOWN, which is the whole difference between this and
        // dismiss: the sweep brings the card back if the condition still holds
        // (`reminders/aging.py`). A user who reads this as "gone for good" has
        // been told the wrong thing by silence.
        () => 'Marked handled — it comes back if it turns out not to be.',
      ),
  });

  const showBrief = !briefHidden && brief.length > 0;

  if (loading) {
    return (
      <div className="pt-1 pb-2" data-testid="inbox-today-loading">
        <Skeleton variant="rect" width="100%" height={56} />
      </div>
    );
  }

  // A quiet morning renders nothing — no empty-state card inventing
  // urgency where there is none. Findings alone don't keep the strip
  // alive either: they arrive as real inbox rows via the risk bridge.
  // ⚠ Recomputed from `reminders`, never stored. Dismissing a visible card
  // must pull the next one UP into the fold rather than leaving a gap, and a
  // snapshot taken once would not.
  const visibleReminders = showAllReminders
    ? reminders
    : reminders.slice(0, REMINDER_VISIBLE_LIMIT);
  const hiddenReminderCount = reminders.length - visibleReminders.length;

  if (!showBrief && reminders.length === 0) return null;

  return (
    <div
      data-testid="inbox-today"
      className="mt-1 mb-2.5 rounded-[var(--r-md)] overflow-hidden"
      style={{ background: 'var(--canvas)', border: '1px solid var(--card-edge)' }}
    >
      <div
        className="flex items-center justify-between px-3 pt-2.5 pb-1.5"
      >
        <span className="eyebrow">Today</span>
        {showBrief && (
          <button
            type="button"
            data-testid="inbox-today-hide-brief"
            onClick={() => {
              dismissBriefForToday();
              setBriefHidden(true);
            }}
            // ⚠ 68x16 as measured in a real browser: 10.5px text with no
            // padding, so the words WERE the target. The type size is a
            // deliberate de-emphasis and stays; the box around it grows to
            // the 24px AA minimum and pulls back into the card's padding so
            // nothing moves visually.
            className="inline-flex items-center min-h-6 px-1.5 -mr-1.5 rounded-[var(--r-sm)] text-[12px] transition-colors duration-150"
            style={{ color: 'var(--ink-faint)' }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.color = 'var(--ink-muted)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.color = 'var(--ink-faint)';
            }}
            title="Hide the brief until tomorrow"
          >
            Hide for today
          </button>
        )}
      </div>

      {/* Morning brief — severity-ordered "things that need you"; each
          row opens in the detail pane. */}
      {showBrief &&
        brief.map((item, i) => (
          <button
            key={item.key}
            type="button"
            data-testid={`inbox-today-brief-${i}`}
            onClick={() =>
              onOpenItem?.({
                kind: 'brief',
                title: titleCase(item.label),
                detail: item.detail,
                amount: item.amount,
                // The risk-flags brief item IS the noticed digest rolled
                // up — its detail pane lists the actual flags.
                findings:
                  item.key === 'risk_flags' && noticed.length > 0
                    ? noticed.map((n) => ({
                        title: titleCase(n.title),
                        detail: n.detail,
                      }))
                    : undefined,
              })
            }
            className="group flex items-center gap-2 w-full text-left px-3 py-1.5 transition-colors duration-150"
            style={{ borderTop: i === 0 ? 'none' : '1px solid var(--line)' }}
            onMouseEnter={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'var(--canvas-hover)';
            }}
            onMouseLeave={(e) => {
              (e.currentTarget as HTMLElement).style.background = 'transparent';
            }}
          >
            <span className="min-w-0 flex-1">
              <span
                className="block truncate text-[13px] font-medium"
                style={{ color: 'var(--ink)' }}
              >
                {titleCase(item.label)}
              </span>
              <span
                className="block truncate text-[12px]"
                style={{ color: 'var(--ink-muted)' }}
              >
                {item.detail}
              </span>
            </span>
            <ChevronRight
              size={14}
              strokeWidth={1.75}
              className="shrink-0 transition-colors"
              style={{ color: 'var(--line-strong)' }}
            />
          </button>
        ))}

      {/* Reminders — the worker sweep's neglect-aware desk cards, CAPPED.
          Founder direction 2026-08-30: at most a handful a day, because a
          long stack is indistinguishable from a bug. Measured that morning:
          one company held 16 open cards reading the SAME sentence (the
          per-property gutter charge, now grouped by ``bills_due``) and
          another held 55 with 55 distinct titles — grouping collapses that
          second one to 50, so grouping alone was never going to be enough.

          ⚠ Collapsed, NOT dropped. Every card is still fetched, still real,
          still clearable; the tail sits one click away with its exact count.
          Suppressing an overdue bill so the strip looks tidy is the failure
          this product treats as its worst — a real obligation that silently
          never reaches the user. */}
      {visibleReminders.map((rem, i) => (
        <div
          key={rem.id}
          data-testid={`inbox-today-reminder-${i}`}
          className="group flex items-start gap-1 px-3 py-1.5 transition-colors duration-150"
          style={{ borderTop: showBrief || i > 0 ? '1px solid var(--line)' : 'none' }}
          onMouseEnter={(e) => {
            (e.currentTarget as HTMLElement).style.background = 'var(--canvas-hover)';
          }}
          onMouseLeave={(e) => {
            (e.currentTarget as HTMLElement).style.background = 'transparent';
          }}
        >
          <button
            type="button"
            className="min-w-0 flex-1 text-left"
            data-testid={`inbox-today-reminder-open-${i}`}
            onClick={() =>
              onOpenItem?.({
                kind: 'reminder',
                title: titleCase(rem.title),
                detail: rem.body ?? '',
                ...reminderActions(rem),
              })
            }
          >
            <span
              className="block truncate text-[13px] font-medium"
              style={{ color: 'var(--ink)' }}
            >
              {titleCase(rem.title)}
            </span>
            {rem.body && (
              <span className="block truncate text-[12px]" style={{ color: 'var(--ink-muted)' }}>
                {rem.body}
              </span>
            )}
            {/* The visible half of the age ladder. Without it the server's
                escalation would be a number nobody sees — and a card that
                has been ignored for six weeks would go on looking exactly
                like one raised this morning, which is the defect. Rendered
                only for cards the SERVER has already decided are stale, so
                the strip is unchanged for everything behaving normally. */}
            {(() => {
              const note = reminderDeskNote(rem);
              return note ? (
                <span
                  className="block truncate text-[12px]"
                  data-testid={`inbox-today-reminder-age-${i}`}
                  style={{ color: 'var(--ink-muted)' }}
                >
                  {note}
                </span>
              ) : null;
            })()}
          </button>
          <ChevronRight
            size={14}
            strokeWidth={1.75}
            className="shrink-0 mt-0.5 order-last"
            style={{ color: 'var(--line-strong)' }}
          />
          <DismissButton
            testId={`inbox-today-reminder-dismiss-${i}`}
            label="Dismiss reminder"
            // The strip row keeps ONE verb deliberately: five 12.5px rows with
            // three buttons each is a different component. Snooze and "mark
            // handled" live in the detail pane, which this row opens.
            onClick={reminderActions(rem).onDismiss}
          />
        </div>
      ))}

      {hiddenReminderCount > 0 && (
        <button
          type="button"
          data-testid="inbox-today-reminder-more"
          onClick={() => setShowAllReminders(true)}
          className="w-full px-3 py-1.5 text-left text-[12px]"
          style={{ borderTop: '1px solid var(--line)', color: 'var(--ink-muted)' }}
        >
          {hiddenReminderCount} more {hiddenReminderCount === 1 ? 'item needs' : 'items need'}{' '}
          attention &rarr;
        </button>
      )}
    </div>
  );
}

function DismissButton({
  testId,
  label,
  onClick,
}: {
  testId: string;
  label: string;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      data-testid={testId}
      aria-label={label}
      title={label}
      onClick={onClick}
      className="shrink-0 flex items-center justify-center w-6 h-6 -mr-1 rounded-[var(--r-xs)] transition-colors duration-150"
      style={{ color: 'var(--ink-faint)' }}
      onMouseEnter={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'var(--canvas-hover)';
        el.style.color = 'var(--ink-muted)';
      }}
      onMouseLeave={(e) => {
        const el = e.currentTarget as HTMLElement;
        el.style.background = 'transparent';
        el.style.color = 'var(--ink-faint)';
      }}
    >
      <X size={12} strokeWidth={1.75} />
    </button>
  );
}
