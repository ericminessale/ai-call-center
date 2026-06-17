import { useState, useEffect, useMemo } from 'react';
import { Search, Phone } from 'lucide-react';
import { Call, QueueConfig } from '../../types/callcenter';
import { QueueItemSkeleton } from '../shared/Skeleton';
import { getQueueDisplayName } from '../../lib/queueColors';
import { Button, RailLiveCallRow } from '../restraint';
import type { RestraintStatus } from '../restraint';

interface QueueListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
  onTakeCall: (call: Call) => void;
  isLoading?: boolean;
  queueConfigs?: QueueConfig[];
  /** Id of the queued call open in the detail pane — highlights its row. */
  selectedCallId?: number;
}

export function QueueList({ calls, onSelectCall, onTakeCall, isLoading, queueConfigs, selectedCallId }: QueueListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [queueFilter, setQueueFilter] = useState<string | null>(null);

  const queuePills = useMemo(() => {
    if (!queueConfigs || queueConfigs.length === 0) return [];
    const counts: Record<string, number> = {};
    calls.forEach((c) => {
      const slug = c.queue_id || '';
      if (slug) counts[slug] = (counts[slug] || 0) + 1;
    });
    return queueConfigs.map((q) => ({
      slug: q.slug,
      label: q.display_name,
      count: counts[q.slug] || 0,
    }));
  }, [queueConfigs, calls]);

  const filteredCalls = calls.filter((call) => {
    if (queueFilter && (call.queue_id || '') !== queueFilter) return false;
    if (!searchQuery) return true;
    const query = searchQuery.toLowerCase();
    return (
      call.from_number?.toLowerCase().includes(query) ||
      call.contact?.displayName?.toLowerCase().includes(query) ||
      call.contact?.company?.toLowerCase().includes(query) ||
      call.queue_id?.toLowerCase().includes(query)
    );
  });

  // Urgent / negative first, then oldest-waiting first (a single ordered list,
  // matching RestraintQueue's flat "Waiting" rail).
  const sortedCalls = [...filteredCalls].sort((a, b) => {
    const aNeedsAttention = a.is_urgent || (a.sentiment !== undefined && a.sentiment < -0.3);
    const bNeedsAttention = b.is_urgent || (b.sentiment !== undefined && b.sentiment < -0.3);
    if (aNeedsAttention && !bNeedsAttention) return -1;
    if (!aNeedsAttention && bNeedsAttention) return 1;
    return new Date(a.created_at || 0).getTime() - new Date(b.created_at || 0).getTime();
  });

  return (
    <div className="h-full flex flex-col">
      {/* Rail head — search · queue chips · waiting count */}
      <div className="px-3 pt-3.5 pb-2 flex flex-col gap-2.5">
        <div className="flex items-center gap-2 bg-canvas border border-rule rounded-lg px-2.5 py-2">
          <Search className="w-3.5 h-3.5 text-ink-dim flex-shrink-0" />
          <input
            type="text"
            placeholder="Search queue…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="flex-1 bg-transparent text-[12.5px] text-ink placeholder:text-ink-dim focus:outline-none"
          />
        </div>

        {queuePills.length > 0 && (
          <div className="flex flex-wrap gap-1.5">
            <QueueChip active={queueFilter === null} onClick={() => setQueueFilter(null)}>
              All queues
            </QueueChip>
            {queuePills.map((pill) => {
              const isActive = queueFilter === pill.slug;
              return (
                <QueueChip
                  key={pill.slug}
                  active={isActive}
                  onClick={() => setQueueFilter(isActive ? null : pill.slug)}
                >
                  {pill.label}
                  {pill.count > 0 && <span className="mono text-[9.5px] ml-1 opacity-70">{pill.count}</span>}
                </QueueChip>
              );
            })}
          </div>
        )}

        <div className="flex items-center justify-between px-1">
          <span className="text-[11px] font-medium text-ink-dim">Waiting</span>
          <span className="mono text-[11px] text-ink-dim">{sortedCalls.length}</span>
        </div>
      </div>

      {/* Flat waiting list — over-SLA / urgent marked by the leading dot (rs-lrow) */}
      <div className="flex-1 overflow-y-auto px-2 pb-2 flex flex-col gap-0.5">
        {isLoading ? (
          <div>{Array.from({ length: 3 }).map((_, i) => <QueueItemSkeleton key={i} />)}</div>
        ) : sortedCalls.length === 0 ? (
          <EmptyQueue />
        ) : (
          sortedCalls.map((call) => (
            <QueueRow
              key={call.id}
              call={call}
              onSelect={() => onSelectCall(call)}
              onTake={() => onTakeCall(call)}
              queueConfigs={queueConfigs}
              selected={selectedCallId != null && call.id === selectedCallId}
            />
          ))
        )}
      </div>
    </div>
  );
}

