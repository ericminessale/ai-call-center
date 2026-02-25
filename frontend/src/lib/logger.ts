/**
 * Development-only logger. All output is silenced in production builds.
 * Uses Vite's import.meta.env.DEV which is true during `npm run dev`
 * and false in production bundles.
 */

const isDev = import.meta.env.DEV;

/* eslint-disable no-console */
export const logger = {
  debug: (...args: unknown[]) => {
    if (isDev) console.log(...args);
  },
  info: (...args: unknown[]) => {
    if (isDev) console.info(...args);
  },
  warn: (...args: unknown[]) => {
    if (isDev) console.warn(...args);
  },
  error: (...args: unknown[]) => {
    // Errors always log, even in production
    console.error(...args);
  },
};
