import { useEffect, useMemo, useState } from 'react';
import {
  Phone,
  Clock,
  AlertCircle,
  CheckCircle2,
  PhoneOff,
  PhoneCall,
  RotateCw,
  X,
  User,
  MessageSquare,
} from 'lucide-react';
import {
  callbacksApi,
  Callback,
  CallbackOutcome,
} from '../../services/api';
import toast from 'react-hot-toast';
import { logger } from '../../lib/logger';

// =============================================================================
// CallbackDetail — right pane showing one selected callback. Lets the agent
// claim, dial, and record an outcome. Handles the demo-mode soft-block on
// the dial endpoint with a clear toast (no surprise 403s).
// =============================================================================

interface CallbackDetailProps {
  callback: Callback;
  currentUserId: number;
  onUpdated: (callback: Callback) => void;
  onClose: () => void;
}

const OUTCOME_LABELS: Record<CallbackOutcome, { label: string; tone: string }> = {
  success: { label: 'Connected — issue resolved', tone: 'text-green-400' },
  'no-answer': { label: 'No answer', tone: 'text-yellow-400' },
  voicemail: { label: 'Left voicemail', tone: 'text-blue-400' },
  declined: { label: 'Caller declined', tone: 'text-gray-400' },
  'wrong-number': { label: 'Wrong number', tone: 'text-orange-400' },
  expired: { label: 'Expired', tone: 'text-gray-500' },
};

const RETRYABLE_OUTCOMES: CallbackOutcome[] = ['no-answer', 'voicemail'];

