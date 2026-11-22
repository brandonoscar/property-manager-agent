/**
 * The form card asks for the blanks — and must not become a write path.
 *
 * The behaviours worth pinning are the ones that go wrong quietly: a blank
 * optional field sent as an empty instruction, a submit button that stays
 * live and sends the same task twice, and a select that silently answers a
 * question nobody read.
 */

import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

import { FormCard, composeFormMessage } from './FormCard';
import type { FormData } from './types';

const TASK_FORM: FormData = {
  title: 'New maintenance task',
  intro: 'A couple of details and I can create it.',
  submit_label: 'Create task',
  fields: [
    { name: 'what', label: 'What needs doing', kind: 'text', value: 'Leaky faucet' },
    { name: 'unit', label: 'Which unit', kind: 'text' },
    { name: 'priority', label: 'Priority', kind: 'choice', options: ['Low', 'Normal', 'High'] },
    { name: 'notes', label: 'Anything else', kind: 'textarea' },
  ],
};

describe('composeFormMessage', () => {
  it('omits blanks rather than sending them as empty instructions', () => {
    // ⚠ "Which unit:" with nothing after it reads to the model as an
    // instruction about the unit — a different request from not saying.
    const msg = composeFormMessage(TASK_FORM, { what: 'Leaky faucet', unit: '', priority: '' });
    expect(msg).toContain('What needs doing: Leaky faucet');
    expect(msg).not.toContain('Which unit');
    expect(msg).not.toContain('Priority');
  });

  it('leads with the action the button names, so the ask is unambiguous', () => {
    const msg = composeFormMessage(TASK_FORM, { what: 'Leaky faucet' });
    expect(msg.split('\n')[0]).toBe('Create task:');
  });

  it('sends nothing at all when every field is blank', () => {
    expect(composeFormMessage(TASK_FORM, {})).toBe('');
  });

  it('trims, so whitespace does not pass as an answer', () => {
    expect(composeFormMessage(TASK_FORM, { what: '   ' })).toBe('');
  });
});

describe('FormCard', () => {
  it('prefills what the assistant already knew and leaves the rest empty', () => {
    // The prefill is the point of a generated form; a plausible value in a
    // field the user never gave is a fabricated fact they will submit
    // without re-reading, so blank has to stay blank.
    render(<FormCard data={TASK_FORM} onSubmit={vi.fn()} testIdPrefix="f" />);
    expect(screen.getByTestId('f-input-what')).toHaveValue('Leaky faucet');
    expect(screen.getByTestId('f-input-unit')).toHaveValue('');
  });

  it('sends the typed answers as one message', () => {
    const onSubmit = vi.fn();
    render(<FormCard data={TASK_FORM} onSubmit={onSubmit} testIdPrefix="f" />);
    fireEvent.change(screen.getByTestId('f-input-unit'), { target: { value: '4B' } });
    fireEvent.change(screen.getByTestId('f-input-priority'), { target: { value: 'High' } });
    fireEvent.click(screen.getByTestId('f-submit'));

    expect(onSubmit).toHaveBeenCalledTimes(1);
    const msg = onSubmit.mock.calls[0][0] as string;
    expect(msg).toContain('Which unit: 4B');
    expect(msg).toContain('Priority: High');
  });

  it('cannot be submitted twice', () => {
    // ⚠ A live button after a send reads to the model as the user asking
    // again, which is how one leaky faucet becomes two work orders.
    const onSubmit = vi.fn();
    render(<FormCard data={TASK_FORM} onSubmit={onSubmit} testIdPrefix="f" />);
    const button = screen.getByTestId('f-submit');
    fireEvent.click(button);
    fireEvent.click(button);
    fireEvent.click(button);
    expect(onSubmit).toHaveBeenCalledTimes(1);
    expect(button).toBeDisabled();
    expect(button).toHaveTextContent('Sent');
  });

  it('refuses to send an entirely empty form', () => {
    const onSubmit = vi.fn();
    const blank: FormData = { ...TASK_FORM, fields: [{ name: 'unit', label: 'Unit', kind: 'text' }] };
    render(<FormCard data={blank} onSubmit={onSubmit} testIdPrefix="f" />);
    fireEvent.click(screen.getByTestId('f-submit'));
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it('opens a choice on nothing chosen, not on the first option', () => {
    // A select that defaults to "Low" has answered a question the user never
    // read, and it looks exactly like a deliberate choice in the message.
    render(<FormCard data={TASK_FORM} onSubmit={vi.fn()} testIdPrefix="f" />);
    expect(screen.getByTestId('f-input-priority')).toHaveValue('');
  });

  it('renders nothing when there are no fields', () => {
    const { container } = render(
      <FormCard data={{ ...TASK_FORM, fields: [] }} onSubmit={vi.fn()} testIdPrefix="f" />,
    );
    expect(container).toBeEmptyDOMElement();
  });

  it('says plainly that nothing is saved until it is sent', () => {
    // The card is an ASK. A form in a property-management app looks like it
    // writes, and the one thing that stops a user hesitating over it is
    // saying so on the card.
    render(<FormCard data={TASK_FORM} onSubmit={vi.fn()} testIdPrefix="f" />);
    expect(screen.getByTestId('f-root')).toHaveTextContent('Nothing is saved until you send this');
  });
});
