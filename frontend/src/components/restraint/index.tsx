/**
 * ============================================================================
 * RESTRAINT DESIGN SYSTEM — React primitives
 * ============================================================================
 *
 * A typed, reusable set of PRESENTATIONAL primitives for the "Restraint"
 * visual direction. Each component takes props and returns markup — no data
 * fetching, no app state, no hooks beyond trivial local UI state.
 *
 * Token mapping (mockup CSS var -> our Tailwind/CSS token, never raw hex):
 *   --pop    -> text-sw-fuchsia / bg-sw-fuchsia  (SOLID; primary action + active markers ONLY)
 *   --ok     -> status-success  / .dot-success
 *   --warn   -> status-warning  / .dot-warning
 *   --bad    -> status-error    / .dot-error
 *   --t1     -> text-ink
 *   --t2     -> text-ink-muted
 *   --t3     -> text-ink-dim
 *   --line   -> border-rule
 *   --line2  -> border-rule-strong  (also bg-rule-strong for silent dots)
 *   --panel  -> bg-canvas-raised
 *   --raised -> bg-canvas-elevated
 *   --selbg  -> bg-canvas-hover
 *   --page   -> bg-canvas (the app page color)
 *   mono     -> .mono / font-mono (Geist Mono)
 *   AI       -> "✦" (U+2726) glyph + text-ai (turquoise) — a deliberate signal
 *
 * Color budget: green / amber / red status dots only; fuchsia ONLY on the
 * primary action button and active/selected markers; turquoise ONLY for AI.
 * Healthy / normal state stays visually silent (neutral gray, no hue wash).
 *
 * @module restraint
 */

import React from 'react';
import { cx, AI_GLYPH, STATUS_DOT_BG } from './_util';
import type { RestraintStatus } from './_util';

export type { RestraintStatus } from './_util';
export { AI_GLYPH, cx } from './_util';

/* ===========================================================================
 * 1. CHIP — neutral status badge
 * ======================================================================== */

export interface ChipProps {
  /** Chip contents (text, glyph, etc.). */
  children: React.ReactNode;
  /**
   * Optional leading status dot. Color comes from this dot only — the chip
   * text always stays neutral. Omit for a plain neutral tag.
   */
  dot?: RestraintStatus;
  /**
   * Marks this chip as referencing an AI handler. Prefixes the deliberate
   * `✦` (U+2726) glyph and tints the chip text turquoise (`text-ai`).
   * Mutually meaningful with `dot` (you can have both, but rarely should).
   */
  ai?: boolean;
  className?: string;
  title?: string;
}

/**
 * The ONE neutral chip style: transparent surface, hairline border, muted
 * text. Meaning is carried by an optional 5px status `dot`, never a hue wash.
 */
export function Chip({ children, dot, ai = false, className, title }: ChipProps) {
  return (
    <span
      title={title}
      className={cx(
        'inline-flex items-center gap-1.5 px-2 py-1 rounded-sm border border-rule-strong',
        'text-xs font-medium bg-transparent whitespace-nowrap',
        ai ? 'text-ai' : 'text-ink-muted',
        className,
      )}
    >
      {dot && <span className={cx('w-1 h-1 rounded-full flex-shrink-0', STATUS_DOT_BG[dot])} />}
      {ai && <span aria-hidden className="text-ai">{AI_GLYPH}</span>}
      {children}
    </span>
  );
}

/* ===========================================================================
 * 2. STATUS DOT — minimal status indicator
 * ======================================================================== */

export interface StatusDotProps {
  /** Status drives the color. `neutral` (default) is the silent healthy state. */
  status?: RestraintStatus;
  /** `row` → 6px (list/row context); `chip` → 5px (inside a chip). */
  size?: 'row' | 'chip';
  className?: string;
  title?: string;
}

/**
 * Bare status circle. Color signals deviation only — the healthy/normal state
 * is visually silent (neutral gray via `bg-rule-strong`).
 */
export function StatusDot({ status = 'neutral', size = 'row', className, title }: StatusDotProps) {
  return (
    <span
      title={title}
      role={title ? 'img' : undefined}
      aria-label={title}
      className={cx(
        'rounded-full flex-shrink-0',
        size === 'chip' ? 'w-1 h-1' : 'w-1.5 h-1.5',
        STATUS_DOT_BG[status],
        className,
      )}
    />
  );
}