export function CallbackDetail({ callback, currentUserId, onUpdated, onClose }: CallbackDetailProps) {
  const [busyAction, setBusyAction] = useState<null | 'claim' | 'release' | 'dial' | 'outcome'>(null);
  const [outcome, setOutcome] = useState<CallbackOutcome | ''>('');
  const [outcomeNotes, setOutcomeNotes] = useState('');
  const [retry, setRetry] = useState(false);

  // Whenever we switch to a different callback row, reset the outcome form.
  useEffect(() => {
    setOutcome('');
    setOutcomeNotes('');
    setRetry(false);
  }, [callback.id]);

  const isMine = callback.claimedByAgentId === currentUserId;
  const isUnclaimed = callback.claimedByAgentId === null;
  const isCompleted = callback.completedAt !== null;
  const isExpired = callback.isExpired && !isCompleted;
  const canDial = !isCompleted && !isExpired && (isMine || isUnclaimed);

  const aiContextEntries = useMemo(() => {
    return Object.entries(callback.aiContext || {}).filter(
      ([, v]) =>
        v !== null &&
        v !== undefined &&
        v !== '' &&
        v !== 'unknown' &&
        v !== 'not specified'
    );
  }, [callback.aiContext]);

  async function handleClaim() {
    try {
      setBusyAction('claim');
      const res = await callbacksApi.claim(callback.id);
      onUpdated(res.data.callback);
      toast.success('Claimed');
    } catch (err: any) {
      const msg = err?.response?.data?.error || 'Failed to claim';
      toast.error(msg);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleRelease() {
    try {
      setBusyAction('release');
      const res = await callbacksApi.release(callback.id);
      onUpdated(res.data.callback);
      toast.success('Released');
    } catch (err: any) {
      const msg = err?.response?.data?.error || 'Failed to release';
      toast.error(msg);
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDial() {
    try {
      setBusyAction('dial');
      const res = await callbacksApi.dial(callback.id);
      onUpdated(res.data.callback);
      toast.success(`Dialing ${callback.phoneNumber}…`);
    } catch (err: any) {
      const code = err?.response?.data?.code;
      const msg = err?.response?.data?.error || 'Failed to dial';
      if (code === 'demo_blocked') {
        toast('Outbound dialing is disabled in demo mode', { icon: 'ℹ️' });
      } else {
        logger.error('Failed to dial callback', err);
        toast.error(msg);
      }
    } finally {
      setBusyAction(null);
    }
  }

  async function handleOutcome() {
    if (!outcome) {
      toast.error('Pick an outcome first');
      return;
    }
    try {
      setBusyAction('outcome');
      const res = await callbacksApi.recordOutcome(callback.id, {
        outcome,
        notes: outcomeNotes || undefined,
        retry: retry && RETRYABLE_OUTCOMES.includes(outcome),
      });
      onUpdated(res.data.callback);
      if (res.data.retry) {
        toast.success('Outcome saved — re-queued for retry');
      } else {
        toast.success('Outcome saved');
      }
      setOutcome('');
      setOutcomeNotes('');
      setRetry(false);
    } catch (err: any) {
      const msg = err?.response?.data?.error || 'Failed to record outcome';
      toast.error(msg);
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <div className="h-full overflow-y-auto bg-canvas">
      {/* Header strip */}
      <div className="px-6 pt-5 pb-4 border-b border-rule">
        <div className="flex items-start justify-between gap-3">
          <div className="flex-1 min-w-0">
            <div className="kicker mb-1">Callback request</div>
            <h1 className="font-display text-[28px] text-ink leading-none tracking-tightest">
              {callback.callerName || callback.contact?.displayName || 'Unknown caller'}
            </h1>
            <div className="mt-1.5 flex items-center gap-3 text-[12px] text-ink-muted">
              <span className="font-mono flex items-center gap-1">
                <Phone className="w-3 h-3" />
                {callback.phoneNumber}
              </span>
              <StatusChip callback={callback} />
              {callback.queueId && (
                <span className="font-mono uppercase tracking-wider text-[10px] text-ink-muted/80">
                  {callback.queueId}
                </span>
              )}
            </div>
          </div>
          <button
            onClick={onClose}
            className="p-2 rounded hover:bg-canvas-sunken text-ink-muted"
            title="Close"
          >
            <X className="w-4 h-4" />
          </button>
        </div>
      </div>

      <div className="px-6 py-5 space-y-5 max-w-3xl">
        {/* Action bar */}
        <div className="flex items-center gap-2">
          {!isCompleted && !isExpired && isUnclaimed && (
            <button
              onClick={handleClaim}
              disabled={busyAction !== null}
              className="px-3 py-1.5 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
            >
              Claim
            </button>
          )}
          {!isCompleted && !isExpired && isMine && (
            <button
              onClick={handleRelease}
              disabled={busyAction !== null}
              className="px-3 py-1.5 text-sm rounded border border-rule hover:bg-canvas-sunken transition-colors disabled:opacity-50"
            >
              Release
            </button>
          )}
          {canDial && (
            <button
              onClick={handleDial}
              disabled={busyAction !== null}
              className="flex items-center gap-2 px-4 py-1.5 text-sm rounded bg-green-600 hover:bg-green-500 text-white transition-colors disabled:opacity-50"
            >
              <PhoneCall className="w-3.5 h-3.5" />
              Dial back
            </button>
          )}
          {isCompleted && (
            <span className="flex items-center gap-1.5 text-sm text-ink-muted">
              <CheckCircle2 className="w-4 h-4" />
              Completed
              {callback.outcome && (
                <span className={`ml-2 ${OUTCOME_LABELS[callback.outcome]?.tone}`}>
                  ({OUTCOME_LABELS[callback.outcome]?.label})
                </span>
              )}
            </span>
          )}
          {isExpired && (
            <span className="flex items-center gap-1.5 text-sm text-orange-400">
              <PhoneOff className="w-4 h-4" />
              Expired
            </span>
          )}
        </div>

        {/* Reason / context */}
        <DetailSection title="What the caller wanted" icon={MessageSquare}>
          {callback.reason ? (
            <p className="text-[13.5px] text-ink leading-relaxed">{callback.reason}</p>
          ) : (
            <p className="text-[13px] text-ink-muted italic">
              No reason captured. Caller pressed 2 from the hold IVR.
            </p>
          )}
        </DetailSection>

        {/* AI-captured fields */}
        {aiContextEntries.length > 0 && (
          <DetailSection title="Captured by AI" icon={User}>
            <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[13px]">
              {aiContextEntries.map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-ink-muted capitalize truncate">
                    {key.replace(/_/g, ' ')}
                  </dt>
                  <dd className="text-ink truncate" title={String(value)}>
                    {typeof value === 'object' ? JSON.stringify(value) : String(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </DetailSection>
        )}

        {/* Timing + attempts */}
        <DetailSection title="Timing" icon={Clock}>
          <dl className="grid grid-cols-2 gap-x-6 gap-y-1.5 text-[13px]">
            <Row label="Requested" value={formatTime(callback.requestedAt)} />
            <Row label="Wait so far" value={callback.waitMinutes != null ? `${callback.waitMinutes}m` : '—'} />
            <Row label="Expires" value={formatTime(callback.expiresAt)} />
            <Row label="Attempts" value={String(callback.attempts + (isCompleted ? 0 : 1))} />
          </dl>
        </DetailSection>

        {/* Outcome form — shown when claimed by current user, after dial, before completion. */}
        {isMine && !isCompleted && (
          <DetailSection title="Record outcome" icon={CheckCircle2}>
            <div className="space-y-3">
              <select
                value={outcome}
                onChange={(e) => {
                  const next = e.target.value as CallbackOutcome | '';
                  setOutcome(next);
                  // Auto-toggle retry when picking a retryable outcome to nudge the right behaviour.
                  if (next && RETRYABLE_OUTCOMES.includes(next as CallbackOutcome)) {
                    setRetry(true);
                  } else {
                    setRetry(false);
                  }
                }}
                className="w-full px-3 py-2 text-sm rounded bg-canvas-sunken border border-rule text-ink focus:outline-none focus:ring-1 focus:ring-blue-400/40"
              >
                <option value="">— Select outcome —</option>
                {(Object.keys(OUTCOME_LABELS) as CallbackOutcome[])
                  .filter((o) => o !== 'expired')
                  .map((o) => (
                    <option key={o} value={o}>
                      {OUTCOME_LABELS[o].label}
                    </option>
                  ))}
              </select>
              <textarea
                value={outcomeNotes}
                onChange={(e) => setOutcomeNotes(e.target.value)}
                rows={3}
                placeholder="Notes (optional)…"
                className="w-full px-3 py-2 text-sm rounded bg-canvas-sunken border border-rule text-ink placeholder-ink-muted/60 focus:outline-none focus:ring-1 focus:ring-blue-400/40 resize-none"
              />
              {outcome && RETRYABLE_OUTCOMES.includes(outcome as CallbackOutcome) && (
                <label className="flex items-center gap-2 text-[12px] text-ink-muted cursor-pointer">
                  <input
                    type="checkbox"
                    checked={retry}
                    onChange={(e) => setRetry(e.target.checked)}
                    className="rounded border-rule"
                  />
                  <RotateCw className="w-3 h-3" />
                  Re-queue for another attempt
                </label>
              )}
              <button
                onClick={handleOutcome}
                disabled={!outcome || busyAction !== null}
                className="px-4 py-2 text-sm rounded bg-blue-600 hover:bg-blue-500 text-white transition-colors disabled:opacity-50"
              >
                Save outcome
              </button>
            </div>
          </DetailSection>
        )}

        {/* If someone else has claimed it, make that visible. */}
        {!isMine && !isUnclaimed && !isCompleted && !isExpired && (
          <div className="rounded border border-rule bg-canvas-sunken px-3 py-2 text-[12px] text-ink-muted flex items-center gap-2">
            <AlertCircle className="w-3.5 h-3.5" />
            Claimed by agent #{callback.claimedByAgentId}.
          </div>
        )}

        {/* Existing notes from a prior outcome */}
        {callback.notes && (
          <DetailSection title="Outcome notes" icon={MessageSquare}>
            <p className="text-[13px] text-ink whitespace-pre-wrap">{callback.notes}</p>
          </DetailSection>
        )}
      </div>
    </div>
  );
}

function DetailSection({
  title,
  icon: Icon,
  children,
}: {
  title: string;
  icon: React.ComponentType<{ className?: string }>;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-lg border border-rule bg-canvas-sunken/30 px-4 py-3">
      <h3 className="flex items-center gap-2 text-[11px] uppercase tracking-wider text-ink-muted mb-2">
        <Icon className="w-3 h-3" />
        {title}
      </h3>
      {children}
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="contents">
      <dt className="text-ink-muted">{label}</dt>
      <dd className="text-ink font-mono">{value}</dd>
    </div>
  );
}

function StatusChip({ callback }: { callback: Callback }) {
  const map: Record<string, { label: string; cls: string }> = {
    pending: { label: 'Pending', cls: 'bg-orange-500/15 text-orange-300' },
    claimed: { label: 'Claimed', cls: 'bg-blue-500/15 text-blue-300' },
    completed: { label: 'Completed', cls: 'bg-green-500/15 text-green-300' },
    expired: { label: 'Expired', cls: 'bg-gray-500/15 text-gray-400' },
  };
  const meta = map[callback.status] ?? map.pending;
  return (
    <span className={`text-[10px] uppercase tracking-wider font-mono px-1.5 py-0.5 rounded ${meta.cls}`}>
      {meta.label}
    </span>
  );
}

function formatTime(iso: string | null): string {
  if (!iso) return '—';
  const d = new Date(iso);
  return d.toLocaleString(undefined, { dateStyle: 'short', timeStyle: 'short' });
}
