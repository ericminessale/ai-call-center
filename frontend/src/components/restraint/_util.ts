/**
 * Restraint design-system — internal utilities.
 *
 * These primitives are PRESENTATIONAL only: props in, markup out. No data
 * fetching, no hooks beyond trivial UI state. Colors come exclusively from the
 * mapped Tailwind tokens (text-ink / border-rule / bg-canvas-* / status-* /
 * sw-fuchsia / ai) — never raw hexes.
 */

/** Tiny conditional-class joiner (no clsx dependency). */
export function cx(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(' ');
}

/**
 * Semantic status used across dots, chips, KPI values, etc.
 * - `success` → green   (--ok)
 * - `warning` → amber   (--warn)
 * - `error`   → red     (--bad)
 * - `neutral` → silent gray (--line2): the healthy/normal state, visually quiet.
 */
export type RestraintStatus = 'success' | 'warning' | 'error' | 'neutral';

/** Maps a status to its dot background token. Healthy state stays silent. */
export const STATUS_DOT_BG: Record<RestraintStatus, string> = {
  success: 'bg-status-success',
  warning: 'bg-status-warning',
  error: 'bg-status-error',
  neutral: 'bg-rule-strong',
};

/** The deliberate AI signal glyph (U+2726, matches the design mockups). Pair with `text-ai` (turquoise). */
export const AI_GLYPH = '✦';
