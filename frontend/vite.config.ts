/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

import pkg from './package.json' with { type: 'json' }

/**
 * The build a user is actually running.
 *
 * ⚠ Prefers the COMMIT, not `package.json`'s version. That number has read
 * `0.1.0` since the repo started and would be identical on every deploy — a
 * version field that never changes cannot distinguish "already fixed" from
 * "still broken", which is the one question a bug report has to answer.
 * Vercel injects `VERCEL_GIT_COMMIT_SHA`; a local build falls back to the
 * package version plus `-dev` so the string never silently claims to be a
 * release. Read at CONFIG time — `process` does not exist in the bundle.
 */
const commit = process.env.VERCEL_GIT_COMMIT_SHA ?? process.env.GIT_COMMIT_SHA ?? ''
const appVersion = commit ? commit.slice(0, 12) : `${pkg.version}-dev`

export default defineConfig({
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
    // The FULL sha, so Sentry's release tag matches the backend's (it tags
    // RENDER_GIT_COMMIT); empty for a local build.
    __APP_RELEASE__: JSON.stringify(commit),
    // Vercel's production / preview / development. Empty outside Vercel.
    __VERCEL_ENV__: JSON.stringify(process.env.VERCEL_ENV ?? ''),
  },
  plugins: [tailwindcss(), react()],
  test: {
    // jsdom for the Phase A.2.5 component tests (ActionChainModal,
    // InboxCard). Service-layer tests still run fine under jsdom;
    // they avoid window/document on purpose.
    environment: 'jsdom',
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
    setupFiles: ['./src/test/setup.ts'],
    // ⚠ A WEST-OF-UTC ZONE, PINNED ON PURPOSE — the suite could not see a
    // whole class of bug without it. CI runs in UTC, where
    // `new Date('2027-03-18').toLocaleDateString()` renders 18 March and looks
    // perfect; in every zone this product's users are actually in, the same
    // line renders 17 March. A lease ending on the 18th was shown as ending on
    // the 17th on six live surfaces (coverage items 65/344) and nothing failed,
    // because the only host that ever ran the tests agreed with the bug.
    //
    // Los Angeles rather than something exotic: it is a real user zone, it is
    // west of UTC so date-only values shift, and it observes DST so a fix that
    // only works at a fixed offset is caught too.
    env: { TZ: 'America/Los_Angeles' },
  },
  server: {
    port: 5173,
    host: '0.0.0.0',
    allowedHosts: true,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
      },
      '/browser/ws': {
        target: 'ws://localhost:8000',
        ws: true,
      },
    },
  },
})
