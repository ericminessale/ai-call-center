import { useState } from 'react';
import {
  Eye,
  Bot,
  User,
  MessageSquare,
  TrendingUp,
  TrendingDown,
  Minus,
  Building2,
  Star,
  AlertTriangle,
} from 'lucide-react';
import { Call } from '../../types/callcenter';

interface SupervisorPanelProps {
  activeCalls: Call[];
  onSelectCall: (call: Call) => void;
}

type ViewFilter = 'all' | 'ai-calls' | 'human-calls' | 'needs-attention';

export function SupervisorPanel({ activeCalls, onSelectCall }: SupervisorPanelProps) {
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

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-rule">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center gap-2">
            <Eye className="w-3.5 h-3.5 text-info-soft" />
            <span className="kicker">Supervisor view</span>
          </div>
          <span className="mono text-[11px] text-ink-dim">{activeCalls.length}</span>
        </div>

        {/* Filter tabs */}
        <div className="flex items-center gap-0 p-0.5 bg-canvas-raised rounded border border-rule">
          {filterButtons.map((filter) => (
            <button
              key={filter.key}
              onClick={() => setViewFilter(filter.key)}
              className={`flex-1 px-1.5 py-1 text-[11.5px] font-medium rounded transition-colors ${
                viewFilter === filter.key
                  ? filter.key === 'needs-attention'
                    ? 'bg-urgent/15 text-ink border border-urgent/30'
                    : 'bg-canvas-elevated text-ink border border-rule-strong'
                  : 'text-ink-dim hover:text-ink-muted border border-transparent'
              }`}
            >
              {filter.label}
              {filter.count !== undefined && filter.count > 0 && (
                <span className="mono text-[10px] ml-1 opacity-80">{filter.count}</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Quick Stats — inline, no grid, no dividers */}
      <div className="px-4 py-4 flex items-baseline gap-8">
        <MiniStat label="Active" value={activeCalls.length} tone="default" />
        <MiniStat label="AI" value={aiCalls.length} tone="ai" />
        <MiniStat label="Attn" value={needsAttention} tone="urgent" />
      </div>

      {/* Call list */}
      <div className="flex-1 overflow-y-auto">
        {filteredCalls.length === 0 ? (
          <EmptyState />
        ) : (
          <>
            {aiCalls.length > 0 && (
              <MonitorSection title="AI agents" icon={<Bot className="w-3 h-3" />} tone="ai" count={aiCalls.length}>
                {aiCalls.map((call) => (
                  <SupervisorCallCard key={call.id} call={call} onClick={() => onSelectCall(call)} />
                ))}
              </MonitorSection>
            )}
            {humanCalls.length > 0 && (
              <MonitorSection title="Human agents" icon={<User className="w-3 h-3" />} tone="info" count={humanCalls.length}>
                {humanCalls.map((call) => (
                  <SupervisorCallCard key={call.id} call={call} onClick={() => onSelectCall(call)} />
                ))}
              </MonitorSection>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function EmptyState() {
  return (
    <div className="p-8 text-center">
      <Eye className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
      <p className="font-display text-[20px] text-ink-muted mb-1">Nothing to monitor</p>
      <p className="text-[12px] text-ink-dim">Live calls appear here as they open.</p>
    </div>
  );
}

function MiniStat({ label, value, tone }: { label: string; value: number; tone: 'default' | 'ai' | 'urgent' }) {
  const numColor =
    tone === 'ai' ? 'text-ai-soft' :
    tone === 'urgent' ? 'text-urgent-soft' : 'text-ink';
  return (
    <div className="flex flex-col gap-0.5 items-start">
      <span className="kicker">{label}</span>
      <span className={`font-heading font-semibold text-[24px] leading-none tabular-nums ${numColor}`}>{value}</span>
    </div>
  );
}

function MonitorSection({
  title,
  icon,
  tone,
  count,
  children,
}: {
  title: string;
  icon: React.ReactNode;
  tone: 'ai' | 'info';
  count: number;
  children: React.ReactNode;
}) {
  const colorClass = tone === 'ai' ? 'text-ai-soft' : 'text-info-soft';
  return (
    <div>
      <div className="sticky top-0 z-10 bg-canvas-sunken/95 backdrop-blur-sm flex items-center justify-between px-4 py-1.5 border-b border-rule">
        <div className={`flex items-center gap-2 ${colorClass}`}>
          {icon}
          <span className="kicker" style={{ color: 'inherit' }}>{title}</span>
        </div>
        <span className="mono text-[10px] text-ink-dim">{count}</span>
      </div>
      {children}
    </div>
  );
}

function SupervisorCallCard({ call, onClick }: { call: Call; onClick: () => void }) {
  const isAI = call.status === 'ai_active';
  const contactName = call.contact?.displayName || call.from_number || 'Unknown';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;

  const getSentimentIndicator = () => {
    if (call.sentiment === undefined) return null;
    if (call.sentiment > 0.3) return { icon: TrendingUp, color: 'text-live-soft', label: 'Positive' };
    if (call.sentiment < -0.3) return { icon: TrendingDown, color: 'text-urgent-soft', label: 'Negative' };
    return { icon: Minus, color: 'text-ink-dim', label: 'Neutral' };
  };

  const sentiment = getSentimentIndicator();
  const needsAttention =
    (call.sentiment !== undefined && call.sentiment < -0.3) ||
    (call.duration && call.duration > 600);

  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  const railColor = needsAttention ? 'border-l-urgent' : isAI ? 'border-l-ai' : 'border-l-live';

  return (
    <button
      onClick={onClick}
      className={`w-full px-4 py-3 text-left hover:bg-canvas-hover/40 transition-colors border-b border-rule/60 border-l-[2px] ${railColor}`}
    >
      <div className="flex items-start gap-3">
        <div className={`w-9 h-9 rounded flex items-center justify-center text-[13px] font-semibold shrink-0 ${
          isAI ? 'bg-ai/15 text-ai-soft border border-ai/30' : 'bg-live/15 text-live-soft border border-live/30'
        }`}>
          {isAI ? <Bot className="w-4 h-4" /> : contactName.charAt(0).toUpperCase()}
        </div>

        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-1.5">
            <span className="font-medium text-ink truncate text-[13.5px]">{contactName}</span>
            {isVip && <Star className="w-3 h-3 text-wait fill-wait flex-shrink-0" />}
            {needsAttention && <AlertTriangle className="w-3 h-3 text-urgent-soft flex-shrink-0" />}
          </div>

          <div className="flex items-center gap-1.5 text-[11.5px] text-ink-dim mt-0.5">
            {isAI && call.ai_agent_name && (
              <>
                <Bot className="w-3 h-3" />
                <span className="truncate mono">{call.ai_agent_name}</span>
                <span className="text-ink-faint">·</span>
              </>
            )}
            {company && (
              <>
                <Building2 className="w-3 h-3" />
                <span className="truncate">{company}</span>
                <span className="text-ink-faint">·</span>
              </>
            )}
            <span className="mono">{formatDuration(call.duration || 0)}</span>
          </div>

          {call.ai_summary && (
            <div className="mt-1 text-[11.5px] text-ink-dim truncate italic">
              "{call.ai_summary}"
            </div>
          )}
        </div>

        <div className="flex flex-col items-end gap-1.5 shrink-0">
          {sentiment && (
            <div className={`flex items-center gap-1 text-[10.5px] font-medium ${sentiment.color}`}>
              <sentiment.icon className="w-3 h-3" />
              <span className="uppercase tracking-wider">{sentiment.label}</span>
            </div>
          )}
          {isAI && (
            <button
              onClick={(e) => { e.stopPropagation(); onClick(); }}
              className="p-1 rounded bg-ai/10 hover:bg-ai/20 text-ai-soft transition-colors border border-ai/25"
              title="Inject message"
            >
              <MessageSquare className="w-3 h-3" />
            </button>
          )}
        </div>
      </div>
    </button>
  );
}

export default SupervisorPanel;
