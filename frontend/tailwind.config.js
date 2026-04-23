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
        heading: ['"Instrument Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        display: ['"Instrument Sans"', 'ui-sans-serif', 'system-ui', 'sans-serif'],
        sans:    ['Lexend',            'ui-sans-serif', 'system-ui', 'sans-serif'],
        mono:    ['"JetBrains Mono"',  'ui-monospace',  'SFMono-Regular', 'Menlo', 'monospace'],
      },
      colors: {
        // ─── Brand-semantic namespace (new code uses these) ───────────────
        bg: {
          page:    '#0e0e18',
          surface: '#181a28',
          raised:  '#222436',
        },
        fg: {
          DEFAULT:   '#f0f0f4',
          secondary: '#e8e8ec',
          muted:     '#a0a0aa',
          subtle:    '#73737e',
        },
        sw: {
          blue:      '#044EF4',
          fuchsia:   '#F72A72',
          turquoise: '#40E0D0',
          gold:      '#FFD700',
          purple:    '#601BE6',
        },
        status: {
          success: '#22c55e',
          warning: '#FFD700',
          error:   '#ef4444',
          info:    '#044EF4',
        },

        // ─── Legacy aliases (existing components keep working) ────────────
        // Canvas → brand page/surface/raised
        canvas: {
          DEFAULT:  '#0e0e18',   // was #0B0D10 (too dark)
          sunken:   '#09090f',
          raised:   '#181a28',
          elevated: '#222436',
          hover:    '#2a2c3e',
        },
        // Ink → brand foreground
        ink: {
          DEFAULT: '#f0f0f4',    // was #E8E5DE (warm cream)
          muted:   '#a0a0aa',
          dim:     '#73737e',
          faint:   '#4a4a55',
        },
        // Rule → brand border
        rule: {
          DEFAULT: 'rgba(255,255,255,0.12)',
          strong:  'rgba(255,255,255,0.18)',
        },
        // Signal → brand fuchsia (reserved for 10% accent)
        signal: {
          DEFAULT: '#F72A72',
          soft:    '#FB5E92',
          deep:    '#C21E5C',
          glow:    'rgba(247,42,114,0.15)',
        },
        // "live" operational green → brand success
        live:   { DEFAULT: '#22c55e', soft: '#4ade80', glow: 'rgba(34,197,94,0.15)' },
        // "ai" → brand turquoise (AI = "read this first" per brand dark emphasis role)
        ai:     { DEFAULT: '#40E0D0', soft: '#7eeee3', glow: 'rgba(64,224,208,0.15)' },
        // "wait" → brand gold (warning token)
        wait:   { DEFAULT: '#FFD700', soft: '#ffe566', glow: 'rgba(255,215,0,0.15)' },
        // "urgent" → brand error
        urgent: { DEFAULT: '#ef4444', soft: '#f87171', glow: 'rgba(239,68,68,0.18)' },
        // "info" → brand blue
        info:   { DEFAULT: '#044EF4', soft: '#6e9eff', glow: 'rgba(4,78,244,0.15)' },
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
        soft:  '0 1px 0 rgba(255,255,255,0.03) inset, 0 1px 2px rgba(0,0,0,0.3)',
        panel: '0 8px 24px -8px rgba(0, 0, 0, 0.5), 0 1px 0 rgba(255,255,255,0.03) inset',
        md:    '0 4px 16px rgba(0, 0, 0, 0.5)',
        'glow-fuchsia': '0 0 20px rgba(247, 42, 114, 0.25)',
        'glow-blue':    '0 0 20px rgba(4, 78, 244, 0.25)',
        'signal-glow':  '0 0 20px rgba(247, 42, 114, 0.25)',  // alias
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
