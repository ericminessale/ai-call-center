import { useState } from 'react';
import { Eye, MessageSquare } from 'lucide-react';
import { Call } from '../../types/callcenter';
import ObserverControls from '../shared/ObserverControls';
import { SegmentedControl, RailLiveCallRow } from '../restraint';
import type { RestraintStatus } from '../restraint';

interface SupervisorPanelProps {
  activeCalls: Call[];
  onSelectCall: (call: Call) => void;
  /** Contact id of the call open in the detail pane — highlights its row. */
  selectedContactId?: number;
}

type ViewFilter = 'all' | 'ai-calls' | 'human-calls' | 'needs-attention';

export function SupervisorPanel({ activeCalls, onSelectCall, selectedContactId }: SupervisorPanelProps) {
  const [viewFilter, setViewFilter] = useState<ViewFilter>('all');

  const filteredCalls = activeCalls.filter((call) => {
    switch (viewFilter) {
      case 'ai-calls':
        return call.status === 'ai_active';
      case 'human-calls':
        return call.handler_type === 'human' && call.status === 'active';
      case 'needs-attention':
        return (
          (call.sentiment !== undefined && call.sentiment < -0.3) ||
          (call.duration && call.duration > 600)
        );
      default:
        return true;
    }
  });

  const aiCalls = filteredCalls.filter((c) => c.status === 'ai_active');
  const humanCalls = filteredCalls.filter((c) => c.handler_type === 'human' && c.status === 'active');

  const needsAttention = activeCalls.filter(
    (c) =>
      (c.sentiment !== undefined && c.sentiment < -0.3) ||
      (c.duration && c.duration > 600)
  ).length;

  const filterButtons: { key: ViewFilter; label: string; count?: number }[] = [
    { key: 'all', label: 'All', count: activeCalls.length },
    { key: 'ai-calls', label: 'AI', count: aiCalls.length },
    { key: 'human-calls', label: 'Agents', count: humanCalls.length },
    { key: 'needs-attention', label: 'Attn', count: needsAttention },
  ];

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  return (
    <div className="h-full flex flex-col">
      {/* Rail head — segmented filter (All/AI/Agents/Attn) + live-call count */}
      <div className="px-3 pt-3.5 pb-2 flex flex-col gap-2.5">
        <SegmentedControl
          value={viewFilter}
          onChange={setViewFilter}
          options={filterButtons.map((filter) => ({
            value: filter.key,
            label: (
              <span className="inline-flex items-center gap-1">
                {filter.label}
                {filter.count !== undefined && filter.count > 0 && (
                  <span className="mono text-[10px] text-ink-dim">{filter.count}</span>
                )}
              </span>
            ),
          }))}
        />
        <div className="flex items-center justify-between px-1">
          <span className="text-[11px] font-medium text-ink-dim">Live calls</span>
          <span className="mono text-[11px] text-ink-dim">{filteredCalls.length}</span>
        </div>
      </div>

      {/* Flat live-call list — one dense row per call (rs-lrow). AI is signalled
          in the subline (✦ + turquoise handler), not a section split. Per-row
          Listen / inject are kept as trailing controls. */}
      <div className="flex-1 overflow-y-auto px-2 pb-2 flex flex-col gap-0.5">
        {filteredCalls.length === 0 ? (
          <EmptyState />
        ) : (
          filteredCalls.map((call) => {
            const isAI = call.status === 'ai_active';
            const name = call.contact?.displayName || call.from_number || 'Unknown';
            const company = call.contact?.company;
            const queue = call.queueId || (call as any).queue_id;
            const needsAttn =
              (call.sentiment !== undefined && call.sentiment < -0.3) ||
              (call.duration && call.duration > 600);
            // 3-tier sentiment dot: clearly-negative → red, sliding (mildly
            // negative) → amber watch, neutral/positive → silent.
            const s = call.sentiment;
            const dot: RestraintStatus =
              s == null ? 'neutral' : s < -0.3 ? 'error' : s < 0 ? 'warning' : 'neutral';
            const handler = isAI
              ? { label: call.ai_agent_name || 'AI', ai: true }
              : company
                ? { label: company }
                : undefined;
            // Hide Listen for bridge-mode calls (no conference to silently join).
            const canListen = !Array.isArray(call.capabilities) || call.capabilities.includes('monitor_listen');
            return (
              <RailLiveCallRow
                key={call.id}
                className="group"
                name={name}
                queue={queue ? String(queue) : undefined}
                handler={handler}
                sentiment={dot}
                duration={formatDuration(call.duration || 0)}
                attention={!!needsAttn}
                selected={selectedContactId != null && call.contact?.id === selectedContactId}
                onClick={() => onSelectCall(call)}
                trailing={
                  <span className="flex items-center gap-1" onClick={(e) => e.stopPropagation()}>
                    {canListen && (
                      <ObserverControls callId={call.id} callType={isAI ? 'ai' : 'human'} compact />
                    )}
                    {isAI && (
                      <button
                        onClick={(e) => { e.stopPropagation(); onSelectCall(call); }}
                        className="p-1 rounded-md bg-transparent hover:bg-canvas-hover text-ai transition-colors border border-rule-strong"
                        title="Inject message"
                      >
                        <MessageSquare className="w-3 h-3" />
                      </button>
                    )}
                  </span>
                }
              />
            );
          })
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="p-8 text-center">
      <Eye className="w-5 h-5 mx-auto mb-3 text-ink-dim" />
      <p className="text-[15px] font-semibold text-ink-muted mb-1">Nothing to monitor</p>
      <p className="text-[12px] text-ink-dim">Live calls appear here as they open.</p>
    </div>
  );
}

export default SupervisorPanel;