/* ===========================================================================
 * 3. KPI STRIP — bordered multi-cell container
 * 4. KPI CELL — single stat
 * ======================================================================== */

export interface KpiCellProps {
  label: string;
  /** Rendered in mono. Accepts node so callers can compose (e.g. a unit). */
  value: React.ReactNode;
  /** When true, value renders in `text-status-error` (e.g. negative sentiment). */
  negative?: boolean;
  className?: string;
}

/** A single label-over-value stat. Usable standalone or inside `KpiStrip`. */
export function KpiCell({ label, value, negative = false, className }: KpiCellProps) {
  return (
    <div className={cx('flex flex-col justify-center', className)}>
      <span className="text-[11px] font-medium text-ink-dim">{label}</span>
      <div className={cx('mono text-base font-semibold mt-0.5', negative ? 'text-status-error' : 'text-ink')}>
        {value}
      </div>
    </div>
  );
}

export interface KpiStripProps {
  /** Cells to render; each becomes an equal-width divider-separated column. */
  cells: KpiCellProps[];
  className?: string;
}

/**
 * Bordered horizontal strip of 4–5 equal KPI cells, separated by vertical
 * hairline dividers (last cell has no trailing rule).
 */
export function KpiStrip({ cells, className }: KpiStripProps) {
  return (
    <div className={cx('flex border border-rule rounded-lg bg-canvas-raised', className)}>
      {cells.map((cell, i) => (
        <div
          key={i}
          className={cx('flex-1 px-4 py-3 border-r border-rule', i === cells.length - 1 && 'border-r-0')}
        >
          <span className="text-[11px] font-medium text-ink-dim">{cell.label}</span>
          <div className={cx('mono text-base font-semibold mt-1', cell.negative ? 'text-status-error' : 'text-ink')}>
            {cell.value}
          </div>
        </div>
      ))}
    </div>
  );
}

/* ===========================================================================
 * 5. CALL HISTORY ROW
 * ======================================================================== */

/** A handler label that may be flagged as an AI handler. */
export interface HandlerRef {
  label: string;
  ai?: boolean;
}

export interface CallHistoryRowProps {
  direction: 'inbound' | 'outbound';
  /** "Outbound call" / "Inbound call" — defaults derived from `direction`. */
  title?: string;
  /** Optional originating handler chip (rendered before the → separator). */
  from?: HandlerRef;
  /** Optional destination handler chip (rendered after the → separator). */
  to?: HandlerRef;
  /** Outcome chip text + dot, e.g. {label:"Completed", status:"success"}. */
  outcome?: { label: string; status: RestraintStatus };
  /** Extra neutral/dot/AI chips rendered after the outcome chip (e.g. sentiment). */
  extraChips?: Array<{ label: React.ReactNode; dot?: RestraintStatus; ai?: boolean }>;
  /** Right-aligned mono date, e.g. "Jun 10". */
  date?: string;
  /** Right-aligned mono duration, e.g. "2:08". */
  duration?: string;
  /** Extra mono meta appended after date/duration in the right group (e.g. cost). */
  metaExtra?: React.ReactNode;
  /** Summary line below the title row. */
  summary?: React.ReactNode;
  onClick?: () => void;
  className?: string;
}

/** Single call record in a history list: direction icon, title row, summary. */
export function CallHistoryRow({
  direction,
  title,
  from,
  to,
  outcome,
  extraChips,
  date,
  duration,
  metaExtra,
  summary,
  onClick,
  className,
}: CallHistoryRowProps) {
  const heading = title ?? (direction === 'outbound' ? 'Outbound call' : 'Inbound call');
  const icon = direction === 'outbound' ? '↗' : '↙';
  return (
    <div
      onClick={onClick}
      className={cx(
        'flex gap-3 py-3 border-b border-rule items-start',
        onClick && 'cursor-pointer',
        className,
      )}
    >
      <div className="w-7 h-7 rounded-md border border-rule-strong bg-canvas-raised flex items-center justify-center text-xs text-ink-muted flex-shrink-0">
        {icon}
      </div>
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className="text-sm font-semibold text-ink">{heading}</span>
          {from && <Chip ai={from.ai}>{from.label}</Chip>}
          {from && to && <span className="text-ink-dim text-xs">→</span>}
          {to && <Chip ai={to.ai}>{to.label}</Chip>}
          {outcome && <Chip dot={outcome.status}>{outcome.label}</Chip>}
          {extraChips?.map((c, i) => (
            <Chip key={i} dot={c.dot} ai={c.ai}>{c.label}</Chip>
          ))}
          {(date || duration || metaExtra) && (
            <div className="ml-auto flex gap-3 mono text-xs text-ink-dim flex-shrink-0">
              {date && <span>{date}</span>}
              {duration && <span>{duration}</span>}
              {metaExtra && <span>{metaExtra}</span>}
            </div>
          )}
        </div>
        {summary && <div className="text-sm text-ink-muted mt-1 leading-relaxed">{summary}</div>}
      </div>
    </div>
  );
}

