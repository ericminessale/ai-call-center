import { useState } from 'react';
import { Sparkles, X } from 'lucide-react';
import { useAuthStore } from '../../stores/authStore';

/**
 * Demo-only contextual tip. Floating bubble with a tail/arrow that
 * anchors next to the UI element it's pointing at.
 *
 * Renders nothing unless ALL of the following are true:
 *   - the running instance is in DEMO_MODE (visitor session)
 *   - the caller's `show` prop is true (caller controls *when* the
 *     tip is contextually relevant — e.g. "the agent is offline")
 *   - the user hasn't already dismissed this tip (tracked in
 *     localStorage by `id` — once dismissed, stays dismissed
 *     forever for this browser).
 *
 * Usage:
 *
 *   <div className="relative">
 *     <button>...</button>
 *     <DemoTip
 *       id="demo-go-available"
 *       show={agentStatus === 'offline'}
 *       title="Set yourself available"
 *       body="Switch to Available to enter the queue and start receiving calls."
 *       placement="bottom-start"
 *     />
 *   </div>
 *
 * Parent must be ``position: relative`` so the absolutely-positioned
 * tip lands relative to it. The component handles the rest.
 */

const STORAGE_PREFIX = 'demo-tip-dismissed:';

type Placement = 'bottom-start' | 'bottom-end' | 'right' | 'top-start';


/**
 * Hook that handles the demo-mode + per-tip dismissal logic. Useful
 * when you want to render a custom tip layout (inline banner, full-
 * width strip, etc.) but still respect the same global rules.
 *
 * Returns ``shouldShow`` (true when in demo mode AND not dismissed)
 * and ``dismiss`` (writes to localStorage + flips local state).
 */
export function useDemoTip(id: string) {
  const isDemo = useAuthStore((s) => s.runtimeConfig?.demo_mode);
  const storageKey = `${STORAGE_PREFIX}${id}`;

  const [dismissed, setDismissed] = useState<boolean>(() => {
    try {
      return window.localStorage.getItem(storageKey) === '1';
    } catch {
      return false;
    }
  });

  const dismiss = () => {
    try {
      window.localStorage.setItem(storageKey, '1');
    } catch {
      // session-only fallback ok
    }
    setDismissed(true);
  };

  return {
    shouldShow: Boolean(isDemo) && !dismissed,
    dismiss,
  };
}

interface DemoTipProps {
  /** Unique id used to persist dismissal across reloads. */
  id: string;
  /** Whether the tip is contextually relevant right now. */
  show: boolean;
  /** Short heading. */
  title: string;
  /** Body copy — keep it one or two short sentences. */
  body: string;
  /** Where the bubble sits relative to its parent. */
  placement?: Placement;
  /** Tone — mostly for visual variation between concurrent tips. */
  tone?: 'ai' | 'live';
}

const placementClasses: Record<Placement, string> = {
  // bubble below + left edge of parent; arrow on top-left
  'bottom-start': 'top-full left-0 mt-2.5',
  // bubble below + right edge; arrow on top-right
  'bottom-end':   'top-full right-0 mt-2.5',
  // bubble to the right; arrow on left middle
  'right':        'top-0 left-full ml-2.5',
  // bubble above + left edge; arrow on bottom-left
  'top-start':    'bottom-full left-0 mb-2.5',
};

const arrowClasses: Record<Placement, string> = {
  'bottom-start': 'absolute -top-[5px] left-4 w-2.5 h-2.5 rotate-45',
  'bottom-end':   'absolute -top-[5px] right-4 w-2.5 h-2.5 rotate-45',
  'right':        'absolute top-3 -left-[5px] w-2.5 h-2.5 rotate-45',
  'top-start':    'absolute -bottom-[5px] left-4 w-2.5 h-2.5 rotate-45',
};

export function DemoTip({
  id,
  show,
  title,
  body,
  placement = 'bottom-start',
  tone = 'ai',
}: DemoTipProps) {
  const { shouldShow, dismiss } = useDemoTip(id);

  if (!shouldShow || !show) return null;

  const handleDismiss = () => dismiss();

  const toneClasses =
    tone === 'live'
      ? 'bg-live/10 border-live/40 text-ink'
      : 'bg-ai/12 border-ai/40 text-ink';
  const accentText = tone === 'live' ? 'text-live-soft' : 'text-ai-soft';
  // Arrow needs to match background of bubble + bottom border for
  // the visible edge. We use the bubble color directly so it blends.
  const arrowFill = tone === 'live' ? 'bg-live/12' : 'bg-ai/15';

  return (
    <div
      className={`absolute z-40 ${placementClasses[placement]} pointer-events-auto`}
      role="status"
      aria-live="polite"
    >
      {/* Arrow / tail */}
      <span
        className={`${arrowClasses[placement]} ${arrowFill} border-l border-t ${
          tone === 'live' ? 'border-live/40' : 'border-ai/40'
        }`}
        aria-hidden="true"
      />
      <div
        className={`relative w-72 px-3.5 py-3 rounded-md border shadow-panel backdrop-blur-sm ${toneClasses} animate-fade-up`}
      >
        <div className="flex items-start gap-2.5">
          <Sparkles className={`w-3.5 h-3.5 mt-0.5 shrink-0 ${accentText}`} />
          <div className="min-w-0 flex-1">
            <div className={`text-[12px] font-medium leading-tight ${accentText}`}>
              {title}
            </div>
            <p className="text-[12px] text-ink-muted leading-snug mt-1">
              {body}
            </p>
          </div>
          <button
            type="button"
            onClick={handleDismiss}
            aria-label="Dismiss tip"
            className="shrink-0 -mt-0.5 -mr-1 p-1 rounded text-ink-dim hover:text-ink hover:bg-canvas-hover transition-colors"
          >
            <X className="w-3 h-3" />
          </button>
        </div>
      </div>
    </div>
  );
}
