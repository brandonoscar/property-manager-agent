/**
 * Artifact download card — files a code-execution run produced.
 *
 * ``run_computation`` writes files to its sandbox OUTPUT_DIR; the backend
 * persists them company-scoped and auto-emits an ``artifacts`` event carrying
 * signed download links (the tabular/map precedent — the model never relays the
 * URL). This renders each as a download button inline in the conversation
 * stream, in the same band a table/bubble occupies.
 *
 * Deliberately simple: one bordered card, a row per file with its name, size,
 * and a Download button that opens the signed URL. The links are short-lived
 * (server-signed); nothing here holds a credential.
 */

import { Download, FileDown } from 'lucide-react';

import type { ArtifactsData } from './types';

interface ArtifactCardProps {
  data: ArtifactsData;
  /** Test hook prefix for the rendered card. */
  testIdPrefix?: string;
}

function humanSize(bytes: number | null | undefined): string | null {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

export function ArtifactCard({ data, testIdPrefix = 'artifacts' }: ArtifactCardProps) {
  const files = (data?.artifacts ?? []).filter((a) => a && a.url);
  if (files.length === 0) return null;

  return (
    <div
      data-testid={`${testIdPrefix}-root`}
      data-count={files.length}
      className="my-3 max-w-[420px] rounded-[var(--r-md)] overflow-hidden"
      style={{ background: 'var(--canvas-elevated)', border: '1px solid var(--line)' }}
    >
      {files.map((f, i) => {
        const size = humanSize(f.size);
        return (
          <div
            key={`${f.name}-${i}`}
            data-testid={`${testIdPrefix}-row`}
            className="flex items-center gap-3 px-3.5 py-2.5"
            style={{ borderTop: i === 0 ? undefined : '1px solid var(--line)' }}
          >
            <FileDown size={16} strokeWidth={1.75} style={{ color: 'var(--ink-muted)', flexShrink: 0 }} />
            <div className="min-w-0 flex-1">
              <div
                className="text-[13px] font-medium truncate"
                style={{ color: 'var(--ink)' }}
                title={f.name}
              >
                {f.name}
              </div>
              {size ? (
                <div className="text-[12px] tabular-nums" style={{ color: 'var(--ink-subtle)' }}>
                  {size}
                </div>
              ) : null}
            </div>
            <a
              data-testid={`${testIdPrefix}-download`}
              href={f.url}
              download={f.name}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1.5 h-8 px-3 rounded-[var(--r-sm)] border text-[13px] font-medium transition-colors duration-150"
              style={{ background: 'var(--iris)', color: 'white', borderColor: 'var(--iris)' }}
              onMouseEnter={(e) => {
                const el = e.currentTarget as HTMLElement;
                el.style.background = 'var(--iris-hover)';
                el.style.borderColor = 'var(--iris-hover)';
              }}
              onMouseLeave={(e) => {
                const el = e.currentTarget as HTMLElement;
                el.style.background = 'var(--iris)';
                el.style.borderColor = 'var(--iris)';
              }}
            >
              <Download size={13} strokeWidth={2} />
              Download
            </a>
          </div>
        );
      })}
    </div>
  );
}
