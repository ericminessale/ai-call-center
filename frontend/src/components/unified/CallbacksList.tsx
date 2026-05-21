import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  PhoneCall,
  Clock,
  RefreshCw,
  AlertCircle,
  Loader2,
  CheckCircle2,
  UserCheck,
} from 'lucide-react';
import { callbacksApi, Callback } from '../../services/api';
import { useSocketContext } from '../../contexts/SocketContext';
import { logger } from '../../lib/logger';

// =============================================================================
// CallbacksList — left-panel surface for the Callback System (Tier 2r).
//
// Shows pending callbacks oldest-first with caller name, reason, wait time,
// and quick-action buttons. Selecting a row opens the detail pane on the
// right (CallbackDetail). Real-time updates via the `callback_event`
// socket channel.
// =============================================================================

type Filter = 'pending' | 'mine' | 'completed';

interface CallbacksListProps {
  selectedId?: number | null;
  onSelect: (callback: Callback) => void;
  /** Notify parent when the pending count changes — for the header badge. */
  onPendingCountChange?: (count: number) => void;
  /** Optional one-shot filter override (e.g. from a deep link). Applied
   *  once on prop change, then onForceFilterAck() fires so the parent can
   *  clear it. */
  forceFilter?: Filter | null;
  onForceFilterAck?: () => void;
}

