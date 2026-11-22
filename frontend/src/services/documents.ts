/**
 * Documents & Links service client (Settings → Documents & Links).
 *
 * Three small stores behind one settings page:
 *   - Links      — the caller's own URLs (application, scheduling). Per user.
 *   - Templates  — company-shared message templates.
 *   - Documents  — company-shared lease/addendum PDFs (upload + signed download).
 *
 * In demo mode these return static fixtures and writes are no-ops, mirroring the
 * other service clients — the page is fully browsable without a backend.
 */

import { apiFetch, ApiError } from './api';
import { demoMode } from './auth';

export interface UserLink {
  id: string;
  label: string;
  url: string;
}

export interface MessageTemplate {
  id: string;
  name: string;
  body: string;
}

export type DocumentKind = 'lease' | 'addendum' | 'other';

export interface CompanyDocument {
  id: string;
  name: string;
  kind: DocumentKind;
  size_bytes: number;
}

// ── demo fixtures ────────────────────────────────────────────────────

const DEMO_LINKS: UserLink[] = [
  { id: 'demo-link-1', label: 'Scheduling link', url: 'https://calendly.com/you/tour' },
  { id: 'demo-link-2', label: 'Application link', url: 'https://apply.example.com' },
];
const DEMO_TEMPLATES: MessageTemplate[] = [
  {
    id: 'demo-tpl-1',
    name: 'Tour reply',
    body: 'Hi {first_name}, thanks for your interest in {property}! Grab a tour time here: {scheduling_link}.',
  },
];
const DEMO_DOCUMENTS: CompanyDocument[] = [
  { id: 'demo-doc-1', name: 'Standard Residential Lease', kind: 'lease', size_bytes: 2_100_000 },
  { id: 'demo-doc-2', name: 'Pet Addendum', kind: 'addendum', size_bytes: 180_000 },
];

async function readError(res: Response, fallback: string): Promise<ApiError> {
  let detail = fallback;
  try {
    const j = (await res.json()) as { detail?: string };
    if (j?.detail) detail = j.detail;
  } catch {
    /* keep the fallback */
  }
  return new ApiError(res.status, detail);
}

// ── links ────────────────────────────────────────────────────────────

export async function listLinks(): Promise<UserLink[]> {
  if (demoMode) return [...DEMO_LINKS];
  const res = await apiFetch('/api/v1/links');
  if (!res.ok) throw await readError(res, 'Could not load links.');
  return (await res.json()) as UserLink[];
}

export async function createLink(label: string, url: string): Promise<UserLink> {
  if (demoMode) return { id: `demo-${Date.now()}`, label, url };
  const res = await apiFetch('/api/v1/links', {
    method: 'POST',
    body: JSON.stringify({ label, url }),
  });
  if (!res.ok) throw await readError(res, 'Could not save the link.');
  return (await res.json()) as UserLink;
}

export async function deleteLink(id: string): Promise<void> {
  if (demoMode) return;
  const res = await apiFetch(`/api/v1/links/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!res.ok) throw await readError(res, 'Could not delete the link.');
}

// ── templates ────────────────────────────────────────────────────────

export async function listTemplates(): Promise<MessageTemplate[]> {
  if (demoMode) return [...DEMO_TEMPLATES];
  const res = await apiFetch('/api/v1/templates');
  if (!res.ok) throw await readError(res, 'Could not load templates.');
  return (await res.json()) as MessageTemplate[];
}

export async function createTemplate(name: string, body: string): Promise<MessageTemplate> {
  if (demoMode) return { id: `demo-${Date.now()}`, name, body };
  const res = await apiFetch('/api/v1/templates', {
    method: 'POST',
    body: JSON.stringify({ name, body }),
  });
  if (!res.ok) throw await readError(res, 'Could not save the template.');
  return (await res.json()) as MessageTemplate;
}

export async function deleteTemplate(id: string): Promise<void> {
  if (demoMode) return;
  const res = await apiFetch(`/api/v1/templates/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!res.ok) throw await readError(res, 'Could not delete the template.');
}

// ── documents ────────────────────────────────────────────────────────

export async function listDocuments(): Promise<CompanyDocument[]> {
  if (demoMode) return [...DEMO_DOCUMENTS];
  const res = await apiFetch('/api/v1/documents');
  if (!res.ok) throw await readError(res, 'Could not load documents.');
  return (await res.json()) as CompanyDocument[];
}

/**
 * Upload a PDF. Uses a multipart request (not JSON), so it calls ``apiFetch``
 * with a FormData body — ``apiFetch`` leaves the Content-Type unset for
 * FormData so the browser sets the multipart boundary.
 */
export async function uploadDocument(
  file: File,
  name: string,
  kind: DocumentKind,
): Promise<CompanyDocument> {
  if (demoMode) {
    return { id: `demo-${Date.now()}`, name, kind, size_bytes: file.size };
  }
  const form = new FormData();
  form.append('file', file);
  form.append('name', name);
  form.append('kind', kind);
  const res = await apiFetch('/api/v1/documents', { method: 'POST', body: form });
  if (!res.ok) throw await readError(res, 'Could not upload the document.');
  return (await res.json()) as CompanyDocument;
}

/** Get a short-lived signed URL to view/download a document, then open it. */
export async function getDocumentUrl(id: string): Promise<string> {
  if (demoMode) return '#';
  const res = await apiFetch(`/api/v1/documents/${encodeURIComponent(id)}/download`);
  if (!res.ok) throw await readError(res, 'Could not open the document.');
  return ((await res.json()) as { url: string }).url;
}

export async function deleteDocument(id: string): Promise<void> {
  if (demoMode) return;
  const res = await apiFetch(`/api/v1/documents/${encodeURIComponent(id)}`, { method: 'DELETE' });
  if (!res.ok) throw await readError(res, 'Could not delete the document.');
}
