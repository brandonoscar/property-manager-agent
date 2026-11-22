/**
 * What a person can perceive after typing an amount, which is the whole point.
 *
 * The control this replaces failed in a way no assertion on internal state
 * could see: `<input type="number">` holds the empty string when its text does
 * not parse, while the text stays on screen. Every test here is therefore
 * about the field's own value and the message beside it — the two things the
 * person actually has to go on.
 */

import { fireEvent, render, screen } from '@testing-library/react';
import { useState } from 'react';
import { describe, expect, it } from 'vitest';

import { MoneyInput } from './MoneyInput';

/** A parent that keeps the string, the way the renewal editor does. */
function Harness({ initial = '', min }: { initial?: string; min?: number }) {
  const [v, setV] = useState(initial);
  return (
    <>
      <MoneyInput label="Proposed rent" value={v} onChange={setV} min={min} />
      <output data-testid="raw">{v}</output>
    </>
  );
}

const field = () => screen.getByLabelText('Proposed rent') as HTMLInputElement;

describe('MoneyInput', () => {
  it('is NOT a number input', () => {
    // The whole reason this component exists. A number input drops unparseable
    // text and steps its value on the scroll wheel.
    render(<Harness />);
    expect(field().getAttribute('type')).toBe('text');
    expect(field().getAttribute('inputmode')).toBe('decimal');
  });

  it('keeps a pasted "$1,250.50" and shows it back formatted', () => {
    render(<Harness />);
    fireEvent.change(field(), { target: { value: '$1,250.50' } });
    fireEvent.blur(field());
    // The blur format IS the feedback: it says what the program understood.
    expect(field().value).toBe('$1,250.50');
    expect(screen.getByTestId('raw').textContent).toBe('$1,250.50');
    expect(screen.queryByTestId('form-error')).toBeNull();
  });

  it('normalises a bare number so the person sees the amount', () => {
    render(<Harness />);
    fireEvent.change(field(), { target: { value: '1250' } });
    fireEvent.blur(field());
    expect(field().value).toBe('$1,250.00');
  });

  it('says so rather than silently dropping an ambiguous comma', () => {
    render(<Harness />);
    fireEvent.change(field(), { target: { value: '1,25' } });
    fireEvent.blur(field());
    expect(screen.getByTestId('form-error')).toHaveTextContent(/not an amount/i);
    // Their text is left alone — reformatting junk destroys the work or
    // invents a number, and 125 here would be a hundredfold error.
    expect(field().value).toBe('1,25');
    expect(field()).toHaveAttribute('aria-invalid', 'true');
  });

  it('refuses an amount below the floor', () => {
    render(<Harness min={0} />);
    fireEvent.change(field(), { target: { value: '-50' } });
    fireEvent.blur(field());
    expect(screen.getByTestId('form-error')).toHaveTextContent(/\$0\.00 or more/);
  });

  it('clears the error as soon as they start fixing it', () => {
    render(<Harness />);
    fireEvent.change(field(), { target: { value: 'abc' } });
    fireEvent.blur(field());
    expect(screen.getByTestId('form-error')).toBeTruthy();
    fireEvent.change(field(), { target: { value: '1' } });
    expect(screen.queryByTestId('form-error')).toBeNull();
  });

  it('treats an empty field as empty, not as an error', () => {
    // Blank is "nothing to send", not "you got it wrong" — the renewal draft
    // is allowed to carry no proposed rent.
    render(<Harness initial="1250" />);
    fireEvent.change(field(), { target: { value: '' } });
    fireEvent.blur(field());
    expect(screen.queryByTestId('form-error')).toBeNull();
    expect(field().value).toBe('');
  });
});
