import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import posthog from 'posthog-js'
import { PostHogProvider } from 'posthog-js/react'
import { App } from './App'
import { initPostHog, posthogConfigured } from './services/analytics'
import { ErrorBoundary, initSentry } from './services/sentry'
// The same faces as occupella.com, self-hosted so they render offline and
// are not a third-party request: IBM Plex Sans for the UI, Fraunces for page
// titles and the wordmark, IBM Plex Mono for IDs, amounts and code. Imported
// before globals.css so the font tokens resolve to the loaded faces.
import '@fontsource-variable/ibm-plex-sans'
import '@fontsource-variable/fraunces/opsz.css'
import '@fontsource/ibm-plex-mono/400.css'
import '@fontsource/ibm-plex-mono/500.css'
import './styles/globals.css'

// Composio OAuth callback — short-circuit before React mounts. The
// connectors flow opens Composio's hosted consent page in a popup and
// passes this URL as ``callbackUrl``; Composio redirects the popup
// here on success, and ``window.close()`` returns the user to the
// parent Occupella tab where the poll loop picks up the new connection.
if (window.location.pathname === '/oauth/callback') {
  document.body.innerHTML =
    '<div style="font: 14px system-ui, -apple-system, sans-serif; padding: 48px; text-align: center; color: #666">Connection successful. You can close this tab.</div>'
  setTimeout(() => window.close(), 250)
} else {
  // Initialize Sentry before React mounts so even render-time errors are
  // captured. Returns immediately when VITE_SENTRY_DSN is not set.
  initSentry()

  // Initialize PostHog before rendering. Returns null when
  // VITE_PUBLIC_POSTHOG_KEY isn't set — the app renders without analytics.
  initPostHog()

  const root = createRoot(document.getElementById('root')!)

  const appTree = (
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  )

  if (posthogConfigured) {
    root.render(
      <StrictMode>
        <PostHogProvider client={posthog}>{appTree}</PostHogProvider>
      </StrictMode>,
    )
  } else {
    root.render(<StrictMode>{appTree}</StrictMode>)
  }
}