function EmptyQueue() {
  return (
    <div className="p-8 text-center">
      <Phone className="w-5 h-5 mx-auto mb-3 text-ink-dim" />
      <p className="text-[15px] font-semibold text-ink-muted mb-1">Queue is clear</p>
      <p className="text-[12px] text-ink-dim">Callers waiting will show here, urgent first.</p>
    </div>
  );
}

function QueueChip({ children, active, onClick }: { children: React.ReactNode; active: boolean; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className={`inline-flex items-center px-2.5 py-1 rounded-full text-[11px] font-medium border transition-colors ${
        active
          ? 'bg-canvas-hover text-ink border-rule-strong'
          : 'bg-transparent text-ink-muted hover:text-ink border-rule-strong'
      }`}
    >
      {children}
    </button>
  );
}

function QueueRow({
  call,
  onSelect,
  onTake,
  queueConfigs,
  selected,
}: {
  call: Call;
  onSelect: () => void;
  onTake: () => void;
  queueConfigs?: QueueConfig[];
  selected?: boolean;
}) {
  const contactName = call.contact?.displayName || call.from_number || 'Unknown caller';
  const queueSlug = call.queue_id || '';
  const queueDisplayName = queueSlug ? getQueueDisplayName(queueSlug, queueConfigs) : '';
  const isNegativeSentiment = call.sentiment !== undefined && call.sentiment < -0.3;

  // Live-ticking wait time from created_at.
  const [waitTime, setWaitTime] = useState('0:00');
  const [waitSeconds, setWaitSeconds] = useState(0);
  useEffect(() => {
    if (!call.created_at) return;
    const update = () => {
      const totalSec = Math.floor((Date.now() - new Date(call.created_at!).getTime()) / 1000);
      const mins = Math.floor(totalSec / 60);
      const secs = totalSec % 60;
      setWaitTime(`${mins}:${String(secs).padStart(2, '0')}`);
      setWaitSeconds(totalSec);
    };
    update();
    const interval = setInterval(update, 1000);
    return () => clearInterval(interval);
  }, [call.created_at]);

  const isUrgent = call.is_urgent || call.queue_status === 'urgent';
  const needsAttention = isUrgent || isNegativeSentiment;
  // Over-SLA uses the queue's configured threshold (default 60s — matches the
  // floor's "SLA threshold" label), not a hardcoded 120s.
  const slaSec = Number((queueConfigs?.find((q) => q.slug === call.queue_id) as any)?.sla_threshold_seconds) || 60;
  const overSla = waitSeconds > slaSec;
  const s = call.sentiment;
  // Leading dot: red if clearly-negative, amber if urgent / over-SLA / sliding, else silent.
  const dot: RestraintStatus =
    isNegativeSentiment ? 'error'
    : (isUrgent || overSla || (s != null && s < 0)) ? 'warning'
    : 'neutral';
  const handler = call.priority ? { label: `priority ${call.priority}` } : undefined;

  return (
    <RailLiveCallRow
      className="group"
      name={contactName}
      queue={queueDisplayName || undefined}
      handler={handler}
      sentiment={dot}
      duration={waitTime}
      attention={!!needsAttention}
      selected={selected}
      onClick={onSelect}
      trailing={
        <Button
          variant="secondary"
          icon={<Phone className="w-3 h-3" />}
          onClick={(e) => { e.stopPropagation(); onTake(); }}
          className="!px-2.5 !py-1 !text-[11px]"
        >
          Take
        </Button>
      }
    />
  );
}

export default QueueList;
