/**
 * ArtifactCard — code-execution download buttons.
 *
 * Contract: renders a Download link per artifact pointing at its signed URL,
 * shows a human-readable size, and drops entries with no URL.
 */

import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';

import { ArtifactCard } from './ArtifactCard';
import type { ArtifactsData } from './types';

describe('ArtifactCard', () => {
  it('renders a download link per artifact pointing at its signed URL', () => {
    const data: ArtifactsData = {
      artifacts: [
        { name: 'owner-statement.csv', size: 2048, url: 'https://signed/owner-statement.csv?t=abc' },
      ],
    };
    render(<ArtifactCard data={data} testIdPrefix="t" />);
    const link = screen.getByTestId('t-download') as HTMLAnchorElement;
    expect(link.getAttribute('href')).toBe('https://signed/owner-statement.csv?t=abc');
    expect(link.getAttribute('download')).toBe('owner-statement.csv');
    expect(screen.getByText('owner-statement.csv')).toBeInTheDocument();
    expect(screen.getByText('2.0 KB')).toBeInTheDocument();
  });

  it('drops artifacts with no URL and renders nothing when all are urlless', () => {
    const { container } = render(
      <ArtifactCard data={{ artifacts: [{ name: 'x.bin', size: 1, url: '' }] }} testIdPrefix="t" />,
    );
    expect(container.firstChild).toBeNull();
  });

  it('renders multiple files each with its own button', () => {
    const data: ArtifactsData = {
      artifacts: [
        { name: 'a.csv', size: 10, url: 'https://signed/a' },
        { name: 'b.pdf', size: 20, url: 'https://signed/b' },
      ],
    };
    render(<ArtifactCard data={data} testIdPrefix="t" />);
    expect(screen.getAllByTestId('t-download')).toHaveLength(2);
    expect(screen.getByTestId('t-root').getAttribute('data-count')).toBe('2');
  });
});
