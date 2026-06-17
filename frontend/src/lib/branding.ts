// Runtime white-label branding (IMP-02).
//
// The console's visual system flows through CSS variables (src/index.css):
// component tokens (buttons, links, tabs, focus rings) reference --sw-*
// directly, and Tailwind brand utilities resolve through the --sw-*-rgb
// channel triplets. Overriding those variables at the document root
// rebrands every surface at runtime — nothing is rebuilt or redeployed.
//
// The SignalWire logo SVG itself is never recolored (brand rule); custom
// brands supply their own logo_url instead.

export interface Branding {
  product_name: string | null;
  logo_url: string | null;
  color_primary: string | null; // structure: buttons, tab indicator, focus
  color_accent: string | null; // emphasis: stats, table headers, link hover
  color_highlight: string | null; // links, active tab
  enabled: boolean;
}

const DEFAULT_TITLE = 'SignalWire Call Center';

// Every variable applyBranding may touch — cleared first on each apply so
// resetting a field in the admin UI reverts that surface to stock.
const MANAGED_VARS = [
  '--sw-blue',
  '--sw-blue-rgb',
  '--sw-fuchsia',
  '--sw-fuchsia-rgb',
  '--sw-turquoise',
  '--sw-turquoise-rgb',
  '--btn-primary-hover',
  '--btn-primary-active',
  '--input-focus-ring',
  '--shadow-glow-blue',
  '--shadow-glow-fuchsia',
];

function hexToTriplet(hex: string): string | null {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return null;
  const n = parseInt(m[1], 16);
  return `${(n >> 16) & 255} ${(n >> 8) & 255} ${n & 255}`;
}

/** Multiply each channel by `factor` (<1 darkens) — for hover/active shades. */
function shade(hex: string, factor: number): string {
  const m = /^#([0-9a-f]{6})$/i.exec(hex.trim());
  if (!m) return hex;
  const n = parseInt(m[1], 16);
  const ch = (v: number) => Math.max(0, Math.min(255, Math.round(v * factor)));
  const r = ch((n >> 16) & 255);
  const g = ch((n >> 8) & 255);
  const b = ch(n & 255);
  return `#${((r << 16) | (g << 8) | b).toString(16).padStart(6, '0')}`;
}

/**
 * Apply (or clear) white-label branding on the document root.
 * Pass null/undefined or a disabled config to restore stock SignalWire.
 */
export function applyBranding(branding: Branding | null | undefined): void {
  const root = document.documentElement;
  MANAGED_VARS.forEach((v) => root.style.removeProperty(v));
  document.title = DEFAULT_TITLE;

  if (!branding || !branding.enabled) return;

  if (branding.product_name) {
    document.title = branding.product_name;
  }

  const set = (name: string, value: string) => root.style.setProperty(name, value);

  if (branding.color_primary) {
    const triplet = hexToTriplet(branding.color_primary);
    if (triplet) {
      set('--sw-blue', branding.color_primary);
      set('--sw-blue-rgb', triplet);
      set('--btn-primary-hover', shade(branding.color_primary, 0.85));
      set('--btn-primary-active', shade(branding.color_primary, 0.7));
      set('--input-focus-ring', `0 0 0 3px rgb(${triplet} / 0.3)`);
      set('--shadow-glow-blue', `0 0 20px rgb(${triplet} / 0.25)`);
    }
  }

  if (branding.color_accent) {
    const triplet = hexToTriplet(branding.color_accent);
    if (triplet) {
      set('--sw-fuchsia', branding.color_accent);
      set('--sw-fuchsia-rgb', triplet);
      set('--shadow-glow-fuchsia', `0 0 20px rgb(${triplet} / 0.25)`);
    }
  }

  if (branding.color_highlight) {
    const triplet = hexToTriplet(branding.color_highlight);
    if (triplet) {
      set('--sw-turquoise', branding.color_highlight);
      set('--sw-turquoise-rgb', triplet);
    }
  }
}
