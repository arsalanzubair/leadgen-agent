/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** Base URL for the real backend. Unused while services/index.ts uses the mock. */
  readonly VITE_API_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