/* ===========================================================================
 * 6. RAIL CONTACT ROW — sidebar recent-contact entry
 * ======================================================================== */

export interface RailContactRowProps {
  name: string;
  phone?: string;
  /** Initials/avatar glyph shown in the avatar box. */
  avatar?: React.ReactNode;
  /** Right-aligned mono call count. Ignored if `trailing` is provided. */
  callCount?: number | string;
  /** Sentiment score in [-1, 1]; a red dot shows when < -0.2. */
  sentiment?: number;
  /** Inline marker rendered right after the name (e.g. a VIP star). */
  badge?: React.ReactNode;
  /** Right-aligned cluster (e.g. Live chip / tier) — replaces callCount position. */
  trailing?: React.ReactNode;
  selected?: boolean;
  onClick?: () => void;
  className?: string;
}

/** Contact in the left sidebar: avatar + name/phone + call count. */
export function RailContactRow({
  name,
  phone,
  avatar,
  callCount,
  sentiment,
  badge,
  trailing,
  selected = false,
  onClick,
  className,
}: RailContactRowProps) {
  const showSentiment = typeof sentiment === 'number' && sentiment < -0.2;
  return (
    <div
      onClick={onClick}
      className={cx(
        'relative flex items-center gap-2.5 p-2 rounded-lg cursor-pointer transition-colors',
        // Selected = rounded raised container + inset hairline (no layout shift),
        // mirroring the mockup's `inset 0 0 0 1px`; the fuchsia stripe is SHORT.
        selected ? 'bg-canvas-hover shadow-[inset_0_0_0_1px_var(--border-strong)]' : 'hover:bg-canvas-hover/50',
        className,
      )}
    >
      {selected && <span className="absolute left-0 top-2 bottom-2 w-0.5 rounded-sm bg-sw-fuchsia" />}
      {/* Monogram chip at the TOP of the elevation ramp (canvas-elevated) +
          strong border, so it always reads lighter than the rail / hover /
          selected row beneath it — never inverts into a dark hole. */}
      <span className="w-7 h-7 rounded-md bg-canvas-elevated border border-rule-strong flex items-center justify-center text-xs font-semibold text-ink-muted flex-shrink-0">
        {avatar ?? name.slice(0, 2).toUpperCase()}
      </span>
      <div className="flex-1 min-w-0">
        <div className="flex items-center text-sm font-medium text-ink">
          <span className="truncate">{name}</span>
          {badge && <span className="ml-1 flex-shrink-0">{badge}</span>}
          {showSentiment && <span className="w-1.5 h-1.5 rounded-full bg-status-error ml-1 flex-shrink-0" />}
        </div>
        {phone && <div className="mono text-xs text-ink-dim truncate">{phone}</div>}
      </div>
      {trailing
        ? <span className="ml-auto flex-shrink-0 flex items-center gap-1.5">{trailing}</span>
        : callCount != null && <span className="mono text-xs text-ink-dim ml-auto flex-shrink-0">{callCount}</span>}
    </div>
  );
}

/* ===========================================================================
 * 7. RAIL LIVE-CALL ROW — supervisor active-call entry
 * ======================================================================== */

export interface RailLiveCallRowProps {
  name: string;
  queue?: string;
  handler?: HandlerRef;
  /** Sentiment status drives the leading dot; `neutral` stays silent. */
  sentiment?: RestraintStatus;
  /** Right-aligned mono call duration, e.g. "4:12". */
  duration?: string;
  /** Attention state: amber left rail + raised background. */
  attention?: boolean;
  /** Selected (open in the detail pane): raised bg + inset ring + short fuchsia stripe. */
  selected?: boolean;
  /** Trailing controls (e.g. Listen / inject) rendered after the duration. */
  trailing?: React.ReactNode;
  onClick?: () => void;
  className?: string;
}

