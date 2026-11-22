/// <reference types="vite/client" />

declare module '*.css' {
  const content: string;
  export default content;
}

// Fontsource packages are CSS side-effect imports with no bundled types;
// declare them so the imports in main.tsx type-check.
declare module '@fontsource-variable/ibm-plex-sans';

/**
 * The build this bundle was made from — a short commit sha in CI, or
 * `<pkg version>-dev` locally. Substituted by `define` in vite.config.ts, so
 * it is a literal in the output and exists under `vitest` too.
 */
declare const __APP_VERSION__: string;

/** The full commit sha this bundle was built from, or `""` for a local build. */
declare const __APP_RELEASE__: string;

/** Vercel's build environment (`production`, `preview`, `development`), or `""`. */
declare const __VERCEL_ENV__: string;
