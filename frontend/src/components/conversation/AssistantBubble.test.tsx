/**
 * PR #125 — markdown rendering inside AssistantBubble.
 *
 * The Claude extension's UI audit flagged that the model's responses
 * were being dumped as raw text via ``whitespace-pre-wrap``: ``**bold**``
 * showed up literally as two asterisks around the word, and bullet
 * lists rendered as ``*`` characters at the start of lines. PR #125
 * pipes assistant ``content`` through ``react-markdown`` with a
 * conservative component override map. Tests below pin the
 * before/after for each of the audit's specific complaints.
 */

import { describe, expect, it } from 'vitest';
import { render, screen } from '@testing-library/react';

import { AssistantBubble } from './AssistantBubble';


describe('PR #125 AssistantBubble — markdown rendering', () => {
  it('renders **bold** as a <strong> element, not literal asterisks', () => {
    render(
      <AssistantBubble
        content="There's a **mouse in the house**!"
        streaming={false}
      />,
    );
    const strong = screen.getByTestId('md-strong');
    expect(strong.tagName).toBe('STRONG');
    expect(strong.textContent).toBe('mouse in the house');
    // The literal "**" characters must NOT appear anywhere in the
    // rendered output.
    const bubble = screen.getByTestId('assistant-bubble');
    expect(bubble.textContent).not.toContain('**');
  });

  it('renders bullet lists as <ul><li> instead of literal "*" lines', () => {
    render(
      <AssistantBubble
        content={'Things to do:\n\n* Pick up keys\n* Call vendor\n* File report'}
        streaming={false}
      />,
    );
    const list = screen.getByTestId('md-ul');
    expect(list.tagName).toBe('UL');
    // Three items, no raw asterisks at the start of any line.
    expect(list.querySelectorAll('li')).toHaveLength(3);
    expect(list.textContent).toContain('Pick up keys');
    expect(list.textContent).toContain('Call vendor');
    expect(list.textContent).toContain('File report');
  });

  it('renders numbered lists as <ol><li>', () => {
    render(
      <AssistantBubble
        content={'Steps:\n\n1. First\n2. Second\n3. Third'}
        streaming={false}
      />,
    );
    const list = screen.getByTestId('md-ol');
    expect(list.tagName).toBe('OL');
    expect(list.querySelectorAll('li')).toHaveLength(3);
  });

  it('renders *italic* as <em>', () => {
    render(
      <AssistantBubble content="Use *bold* for emphasis" streaming={false} />,
    );
    const em = screen.getByText('bold');
    expect(em.tagName).toBe('EM');
    // No literal "*" rendered around it.
    const bubble = screen.getByTestId('assistant-bubble');
    expect(bubble.textContent).not.toMatch(/\*bold\*/);
  });

  it('renders [link text](url) as an <a target="_blank">', () => {
    render(
      <AssistantBubble
        content="See [the docs](https://example.com/docs) for more."
        streaming={false}
      />,
    );
    const link = screen.getByRole('link', { name: /the docs/i });
    expect(link.getAttribute('href')).toBe('https://example.com/docs');
    expect(link.getAttribute('target')).toBe('_blank');
    // Security: external links must carry noopener noreferrer.
    expect(link.getAttribute('rel')).toContain('noopener');
    expect(link.getAttribute('rel')).toContain('noreferrer');
  });

  it('renders inline `code` as a <code> chip', () => {
    render(
      <AssistantBubble
        content="Run `npm install` to set up."
        streaming={false}
      />,
    );
    const code = screen.getByText('npm install');
    expect(code.tagName).toBe('CODE');
  });

  it('renders fenced code blocks as <pre><code>', () => {
    render(
      <AssistantBubble
        content={'```ts\nconst x = 1;\n```'}
        streaming={false}
      />,
    );
    const code = screen.getByText(/const x = 1;/);
    expect(code.tagName).toBe('CODE');
    // Fenced code should be inside a <pre> for block layout.
    expect(code.parentElement?.tagName).toBe('PRE');
  });

  it('renders plain text without markdown noise (back-compat)', () => {
    render(
      <AssistantBubble
        content="Just a plain sentence with no markdown."
        streaming={false}
      />,
    );
    expect(screen.getByText(/Just a plain sentence/)).toBeInTheDocument();
    // No <strong> or <ul> elements when there's no markdown syntax.
    expect(screen.queryByTestId('md-strong')).toBeNull();
    expect(screen.queryByTestId('md-ul')).toBeNull();
  });

  it('keeps the streaming cursor visible while streaming', () => {
    const { container } = render(
      <AssistantBubble content="Working" streaming={true} />,
    );
    // The cursor is the trailing aria-hidden span with the pulse
    // animation; pin its presence by the inline style we set.
    const cursors = container.querySelectorAll('span[aria-hidden]');
    expect(cursors.length).toBeGreaterThan(0);
  });

  it('omits the cursor once streaming is done', () => {
    const { container } = render(
      <AssistantBubble content="Done." streaming={false} />,
    );
    // No pulsing cursor span. (Markdown rendering may still produce
    // some aria-hidden spans for inline elements, but the pulse-dot
    // animation is unique to the cursor.)
    const styled = Array.from(container.querySelectorAll('span'));
    const pulsing = styled.filter((s) =>
      (s.getAttribute('style') ?? '').includes('pulse-dot'),
    );
    expect(pulsing).toHaveLength(0);
  });

  it('does NOT render raw HTML (XSS guard — react-markdown default)', () => {
    render(
      <AssistantBubble
        content='<script>alert("xss")</script> safe'
        streaming={false}
      />,
    );
    const bubble = screen.getByTestId('assistant-bubble');
    // The script tag should be rendered as text, not as a live element.
    expect(bubble.querySelector('script')).toBeNull();
  });
});


describe('PR #129 AssistantBubble — chat variant chrome', () => {
  it('renders the Occupella avatar + role label in chat variant', () => {
    render(
      <AssistantBubble
        content="OK, here are the open requests."
        streaming={false}
        variant="chat"
      />,
    );
    // Avatar + role label — both are chat-variant-only.
    expect(screen.getByTestId('assistant-avatar')).toBeInTheDocument();
    const bubble = screen.getByTestId('assistant-bubble');
    expect(bubble.textContent).toContain('Occupella');
  });

  it('omits the avatar + role label in inbox variant (typography-only)', () => {
    render(
      <AssistantBubble
        content="OK, here are the open requests."
        streaming={false}
        variant="inbox"
      />,
    );
    // Inbox stays typography-only — no avatar, no role label.
    expect(screen.queryByTestId('assistant-avatar')).toBeNull();
    const bubble = screen.getByTestId('assistant-bubble');
    expect(bubble.textContent).not.toContain('Occupella');
  });
});