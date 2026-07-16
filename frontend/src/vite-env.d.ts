/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_WS_URL?: string;
  readonly VITE_SIGNALWIRE_HOST?: string;
  readonly VITE_FABRIC_APPLICATION_ID?: string;
  readonly VITE_SW_DEBUG?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
