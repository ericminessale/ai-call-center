/** @type {import('tailwindcss').Config} */
// SignalWire brand tokens — mirrored from src/index.css so Tailwind utilities work.
// 60-30-10 discipline: brand colors appear sparingly.
// Legacy class names (canvas, ink, rule, signal, live, ai, wait, urgent, info) are kept
// as aliases routed to brand-correct values, so existing components stay compiling.
export default {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      fontFamily: {
        heading: ['Geist', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['Geist', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans:    ['Geist', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono:    ['"Geist Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // ─── Brand-semantic namespace (new code uses these) ───────────────
        bg: {
          page:    '#0f1013',
          surface: '#17191e',
          raised:  '#282c34',
        },
        fg: {
          DEFAULT:   '#f2f3f5',
          secondary: '#d7d9dd',
          muted:     '#a2a5ab',
          subtle:    '#63666d',
        },
        // Brand colors resolve through CSS channel-triplet variables
        // (src/index.css --sw-*-rgb) so the runtime branding layer (IMP-02)
        // can re-theme built utility classes — including opacity modifiers
        // like sw-blue/20 — without a rebuild.
        sw: {
          blue:      'rgb(var(--sw-blue-rgb) / <alpha-value>)',
          fuchsia:   'rgb(var(--sw-fuchsia-rgb) / <alpha-value>)',
          turquoise: 'rgb(var(--sw-turquoise-rgb) / <alpha-value>)',
          gold:      'rgb(var(--sw-gold-rgb) / <alpha-value>)',
          purple:    'rgb(var(--sw-purple-rgb) / <alpha-value>)',
        },
        status: {
          success: '#2fbf71',
          warning: '#d99a2b',
          error:   '#d65745',
          // Restraint has no blue "info" accent — neutral gray (matches the
          // --status-info override in index.css). Keeps the "Connecting" banner
          // off the blue that's outside the color budget.
          info:    '#5b6470',
        },

        // ─── Legacy aliases (existing components keep working) ────────────
        // Canvas → dark elevation ramp. Even ~8% lightness steps so surfaces
        // separate cleanly (incl. on cheap IPS panels). Order: page < raised
        // < hover < elevated; AVATARS use `elevated` (top) so a monogram chip
        // is always lighter than the rail / hovered / selected row beneath it.
        canvas: {
          DEFAULT:  '#0f1013',   // base page / main content
          sunken:   '#0a0b0d',
          raised:   '#17191e',   // header + rail + cards (primary elevated)
          elevated: '#282c34',   // avatars + overlays (top of ramp)
          hover:    '#1f222a',   // row hover + selected row (secondary elevated)
        },
        // Ink → brand foreground
        ink: {
          DEFAULT: '#f2f3f5',
          muted:   '#a2a5ab',
          dim:     '#63666d',
          faint:   '#45474d',
        },
        // Rule → real hairline borders (Restraint). Bumped a touch for
        // definition against the lighter surface ramp + on poor panels.
        rule: {
          DEFAULT: '#2b2e35',
          strong:  '#393d46',
        },
        // Signal → brand fuchsia (reserved for 10% accent). DEFAULT/glow ride
        // the runtime-brandable channel vars; soft/deep stay static tints.
        signal: {
          DEFAULT: 'rgb(var(--sw-fuchsia-rgb) / <alpha-value>)',
          soft:    '#FB5E92',
          deep:    '#C21E5C',
          glow:    'rgb(var(--sw-fuchsia-rgb) / 0.15)',
        },
        // "live" operational green → calmer success (Restraint)
        live:   { DEFAULT: '#2fbf71', soft: '#5fd398', glow: 'rgba(47,191,113,0.15)' },
        // "ai" → brand turquoise — RESERVED as the AI signal (✦, AI chips, AI-active)
        ai:     { DEFAULT: 'rgb(var(--sw-turquoise-rgb) / <alpha-value>)', soft: '#7eeee3', glow: 'rgb(var(--sw-turquoise-rgb) / 0.15)' },
        // "wait" → calmer amber (Restraint warning)
        wait:   { DEFAULT: '#d99a2b', soft: '#e8b85a', glow: 'rgba(217,154,43,0.15)' },
        // "urgent" → calmer red (Restraint error)
        urgent: { DEFAULT: '#d65745', soft: '#e07d6f', glow: 'rgba(214,87,69,0.16)' },
        // "info" → neutral gray under Restraint (no blue accent in the budget)
        info:   { DEFAULT: '#5b6470', soft: '#8b9099', glow: 'rgba(91,100,112,0.15)' },
      },
      borderRadius: {
        xs: '2px',
        sm: '4px',
        DEFAULT: '6px',
        md: '8px',
        lg: '10px',
        xl: '12px',
      },
      boxShadow: {
        soft:  '0 1px 0 rgba(255,255,255,0.02) inset, 0 1px 2px rgba(0,0,0,0.3)',
        panel: '0 1px 0 rgba(255,255,255,0.02) inset',
        md:    '0 4px 16px rgba(0, 0, 0, 0.4)',
        'glow-fuchsia': 'none',
        'glow-blue':    'none',
        'signal-glow':  'none',  // glows retired under Restraint
      },
      letterSpacing: {
        heading:  '-0.02em',
        eyebrow:  '0.14em',
        tightest: '-0.02em',  // alias for legacy
      },
      keyframes: {
        'fade-up': {
          from: { opacity: '0', transform: 'translateY(6px)' },
          to:   { opacity: '1', transform: 'translateY(0)' },
        },
        'fade-in': {
          from: { opacity: '0' },
          to:   { opacity: '1' },
        },
        'pulse-ring': {
          '0%':   { boxShadow: '0 0 0 0 rgba(64,224,208,0.5)' },
          '70%':  { boxShadow: '0 0 0 8px rgba(64,224,208,0)' },
          '100%': { boxShadow: '0 0 0 0 rgba(64,224,208,0)' },
        },
        'scan': {
          '0%':   { transform: 'translateX(-100%)', opacity: '0' },
          '50%':  { opacity: '0.4' },
          '100%': { transform: 'translateX(100%)', opacity: '0' },
        },
      },
      animation: {
        'fade-up':    'fade-up 0.3s cubic-bezier(0.2, 0.8, 0.2, 1) both',
        'fade-in':    'fade-in 0.2s ease both',
        'pulse-ring': 'pulse-ring 2s cubic-bezier(0.4, 0, 0.6, 1) infinite',
        'scan':       'scan 12s linear infinite',
      },
    },
  },
  plugins: [],
}
