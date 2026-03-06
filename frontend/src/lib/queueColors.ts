import { QueueConfig } from '../types/callcenter';

/**
 * Shared queue badge color utility.
 * Colors are derived from a slug hash so the same queue always gets the same color.
 */

interface QueueBadgeColor {
  bg: string;    // Tailwind bg class  e.g. "bg-blue-900/40"
  text: string;  // Tailwind text class e.g. "text-blue-300"
  pill: string;  // Combined classes for a pill badge
}

const PALETTE: QueueBadgeColor[] = [
  { bg: 'bg-blue-900/40',    text: 'text-blue-300',    pill: 'bg-blue-900/40 text-blue-300' },
  { bg: 'bg-emerald-900/40', text: 'text-emerald-300', pill: 'bg-emerald-900/40 text-emerald-300' },
  { bg: 'bg-amber-900/40',   text: 'text-amber-300',   pill: 'bg-amber-900/40 text-amber-300' },
  { bg: 'bg-violet-900/40',  text: 'text-violet-300',  pill: 'bg-violet-900/40 text-violet-300' },
  { bg: 'bg-rose-900/40',    text: 'text-rose-300',    pill: 'bg-rose-900/40 text-rose-300' },
  { bg: 'bg-cyan-900/40',    text: 'text-cyan-300',    pill: 'bg-cyan-900/40 text-cyan-300' },
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