/** Active call in the supervisor sidebar: sentiment dot + name/queue + timer. */
export function RailLiveCallRow({
  name,
  queue,
  handler,
  sentiment = 'neutral',
  duration,
  attention = false,
  selected = false,
  trailing,
  onClick,
  className,
}: RailLiveCallRowProps) {
  return (
    <div
      onClick={onClick}
      className={cx(
        'relative flex items-center gap-2.5 p-2 rounded-lg',
        (attention || selected) && 'bg-canvas-hover',
        selected && 'shadow-[inset_0_0_0_1px_var(--border-strong)]',
        onClick && 'cursor-pointer',
        className,
      )}
    >
      {/* Selected (fuchsia) takes the stripe over attention (amber) when both. */}
      {selected
        ? <span className="absolute left-0 top-2 bottom-2 w-0.5 rounded-sm bg-sw-fuchsia" />
        : attention && <span className="absolute left-0 top-2 bottom-2 w-0.5 rounded-sm bg-status-warning" />}
      <StatusDot status={sentiment} />
      <div className="flex-1 min-w-0">
        <div className="text-sm font-medium text-ink truncate">{name}</div>
        {(queue || handler) && (
          <div className="flex items-center gap-1.5 text-xs text-ink-dim mt-0.5">
            {queue && <span>{queue}</span>}
            {queue && handler && <span className="text-rule-strong">·</span>}
            {handler && (
              <span className={cx(handler.ai && 'text-ai')}>
                {handler.ai && <span aria-hidden>{AI_GLYPH} </span>}
                {handler.label}
              </span>
            )}
          </div>
        )}
      </div>
      {duration && <span className={cx('mono text-xs text-ink-muted flex-shrink-0', !trailing && 'ml-auto')}>{duration}</span>}
      {trailing && <span className={cx('flex items-center gap-1 flex-shrink-0', !duration && 'ml-auto')}>{trailing}</span>}
    </div>
  );
}

/* ===========================================================================
 * 8. SEGMENTED CONTROL — filter tabs
 * ======================================================================== */

export interface SegmentedControlProps<T extends string = string> {
  /** Option list; each `{ value, label }`. */
  options: Array<{ value: T; label: React.ReactNode }>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
}

