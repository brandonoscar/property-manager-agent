/**
 * The backend's own origin, for URLs a PMS calls directly (webhooks).
 *
 * Only `/api/*` goes through the Vercel rewrite, so a webhook URL built from the
 * page's origin would reach Vercel, not the API (gotcha 135). Set
 * `VITE_RENDER_BACKEND_URL` to point it at a preview or dev backend.
 */
export function backendOrigin(): string {
  const explicit = import.meta.env.VITE_RENDER_BACKEND_URL as string | undefined;
  return (explicit || 'https://your-api.onrender.com').replace(/\/$/, '');
}
