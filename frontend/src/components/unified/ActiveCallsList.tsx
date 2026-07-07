import { useState, useEffect, useMemo } from 'react';
import { Search, Phone } from 'lucide-react';
import { Call, QueueConfig } from '../../types/callcenter';
import { CallListSkeletonGroup } from '../shared/Skeleton';
import { getQueueDisplayName } from '../../lib/queueColors';
import { SegmentedControl, RailLiveCallRow } from '../restraint';
import type { RestraintStatus } from '../restraint';

interface ActiveCallsListProps {
  calls: Call[];
  onSelectCall: (call: Call) => void;
  isLoading?: boolean;
  queueConfigs?: QueueConfig[];
  /** Contact id of the call open in the detail pane — highlights its row. */
  selectedContactId?: number;
}

type FilterType = 'all' | 'my-calls' | 'ai-active';

export function ActiveCallsList({ calls, onSelectCall, isLoading, queueConfigs, selectedContactId }: ActiveCallsListProps) {
  const [searchQuery, setSearchQuery] = useState('');
  const [activeFilter, setActiveFilter] = useState<FilterType>('all');
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

  // Base = search + queue filtered (NOT the active-tab filter) so the segmented
  // counts stay stable as you switch tabs.
  const baseCalls = calls.filter((call) => {
    if (queueFilter && (call.queue_id || '') !== queueFilter) return false;
    if (searchQuery) {
      const query = searchQuery.toLowerCase();
      const matches =
        call.from_number?.toLowerCase().includes(query) ||
        call.phoneNumber?.toLowerCase().includes(query) ||
        call.contact?.displayName?.toLowerCase().includes(query) ||
        call.contact?.company?.toLowerCase().includes(query);
      if (!matches) return false;
    }
    return true;
  });

  const isMine = (c: Call) => c.handler_type === 'human' && (c.status === 'active' || c.status === 'connecting');
  const isAi = (c: Call) => c.status === 'ai_active' || c.handler_type === 'ai';

  const filteredCalls = baseCalls.filter((call) => {
    switch (activeFilter) {
      case 'my-calls':
        return isMine(call);
      case 'ai-active':
        return isAi(call);
      default:
        return true;
    }
  });

  const filterButtons: { key: FilterType; label: string; count: number }[] = [
    { key: 'all', label: 'All', count: baseCalls.length },
    { key: 'my-calls', label: 'My calls', count: baseCalls.filter(isMine).length },
    { key: 'ai-active', label: 'AI active', count: baseCalls.filter(isAi).length },
  ];

  return (
    <div className="h-full flex flex-col">
      {/* Rail head — search · segmented filter · queue chips · count */}
      <div className="px-3 pt-3.5 pb-2 flex flex-col gap-2.5">
        {/* Search (kept; mockup omits it, but it's a real affordance) */}
        <div className="flex items-center gap-2 bg-canvas border border-rule rounded-lg px-2.5 py-2">
          <Search className="w-3.5 h-3.5 text-ink-dim flex-shrink-0" />
          <input
            type="text"
            placeholder="Search calls…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            className="flex-1 bg-transparent text-[12.5px] text-ink placeholder:text-ink-dim focus:outline-none"
          />
        </div>

        {/* Filter tabs */}
        <SegmentedControl<FilterType>
          value={activeFilter}
          onChange={setActiveFilter}
          options={filterButtons.map((filter) => ({
            value: filter.key,
            label: (
              <span className="inline-flex items-center gap-1">
                {filter.label}
                {filter.count > 0 && <span className="mono text-[10px] text-ink-dim">{filter.count}</span>}
              </span>
            ),
          }))}
        />

        {/* Queue chips — neutral rounded pills (rx-qchip), active = raised */}
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
          <span className="text-[11px] font-medium text-ink-dim">Active</span>
          <span className="mono text-[11px] text-ink-dim">{filteredCalls.length}</span>
        </div>
      </div>

      {/* Flat call list — one dense row per call (rs-lrow) */}
      <div className="flex-1 overflow-y-auto px-2 pb-2 flex flex-col gap-0.5">
        {isLoading ? (
          <CallListSkeletonGroup count={3} />
        ) : filteredCalls.length === 0 ? (
          <EmptyState />
        ) : (
          filteredCalls.map((call) => (
            <ActiveCallRow
              key={call.id}
              call={call}
              onSelectCall={onSelectCall}
              queueConfigs={queueConfigs}
              selected={selectedContactId != null && call.contact?.id === selectedContactId}
            />
          ))
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="p-8 text-center">
      <Phone className="w-5 h-5 mx-auto mb-3 text-ink-dim" />
      <p className="text-[15px] font-semibold text-ink-muted mb-1">All quiet</p>
      <p className="text-[12px] text-ink-dim">No calls in flight right now.</p>
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

function ActiveCallRow({
  call,
  onSelectCall,
  queueConfigs,
  selected,
}: {
  call: Call;
  onSelectCall: (call: Call) => void;
  queueConfigs?: QueueConfig[];
  selected?: boolean;
}) {
  const isAI = call.status === 'ai_active' || call.handler_type === 'ai';
  const isConnecting = call.status === 'connecting' || call.status === 'ringing';
  const contactName = call.contact?.displayName || call.from_number || 'Unknown';
  const queueSlug = call.queue_id || '';
  const isNegativeSentiment = call.sentiment !== undefined && call.sentiment < -0.3;

  // True elapsed time since the call started — derived from the call's start
  // timestamp, NOT a mount-seeded counter. The old code reset to call.duration
  // (null while a call is live) and counted up from 0 on every component mount,
  // so the timer restarted at 0:00 every time the Active Calls tab was opened.
  // Prefer answeredAt (when audio began) → createdAt → startTime; REST ships
  // camelCase, the Call type declares snake_case, so read both.
  const startMs = (() => {
    const raw = (call as any).answeredAt || (call as any).answered_at
      || call.created_at || (call as any).createdAt || (call as any).startTime;
    const t = raw ? new Date(raw).getTime() : NaN;
    return Number.isFinite(t) ? t : null;
  })();
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (isConnecting) return;
    setNow(Date.now());
    const interval = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(interval);
  }, [isConnecting]);
  const liveDuration = startMs != null
    ? Math.max(0, Math.floor((now - startMs) / 1000))
    : (call.duration || 0);

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  // 3-tier sentiment dot: clearly-negative → red, sliding (mildly negative) →
  // amber watch, neutral/positive → silent. Connecting (no sentiment yet) → amber.
  const s = call.sentiment;
  const dot: RestraintStatus =
    isConnecting ? 'warning'
    : s == null ? 'neutral'
    : s < -0.3 ? 'error'
    : s < 0 ? 'warning'
    : 'neutral';
  const queueDisplayName = queueSlug ? getQueueDisplayName(queueSlug, queueConfigs) : '';
  // Show the real handler identity: the AI specialist's name (✦ Support AI) for
  // AI legs; human legs fall back to status (we don't carry the agent name here).
  const handler = isAI
    ? { label: call.ai_agent_name || 'AI agent', ai: true }
    : { label: isConnecting ? 'Connecting' : 'Live' };

  return (
    <RailLiveCallRow
      className="group"
      name={contactName}
      queue={queueDisplayName || undefined}
      handler={handler}
      sentiment={dot}
      duration={isConnecting ? '—:—' : formatDuration(liveDuration)}
      attention={isNegativeSentiment}
      selected={selected}
      onClick={() => onSelectCall(call)}
    />
  );
}

export default ActiveCallsList;