/** Background-contrast toggle between filter categories (no color, no fade). */
export function SegmentedControl<T extends string = string>({
  options,
  value,
  onChange,
  className,
}: SegmentedControlProps<T>) {
  return (
    <div className={cx('flex gap-0.5 border border-rule rounded-lg p-0.5 bg-canvas', className)}>
      {options.map((opt) => {
        const on = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={cx(
              'flex-1 text-center px-3 py-1 text-xs font-medium rounded-md',
              on
                ? 'bg-canvas-raised text-ink border border-rule-strong'
                : 'bg-transparent text-ink-muted',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/* ===========================================================================
 * 9. FLOOR STAT TILES — 4-column metric grid
 * ======================================================================== */

export interface FloorStatTileProps {
  label: string;
  /** Big mono value. */
  value: React.ReactNode;
  subtitle?: React.ReactNode;
  /** Optional attention dot shown to the right of the value. */
  attention?: RestraintStatus;
  className?: string;
}

/** A single floor metric card. Compose 4 inside `FloorStatGrid`. */
export function FloorStatTile({ label, value, subtitle, attention, className }: FloorStatTileProps) {
  return (
    <div className={cx('border border-rule rounded-lg bg-canvas-raised px-4 py-3.5', className)}>
      <span className="text-[11px] font-medium text-ink-dim">{label}</span>
      <div className="flex items-center gap-2 mt-2">
        <span className="mono text-2xl font-semibold text-ink">{value}</span>
        {attention && <StatusDot status={attention} />}
      </div>
      {subtitle && <div className="text-xs text-ink-dim mt-1.5">{subtitle}</div>}
    </div>
  );
}

export interface FloorStatGridProps {
  tiles: FloorStatTileProps[];
  /** Columns in the grid (default 4). */
  columns?: 2 | 3 | 4 | 5;
  className?: string;
}

/** Responsive grid wrapper for `FloorStatTile`s (high-level floor metrics). */
export function FloorStatGrid({ tiles, columns = 4, className }: FloorStatGridProps) {
  const cols =
    columns === 2 ? 'grid-cols-2' :
    columns === 3 ? 'grid-cols-3' :
    columns === 5 ? 'grid-cols-5' :
    'grid-cols-4';
  return (
    <div className={cx('grid gap-2.5', cols, className)}>
      {tiles.map((tile, i) => (
        <FloorStatTile key={i} {...tile} />
      ))}
    </div>
  );
}

/* ===========================================================================
 * 10. QUEUE DEPTH BAR ROW
 * ======================================================================== */

export interface QueueDepthRowProps {
  name: string;
  /** Current depth. */
  depth: number;
  /** Max depth used to scale the fill width. */
  max: number;
  /** Right-aligned SLA / wait label, e.g. "0:45". */
  sla?: string;
  /** Depth above which the fill turns amber (default 1). */
  warnThreshold?: number;
  className?: string;
}

/** One queue's depth as an SLA progress bar: name + track/fill + SLA time. */
export function QueueDepthRow({ name, depth, max, sla, warnThreshold = 1, className }: QueueDepthRowProps) {
  const pct = max > 0 ? Math.min(100, Math.max(0, (depth / max) * 100)) : 0;
  const warn = depth > warnThreshold;
  return (
    <div className={cx('flex items-center gap-2.5 py-3', className)}>
      <span className="w-14 text-xs font-medium text-ink whitespace-nowrap">{name}</span>
      <span className="flex-1 h-1.5 rounded-full bg-rule overflow-hidden">
        <span
          className={cx('block h-full rounded-full', warn ? 'bg-status-warning' : 'bg-ink-muted')}
          style={{ width: `${pct}%` }}
        />
      </span>
      {sla && <span className="mono text-xs text-ink-dim w-10 text-right whitespace-nowrap">{sla}</span>}
    </div>
  );
}

/* ===========================================================================
 * 11. TRANSCRIPT UTTERANCE ROW
 * ======================================================================== */

export interface TranscriptUtteranceProps {
  /** Speaker label, e.g. "Caller", "Agent", "Sofia". */
  speaker: string;
  /** `caller` text is bright (text-ink); `agent` text is muted (text-ink-muted). */
  role?: 'caller' | 'agent';
  text: React.ReactNode;
  /** Right-aligned mono timestamp, e.g. "14:09". */
  timestamp?: string;
  className?: string;
}

/** A single line of conversation: speaker + bordered message + timestamp. */
export function TranscriptUtterance({ speaker, role = 'caller', text, timestamp, className }: TranscriptUtteranceProps) {
  return (
    <div className={cx('flex gap-2.5 py-2', className)}>
      <span className="mono text-xs font-semibold text-ink-dim w-12 flex-shrink-0 pt-0.5">{speaker}</span>
      <span
        className={cx(
          'text-sm leading-relaxed border-l-2 border-rule pl-2.5 flex-1 min-w-0',
          role === 'agent' ? 'text-ink-muted' : 'text-ink',
        )}
      >
        {text}
      </span>
      {timestamp && <span className="mono text-xs text-ink-dim ml-auto flex-shrink-0 pt-0.5">{timestamp}</span>}
    </div>
  );
}

/* ===========================================================================
 * 12. CALL-TIMELINE NODE
 * ======================================================================== */

export interface CallTimelineNodeProps {
  /** Mono time label, e.g. "14:02". */
  time: string;
  /** Event title. Pass `ai` to prefix the turquoise ✦ AI signal. */
  event: React.ReactNode;
  description?: React.ReactNode;
  ai?: boolean;
  /** When true, omits the trailing vertical connector (last node in list). */
  last?: boolean;
  className?: string;
}

/** Single event in a vertical call timeline: time + rail dot/line + content. */
export function CallTimelineNode({ time, event, description, ai = false, last = false, className }: CallTimelineNodeProps) {
  return (
    <div className={cx('flex gap-2.5 py-1.5', className)}>
      <span className="mono text-xs text-ink-dim w-10 flex-shrink-0 pt-0.5">{time}</span>
      <div className="flex flex-col items-center flex-shrink-0">
        <span
          className="w-1.5 h-1.5 rounded-full border border-ink-dim bg-canvas-raised flex-shrink-0 mt-1"
          style={{ borderWidth: '1.5px' }}
        />
        {!last && <span className="w-0.5 flex-1 min-h-12 bg-rule mx-auto" />}
      </div>
      <div className="pb-1">
        <div className="text-sm font-semibold text-ink">
          {ai && <span aria-hidden className="text-ai">{AI_GLYPH} </span>}
          {event}
        </div>
        {description && <div className="text-xs text-ink-dim mt-0.5">{description}</div>}
      </div>
    </div>
  );
}

/* ===========================================================================
 * 13. BUTTON — primary / secondary / danger / icon
 * ======================================================================== */

export type ButtonVariant = 'primary' | 'secondary' | 'danger';

export interface ButtonProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  /** Optional leading icon node. */
  icon?: React.ReactNode;
  /** Renders a square 32px icon-only button (children become aria-label content). */
  iconOnly?: boolean;
}

const BUTTON_BASE = 'inline-flex items-center gap-1.5 rounded-lg text-sm disabled:opacity-50 disabled:cursor-not-allowed transition-colors';
const BUTTON_VARIANTS: Record<ButtonVariant, string> = {
  // Fuchsia is SOLID and only here (+ active markers). Never faded, never ghost.
  primary: 'px-5 py-1.5 bg-sw-fuchsia text-white font-semibold border border-sw-fuchsia',
  secondary: 'px-4 py-1.5 border border-rule-strong text-ink font-medium bg-canvas-raised hover:bg-canvas-hover',
  danger: 'px-4 py-1.5 bg-status-error text-white font-semibold border border-status-error',
};

/**
 * Call-to-action button. Fuchsia is reserved for `primary`. Secondary is the
 * default neutral surface; danger is the solid-red destructive action.
 */
export function Button({ variant = 'secondary', icon, iconOnly = false, className, children, ...rest }: ButtonProps) {
  if (iconOnly) {
    return (
      <button
        {...rest}
        className={cx(
          BUTTON_BASE,
          'w-8 h-8 justify-center p-0',
          variant === 'primary'
            ? 'bg-sw-fuchsia text-white border border-sw-fuchsia'
            : variant === 'danger'
            ? 'bg-status-error text-white border border-status-error'
            : 'border border-rule-strong text-ink bg-canvas-raised hover:bg-canvas-hover',
          className,
        )}
      >
        {icon ?? children}
      </button>
    );
  }
  return (
    <button {...rest} className={cx(BUTTON_BASE, BUTTON_VARIANTS[variant], className)}>
      {icon}
      {children}
    </button>
  );
}

/* ===========================================================================
 * 14. FORM INPUT + LABEL + CHECKBOX
 * ======================================================================== */

export interface FieldLabelProps {
  children: React.ReactNode;
  htmlFor?: string;
  className?: string;
}

/** Tiny uppercase field label. */
export function FieldLabel({ children, htmlFor, className }: FieldLabelProps) {
  return (
    <label htmlFor={htmlFor} className={cx('block text-[11px] font-medium text-ink-dim mb-1.5', className)}>
      {children}
    </label>
  );
}

export interface TextInputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  /** Optional trailing adornment (e.g. a dropdown caret) rendered inside a flex wrapper. */
  adornment?: React.ReactNode;
}

/** Restraint text input. Quiet surface, strong hairline, no glow. */
export const TextInput = React.forwardRef<HTMLInputElement, TextInputProps>(function TextInput(
  { adornment, className, ...rest },
  ref,
) {
  const input = (
    <input
      ref={ref}
      {...rest}
      className={cx(
        'w-full px-3 py-2 rounded-lg border border-rule-strong bg-canvas text-ink text-sm font-medium',
        'placeholder:text-ink-dim focus:outline-none focus:border-sw-fuchsia',
        !adornment && className,
      )}
    />
  );
  if (!adornment) return input;
  return (
    <div className={cx('flex items-center gap-2 rounded-lg border border-rule-strong bg-canvas px-3', className)}>
      <input
        ref={ref}
        {...rest}
        className="w-full py-2 bg-transparent text-ink text-sm font-medium placeholder:text-ink-dim focus:outline-none"
      />
      <span className="text-ink-dim text-xs flex-shrink-0">{adornment}</span>
    </div>
  );
});

export interface CheckboxProps {
  checked: boolean;
  onChange?: (checked: boolean) => void;
  label?: React.ReactNode;
  disabled?: boolean;
  className?: string;
}

/** Square checkbox; checked state fills fuchsia (an "active marker", per spec). */
export function Checkbox({ checked, onChange, label, disabled = false, className }: CheckboxProps) {
  return (
    <label className={cx('inline-flex items-center gap-2', disabled ? 'opacity-50' : 'cursor-pointer', className)}>
      <span
        onClick={() => !disabled && onChange?.(!checked)}
        className={cx(
          'w-4 h-4 rounded flex items-center justify-center text-white text-xs flex-shrink-0',
          checked ? 'bg-sw-fuchsia' : 'border border-rule-strong bg-canvas',
        )}
      >
        {checked && '✓'}
      </span>
      {label && <span className="text-sm text-ink">{label}</span>}
    </label>
  );
}

/* ===========================================================================
 * 15. PILL BADGE — duration + cost
 * ======================================================================== */

export interface PillBadgeProps {
  /** Primary mono text, e.g. "2:08". */
  time: React.ReactNode;
  /** Smaller muted suffix, e.g. "~$0.42". */
  cost?: React.ReactNode;
  /** Leading status dot (e.g. green for active/recording). */
  dot?: RestraintStatus;
  /** Replaces the dot with the turquoise ✦ AI signal. */
  ai?: boolean;
  className?: string;
}

/** Inline duration/cost pill. Green dot = active/recording; ✦ = AI-handled. */
export function PillBadge({ time, cost, dot, ai = false, className }: PillBadgeProps) {
  return (
    <span
      className={cx(
        'inline-flex items-center gap-2 px-3 py-1.5 rounded-lg border border-rule-strong text-sm font-semibold mono bg-transparent',
        className,
      )}
    >
      {ai ? (
        <span aria-hidden className="text-ai">{AI_GLYPH}</span>
      ) : (
        dot && <StatusDot status={dot} />
      )}
      <span className={cx('text-sm font-semibold mono', ai ? 'text-ai' : 'text-ink')}>{time}</span>
      {cost && <span className="text-xs font-normal text-ink-dim">{cost}</span>}
    </span>
  );
}

/* ===========================================================================
 * 16. TAB BAR — content subtabs
 * ======================================================================== */

export interface TabItem<T extends string = string> {
  value: T;
  label: React.ReactNode;
  /** When set, shows a close "×" and calls back when clicked. */
  onClose?: () => void;
}

export interface TabsProps<T extends string = string> {
  tabs: Array<TabItem<T>>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
}

/** Underline subtabs; the active tab gets a fuchsia underline (active marker). */
export function Tabs<T extends string = string>({ tabs, value, onChange, className }: TabsProps<T>) {
  return (
    <div className={cx('flex gap-4 border-b border-rule', className)}>
      {tabs.map((tab) => {
        const on = tab.value === value;
        return (
          <button
            key={tab.value}
            type="button"
            onClick={() => onChange(tab.value)}
            className={cx(
              'px-0.5 py-2 text-sm font-medium border-b-2 -mb-px inline-flex items-center',
              on ? 'border-sw-fuchsia text-ink' : 'border-transparent text-ink-muted',
            )}
          >
            {tab.label}
            {tab.onClose && (
              <span
                role="button"
                aria-label="Close tab"
                onClick={(e) => {
                  e.stopPropagation();
                  tab.onClose?.();
                }}
                className="ml-0.5 text-xs text-ink-dim"
              >
                ×
              </span>
            )}
          </button>
        );
      })}
    </div>
  );
}

/* ===========================================================================
 * 17. INLINE ATTENTION BAR
 * ======================================================================== */

export interface AttentionBarProps {
  /** Severity drives the left bar + label color (default `warning`). */
  status?: Exclude<RestraintStatus, 'neutral'>;
  /** Bold short label, e.g. "Needs attention". */
  label: React.ReactNode;
  /** Muted meta on the label row, e.g. "sentiment −0.6 and falling". */
  meta?: React.ReactNode;
  /** Mono phone number in the details row. */
  phone?: React.ReactNode;
  /** Neutral chips for the details row. */
  chips?: Array<{ label: string; dot?: RestraintStatus; ai?: boolean }>;
  /** Mono timestamp in the details row. */
  timestamp?: React.ReactNode;
  /** Optional context line(s) below the details — e.g. a transcript/summary preview. */
  preview?: React.ReactNode;
  /** Action buttons stacked on the right. */
  actions?: React.ReactNode;
  className?: string;
}

const ATTENTION_BAR_BG: Record<Exclude<RestraintStatus, 'neutral'>, string> = {
  success: 'bg-status-success',
  warning: 'bg-status-warning',
  error: 'bg-status-error',
};
const ATTENTION_TEXT: Record<Exclude<RestraintStatus, 'neutral'>, string> = {
  success: 'text-status-success',
  warning: 'text-status-warning',
  error: 'text-status-error',
};

/** Highlights a call needing attention: colored left bar + content + actions. */
export function AttentionBar({
  status = 'warning',
  label,
  meta,
  phone,
  chips,
  timestamp,
  preview,
  actions,
  className,
}: AttentionBarProps) {
  return (
    <div className={cx('relative overflow-hidden border border-rule rounded-lg bg-canvas-raised p-3.5 flex gap-5', className)}>
      <span className={cx('absolute left-0 top-0 bottom-0 w-0.5', ATTENTION_BAR_BG[status])} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2 flex-wrap">
          <span className={cx('inline-flex items-center gap-1.5 text-xs font-semibold', ATTENTION_TEXT[status])}>
            <StatusDot status={status} />
            {label}
          </span>
          {meta && <span className="text-xs text-ink-dim">{meta}</span>}
        </div>
        {(phone || chips || timestamp) && (
          <div className="flex items-center gap-2.5 mt-2 flex-wrap">
            {phone && <span className="mono text-sm font-semibold text-ink">{phone}</span>}
            {chips?.map((c, i) => (
              <Chip key={i} dot={c.dot} ai={c.ai}>
                {c.label}
              </Chip>
            ))}
            {timestamp && <span className="mono text-xs text-ink-dim">{timestamp}</span>}
          </div>
        )}
        {preview && <div className="text-xs text-ink-muted mt-2 leading-relaxed">{preview}</div>}
      </div>
      {actions && <div className="flex flex-col gap-2 flex-shrink-0 justify-center">{actions}</div>}
    </div>
  );
}

/* ===========================================================================
 * 18. CONTEXT BOX — call metadata grid
 * ======================================================================== */

export interface ContextBoxItem {
  key: string;
  /** Value; pass a node to compose a dot or badge alongside the text. */
  value: React.ReactNode;
}

export interface ContextBoxProps {
  /** Optional tiny uppercase section title. */
  title?: string;
  items: ContextBoxItem[];
  className?: string;
}

/** Bordered box of collected call context as a wrapping 2-column key/value grid. */
export function ContextBox({ title, items, className }: ContextBoxProps) {
  return (
    <div className={cx('border border-rule rounded-lg bg-canvas-raised p-3', className)}>
      {title && <div className="text-[11px] font-medium text-ink-dim">{title}</div>}
      <div className={cx('flex gap-6 flex-wrap', title && 'mt-2')}>
        {items.map((item, i) => (
          <div key={i} className="flex flex-col">
            <span className="text-xs text-ink-dim font-medium">{item.key}</span>
            <span className="text-xs font-semibold text-ink mt-0.5 flex items-center gap-1.5">{item.value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

/* ===========================================================================
 * 19. SEGMENTED MODE CONTROL — On/Off/Auto inline toggle
 * ======================================================================== */

export interface SegmentedModeControlProps<T extends string = string> {
  options: Array<{ value: T; label: React.ReactNode }>;
  value: T;
  onChange: (value: T) => void;
  className?: string;
}

/**
 * Compact inline toggle between discrete modes (Off / On request / Auto).
 * Active segment lifts to `bg-canvas-hover` — contrast, not color.
 */
export function SegmentedModeControl<T extends string = string>({
  options,
  value,
  onChange,
  className,
}: SegmentedModeControlProps<T>) {
  return (
    <div className={cx('inline-flex border border-rule-strong rounded-lg p-0.5 gap-0', className)}>
      {options.map((opt) => {
        const on = opt.value === value;
        return (
          <button
            key={opt.value}
            type="button"
            onClick={() => onChange(opt.value)}
            className={cx(
              'px-2.5 py-0.5 text-xs font-medium rounded-md',
              on ? 'bg-canvas-hover text-ink' : 'text-ink-muted',
            )}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

/* ===========================================================================
 * 20. TEMPLATE PILL — whisper / quick-action button
 * ======================================================================== */

export interface TemplatePillProps extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  children: React.ReactNode;
}

/** Rounded quick-fill button for common instructions (Offer discount, etc.). */
export function TemplatePill({ children, className, ...rest }: TemplatePillProps) {
  return (
    <button
      type="button"
      {...rest}
      className={cx(
        'inline-flex px-3 py-1 rounded-full border border-rule-strong text-ink text-xs font-medium',
        'bg-transparent hover:bg-canvas-hover cursor-pointer',
        className,
      )}
    >
      {children}
    </button>
  );
}
