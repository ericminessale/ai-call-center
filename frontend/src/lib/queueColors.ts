import { QueueConfig } from '../types/callcenter';

/**
 * Shared queue badge color utility.
 * Colors are derived from a slug hash so the same queue always gets the same color.
 * Tuned to match the "OPERATOR" palette — desaturated, warm, readable over the canvas.
 */

interface QueueBadgeColor {
  bg: string;    // Tailwind bg class
  text: string;  // Tailwind text class
  pill: string;  // Combined classes for a pill badge
  dot: string;   // Matching dot color (hex)
}

// Brand-palette only: blue / turquoise / gold / neutrals.
// Fuchsia is reserved per brand 10% rule. Queues are internal data buckets, so we
// intentionally rotate through non-accent brand colors + neutrals.
const PALETTE: QueueBadgeColor[] = [
  { bg: 'bg-ai/10',     text: 'text-ai-soft',     pill: 'bg-ai/10 text-ai-soft border border-ai/25',           dot: '#40E0D0' },
  { bg: 'bg-info/10',   text: 'text-info-soft',   pill: 'bg-info/10 text-info-soft border border-info/25',     dot: '#044EF4' },
  { bg: 'bg-wait/10',   text: 'text-wait-soft',   pill: 'bg-wait/10 text-wait-soft border border-wait/25',     dot: '#FFD700' },
  { bg: 'bg-live/10',   text: 'text-live-soft',   pill: 'bg-live/10 text-live-soft border border-live/25',     dot: '#22c55e' },
  { bg: 'bg-canvas-elevated', text: 'text-ink-muted', pill: 'bg-canvas-elevated text-ink-muted border border-rule', dot: '#a0a0aa' },
];

/** Simple string hash — deterministic for the same slug. */
function hashSlug(slug: string): number {
  let hash = 0;
  for (let i = 0; i < slug.length; i++) {
    hash = ((hash << 5) - hash + slug.charCodeAt(i)) | 0;
  }
  return Math.abs(hash);
}

/** Get a consistent badge color object for a given queue slug. */
export function getQueueBadgeColor(slug: string): QueueBadgeColor {
  return PALETTE[hashSlug(slug) % PALETTE.length];
}

/** Resolve a display name from the queue configs list, falling back to slug. */
export function getQueueDisplayName(
  slug: string,
  configs: QueueConfig[] | undefined,
): string {
  if (!configs) return slug;
  const found = configs.find((q) => q.slug === slug);
  return found?.display_name ?? slug;
}
