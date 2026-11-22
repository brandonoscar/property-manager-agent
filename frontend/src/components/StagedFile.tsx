/**
 * The file you are about to send, shown before it goes.
 *
 * Until this existed, picking a file uploaded it instantly: the first time you
 * saw WHICH file you had chosen was in the transcript, after it had cost a
 * request and a chat turn. That is tolerable for a button you deliberately
 * clicked and bad for a drop, which is easy to do by accident — dragging
 * anything across the window is one slip away from an upload.
 *
 * So both doors stage here instead, and the person confirms. For an image the
 * thumbnail is the whole point: somebody with five screenshots cannot tell
 * `Screenshot 2026-09-08 at 14.03.11.png` from the next one by NAME, and that
 * is exactly the moment they need to.
 */

import { useEffect, useState } from 'react';

/** Human-readable size. Bytes are what the API cares about; nobody reads them. */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const kb = bytes / 1024;
  if (kb < 1024) return `${Math.round(kb)} KB`;
  const mb = kb / 1024;
  // One decimal below 10 MB, none above — "1.4 MB" is useful, "14.3 MB" is noise.
  return mb < 10 ? `${mb.toFixed(1)} MB` : `${Math.round(mb)} MB`;
}

/** Whether the browser can render this file as a thumbnail. */
export function isPreviewableImage(type: string): boolean {
  // ⚠ Deliberately NOT `type.startsWith('image/')`. The upload accepts a
  // specific set, and an `image/tiff` or `image/heic` that the accept list
  // rejects would render a BROKEN thumbnail here — a preview that shows a
  // torn-image icon reads as "the file is corrupt" rather than "we don't take
  // this kind", which sends somebody to re-export a perfectly good photo.
  return type === 'image/png' || type === 'image/jpeg' || type === 'image/webp';
}

export function StagedFile({
  file,
  disabled = false,
  onRemove,
}: {
  file: File;
  disabled?: boolean;
  onRemove: () => void;
}) {
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!isPreviewableImage(file.type)) {
      setPreviewUrl(null);
      return;
    }
    // ⚠ An object URL pins the whole file in memory until it is revoked, so
    // the cleanup is not tidiness — staging ten 20 MB photos in a session
    // without it holds 200 MB for as long as the tab is open.
    const url = URL.createObjectURL(file);
    setPreviewUrl(url);
    return () => URL.revokeObjectURL(url);
  }, [file]);

  return (
    <div
      className="flex items-center gap-2.5 px-2.5 py-2 rounded-[var(--r-sm)] border"
      style={{ borderColor: 'var(--line)', background: 'var(--canvas-elevated)' }}
      data-testid="staged-file"
    >
      {previewUrl ? (
        <img
          src={previewUrl}
          // The filename is already beside it, so repeating it here would make
          // a screen reader say it twice. What the sighted user gets from the
          // thumbnail is "is this the right picture", which alt text cannot
          // give them anyway — so it says what the image IS.
          alt="Preview of the attached image"
          className="rounded-[var(--r-xs)] object-cover shrink-0"
          style={{ width: 34, height: 34, background: 'var(--canvas)' }}
        />
      ) : (
        <span
          aria-hidden="true"
          className="flex items-center justify-center rounded-[var(--r-xs)] shrink-0 text-[12px] font-mono font-medium"
          style={{
            width: 34,
            height: 34,
            background: 'var(--canvas)',
            border: '1px solid var(--line)',
            color: 'var(--ink-muted)',
          }}
        >
          PDF
        </span>
      )}

      <div className="flex flex-col min-w-0">
        {/* `break-all` rather than truncate: a filename is how somebody
            recognises their own file, and the distinguishing part is usually
            at the END (a date, a unit number, "-final"). */}
        <span
          className="text-[13px] font-medium break-all"
          style={{ color: 'var(--ink)' }}
        >
          {file.name}
        </span>
        <span className="text-[12px] tabular-nums" style={{ color: 'var(--ink-subtle)' }}>
          {formatBytes(file.size)} · ready to send
        </span>
      </div>

      <button
        type="button"
        onClick={onRemove}
        disabled={disabled}
        aria-label={`Remove ${file.name}`}
        className="ml-auto shrink-0 px-2 py-1 rounded-[var(--r-xs)] text-[12px] font-medium transition-colors disabled:opacity-40"
        style={{ color: 'var(--ink-muted)' }}
      >
        Remove
      </button>
    </div>
  );
}