export function CallbacksList({
  selectedId,
  onSelect,
  onPendingCountChange,
  forceFilter,
  onForceFilterAck,
}: CallbacksListProps) {
  const [filter, setFilter] = useState<Filter>('pending');

  // Honour an external filter override from a deep link, then ack the parent.
  useEffect(() => {
    if (forceFilter && forceFilter !== filter) {
      setFilter(forceFilter);
    }
    if (forceFilter && onForceFilterAck) {
      onForceFilterAck();
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [forceFilter]);
  const [callbacks, setCallbacks] = useState<Callback[]>([]);
  const [isLoading, setIsLoading] = useState(false);
  const { socket } = useSocketContext();

  const load = useCallback(async () => {
    setIsLoading(true);
    try {
      const params: Parameters<typeof callbacksApi.list>[0] =
        filter === 'mine' ? { mine: true, status: 'claimed' } :
        filter === 'completed' ? { status: 'completed', limit: 100 } :
        { status: 'pending', limit: 100 };
      const res = await callbacksApi.list(params);
      setCallbacks(res.data.callbacks);
    } catch (err) {
      logger.error('Failed to load callbacks', err);
    } finally {
      setIsLoading(false);
    }
  }, [filter]);

  useEffect(() => {
    load();
  }, [load]);

  // Listen for real-time updates so the list stays current without polling.
  useEffect(() => {
    if (!socket) return;
    const handler = () => load();
    socket.on('callback_event', handler);
    return () => {
      socket.off('callback_event', handler);
    };
  }, [socket, load]);

  // Surface a pending count for the header badge — only counts the "true
  // pending" set (unclaimed, unexpired) regardless of the active filter so
  // the badge is consistent.
  useEffect(() => {
    let cancelled = false;
    callbacksApi
      .pendingCount()
      .then((res) => {
        if (!cancelled) onPendingCountChange?.(res.data.pending);
      })
      .catch((err) => logger.error('Failed to load callback pending count', err));
    return () => {
      cancelled = true;
    };
  }, [callbacks, onPendingCountChange]);

  const visible = useMemo(() => callbacks, [callbacks]);

  return (
    <div className="h-full flex flex-col bg-canvas">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-rule">
        <div className="flex items-center justify-between mb-2">
          <h2 className="font-display text-[18px] text-ink leading-none tracking-tightest">
            Callbacks
          </h2>
          <button
            onClick={() => load()}
            disabled={isLoading}
            className="p-1.5 rounded hover:bg-canvas-sunken text-ink-muted disabled:opacity-50"
            title="Refresh"
          >
            {isLoading ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
          </button>
        </div>
        <div className="flex items-center gap-1 text-[11px]">
          <FilterPill active={filter === 'pending'} onClick={() => setFilter('pending')}>
            Pending
          </FilterPill>
          <FilterPill active={filter === 'mine'} onClick={() => setFilter('mine')}>
            Mine
          </FilterPill>
          <FilterPill active={filter === 'completed'} onClick={() => setFilter('completed')}>
            Done
          </FilterPill>
        </div>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading && callbacks.length === 0 ? (
          <div className="p-12 text-center text-ink-muted">
            <Loader2 className="w-5 h-5 animate-spin mx-auto mb-2" />
            <span className="text-xs">Loading…</span>
          </div>
        ) : visible.length === 0 ? (
          <EmptyState filter={filter} />
        ) : (
          <ul className="divide-y divide-rule/50">
            {visible.map((cb) => (
              <li key={cb.id}>
                <CallbackRow
                  callback={cb}
                  selected={selectedId === cb.id}
                  onClick={() => onSelect(cb)}
                />
              </li>
            ))}
          </ul>
        )}
      </div>
    </div>
  );
}

function FilterPill({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`px-2 py-1 rounded transition-colors ${
        active
          ? 'bg-canvas-sunken text-ink'
          : 'text-ink-muted hover:text-ink hover:bg-canvas-sunken/50'
      }`}
    >
      {children}
    </button>
  );
}

function CallbackRow({
  callback,
  selected,
  onClick,
}: {
  callback: Callback;
  selected: boolean;
  onClick: () => void;
}) {
  const wait = callback.waitMinutes ?? 0;
  const isUrgent = wait > 60 && callback.status === 'pending';

  return (
    <button
      onClick={onClick}
      className={`w-full text-left px-4 py-3 transition-colors ${
        selected
          ? 'bg-canvas-sunken'
          : 'hover:bg-canvas-sunken/50'
      }`}
    >
      <div className="flex items-start gap-3">
        <div className={`mt-0.5 w-7 h-7 rounded-full flex items-center justify-center flex-shrink-0 ${
          callback.status === 'completed' ? 'bg-green-500/15 text-green-400' :
          callback.status === 'claimed' ? 'bg-blue-500/15 text-blue-400' :
          isUrgent ? 'bg-red-500/15 text-red-400' :
          'bg-orange-500/15 text-orange-400'
        }`}>
          {callback.status === 'completed' ? (
            <CheckCircle2 className="w-3.5 h-3.5" />
          ) : callback.status === 'claimed' ? (
            <UserCheck className="w-3.5 h-3.5" />
          ) : (
            <PhoneCall className="w-3.5 h-3.5" />
          )}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-baseline justify-between gap-2 mb-0.5">
            <span className="text-[13px] font-medium text-ink truncate">
              {callback.callerName || callback.contact?.displayName || callback.phoneNumber}
            </span>
            <span className={`text-[10px] flex items-center gap-0.5 flex-shrink-0 ${
              isUrgent ? 'text-red-400' : 'text-ink-muted'
            }`}>
              <Clock className="w-2.5 h-2.5" />
              {formatWait(wait)}
            </span>
          </div>
          <div className="text-[11px] text-ink-muted truncate font-mono">
            {callback.phoneNumber}
          </div>
          {callback.reason && (
            <div className="text-[11px] text-ink-muted/80 mt-1 line-clamp-2 leading-snug">
              {callback.reason}
            </div>
          )}
          <div className="flex items-center gap-2 mt-1.5 text-[10px] text-ink-muted/70 font-mono uppercase tracking-wider">
            {callback.queueId && <span>{callback.queueId}</span>}
            {callback.attempts > 0 && (
              <span className="text-orange-400">attempt #{callback.attempts + 1}</span>
            )}
            {callback.outcome && (
              <span className={callback.outcome === 'success' ? 'text-green-400' : 'text-ink-muted'}>
                {callback.outcome}
              </span>
            )}
          </div>
        </div>
      </div>
    </button>
  );
}

function EmptyState({ filter }: { filter: Filter }) {
  const messages: Record<Filter, { title: string; subtitle: string }> = {
    pending: {
      title: 'No callbacks pending',
      subtitle: "When a caller waits past the queue threshold and presses 2, they'll show up here.",
    },
    mine: {
      title: 'You haven’t claimed any',
      subtitle: 'Claim one from Pending to start dialing.',
    },
    completed: {
      title: 'No completed callbacks yet',
      subtitle: 'Records of dialled callbacks (success / no-answer / etc.) appear here.',
    },
  };
  const meta = messages[filter];
  return (
    <div className="p-12 text-center text-ink-muted">
      <AlertCircle className="w-8 h-8 mx-auto mb-3 opacity-40" />
      <p className="text-sm font-medium text-ink mb-1">{meta.title}</p>
      <p className="text-xs leading-relaxed max-w-xs mx-auto">{meta.subtitle}</p>
    </div>
  );
}

function formatWait(minutes: number): string {
  if (minutes < 1) return '<1m';
  if (minutes < 60) return `${minutes}m`;
  const h = Math.floor(minutes / 60);
  const m = minutes % 60;
  return m === 0 ? `${h}h` : `${h}h${m}m`;
}
