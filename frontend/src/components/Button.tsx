/**
 * Shared button system (PR #113; restyled 2026-09-29 to match occupella.com).
 *
 * Variants
 *   primary    → solid iris (#1957A0). ONE per logical group.
 *   secondary  → white with a border. Skip, Retry, paired actions.
 *   ghost      → the tertiary action: a text link, no box.
 *   icon       → square, transparent, label-less.
 *
 * Sizes (text buttons are 36–40px, the site's range)
 *   sm = 36px, 13px text  → dense rows.
 *   md = 36px, 14px text  → default.
 *   lg = 40px, 14px text  → composer Send.
 *   Icon buttons keep their own 28/32/36 squares: they carry no text, and
 *   the Inbox rows they sit in were measured at those sizes.
 *
 * Shape
 *   Every button is 6px (`--r-sm`), the radius the site uses for buttons and
 *   inputs. `shape` is kept so call sites compile; both values render the same.
 *
 * Disabled
 *   Gray fill and gray text, not a faded copy of the enabled colour: a pale
 *   blue button reads as "loading", not "not yet". cursor: not-allowed.
 */

import type { ButtonHTMLAttributes, ReactNode } from 'react';

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'icon';
export type ButtonSize = 'sm' | 'md' | 'lg';
export type ButtonShape = 'pill' | 'rectangle';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
  shape?: ButtonShape;
  icon?: ReactNode;
  iconPosition?: 'left' | 'right';
}

const _HEIGHT_PX: Record<ButtonSize, number> = {
  sm: 36,
  md: 36,
  lg: 40,
};

/** Icon-only squares. Not the text sizes: see the header. */
const _ICON_PX: Record<ButtonSize, number> = {
  sm: 28,
  md: 32,
  lg: 36,
};

const _FONT_PX: Record<ButtonSize, number> = {
  sm: 13,
  md: 14,
  lg: 14,
};

// Horizontal padding by size. Icon variants ignore this — they use
// the height as width for a square hit target.
const _PAD_X: Record<ButtonSize, string> = {
  sm: '0.75rem',
  md: '0.875rem',
  lg: '1rem',
};

function _variantStyle(
  variant: ButtonVariant,
): {
  background: string;
  color: string;
  border: string;
  hoverBg: string;
  hoverColor?: string;
  underlineOnHover?: boolean;
} {
  switch (variant) {
    case 'primary':
      // Solid iris — reserved for the single primary CTA per logical
      // group. Hover and press go darker (--iris-hover, --iris-press).
      return {
        background: 'var(--iris)',
        color: 'var(--canvas)',
        border: '1px solid var(--iris)',
        hoverBg: 'var(--iris-hover)',
      };
    case 'secondary':
      // White with a border — Skip / Retry / paired secondary actions.
      return {
        background: 'var(--canvas)',
        color: 'var(--ink)',
        border: '1px solid var(--line-strong)',
        hoverBg: 'var(--canvas-raised)',
      };
    case 'ghost':
      // The tertiary action is a text link: no fill, no border, iris text,
      // underlined on hover. Top-right Refresh and similar.
      return {
        background: 'transparent',
        color: 'var(--iris)',
        border: '1px solid transparent',
        hoverBg: 'transparent',
        hoverColor: 'var(--iris-hover)',
        underlineOnHover: true,
      };
    case 'icon':
      // Icon-only — section-anchored. No visible chrome at rest,
      // hover surfaces a soft canvas-hover background so the
      // affordance is discoverable.
      return {
        background: 'transparent',
        color: 'var(--ink-muted)',
        border: '1px solid transparent',
        hoverBg: 'var(--canvas-hover)',
        hoverColor: 'var(--ink)',
      };
  }
}

export function Button({
  variant = 'secondary',
  size = 'md',
  shape = 'pill',
  icon,
  iconPosition = 'left',
  disabled = false,
  className = '',
  style,
  children,
  ...rest
}: ButtonProps) {
  const isIcon = variant === 'icon';
  const tint = _variantStyle(variant);
  // Gray when disabled, for the boxed variants. A disabled link or icon just
  // goes gray text.
  const boxed = variant === 'primary' || variant === 'secondary';
  const rest_ = disabled
    ? {
        background: boxed ? 'var(--disabled-bg)' : 'transparent',
        color: 'var(--disabled-ink)',
        border: boxed ? '1px solid var(--disabled-bg)' : '1px solid transparent',
      }
    : { background: tint.background, color: tint.color, border: tint.border };
  const h = isIcon ? _ICON_PX[size] : _HEIGHT_PX[size];
  // Both shapes are the site's 6px. See the header.
  void shape;
  const radius = 'var(--r-sm)';

  return (
    <button
      type={rest.type ?? 'button'}
      disabled={disabled}
      className={`inline-flex items-center justify-center transition-colors ${className}`}
      style={{
        height: h,
        width: isIcon ? h : undefined,
        // ⚠ A square icon button is square only until a flex parent squeezes
        // it. Measured in a real browser at 768px, the Inbox's Resummarize
        // and Re-plan buttons rendered 15x28 — the primitive asked for 28
        // wide and the row took 13 of it back, putting a control the design
        // states is square under the 24x24 WCAG 2.2 AA minimum. The width
        // above is the intent; these two are what make it true, and they
        // live here rather than at each call site so the next icon button in
        // a tight row cannot inherit the same defect (gotcha 25).
        minWidth: isIcon ? h : undefined,
        flexShrink: isIcon ? 0 : undefined,
        // A text link needs only enough padding for its focus ring.
        paddingLeft: isIcon ? 0 : variant === 'ghost' ? 4 : _PAD_X[size],
        paddingRight: isIcon ? 0 : variant === 'ghost' ? 4 : _PAD_X[size],
        borderRadius: radius,
        fontSize: _FONT_PX[size],
        fontWeight: 500,
        background: rest_.background,
        color: rest_.color,
        border: rest_.border,
        cursor: disabled ? 'not-allowed' : 'pointer',
        gap: isIcon ? 0 : 6,
        ...style,
      }}
      onMouseEnter={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = tint.hoverBg;
        if (tint.hoverColor) e.currentTarget.style.color = tint.hoverColor;
        if (tint.underlineOnHover) e.currentTarget.style.textDecoration = 'underline';
      }}
      onMouseLeave={(e) => {
        if (disabled) return;
        e.currentTarget.style.background = tint.background;
        e.currentTarget.style.color = tint.color;
        if (tint.underlineOnHover) e.currentTarget.style.textDecoration = 'none';
      }}
      {...rest}
    >
      {isIcon ? (
        icon
      ) : (
        <>
          {icon && iconPosition === 'left' ? <span className="inline-flex">{icon}</span> : null}
          {children}
          {icon && iconPosition === 'right' ? <span className="inline-flex">{icon}</span> : null}
        </>
      )}
    </button>
  );
}
