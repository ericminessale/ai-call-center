import { useState } from 'react';
import {
  Eye,
  Bot,
  User,
  Phone,
  MessageSquare,
  Clock,
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

  // Filter calls
  const filteredCalls = activeCalls.filter((call) => {
    switch (viewFilter) {
      case 'ai-calls':
        return call.status === 'ai_active';
      case 'human-calls':
        return call.handler_type === 'human' && call.status === 'active';
      case 'needs-attention':
        // Calls with negative sentiment or long duration
        return (
          (call.sentiment !== undefined && call.sentiment < -0.3) ||
          (call.duration && call.duration > 600) // > 10 minutes
        );
      default:
        return true;
    }
  });

  // Separate AI and human calls
  const aiCalls = filteredCalls.filter((c) => c.status === 'ai_active');
  const humanCalls = filteredCalls.filter(
    (c) => c.handler_type === 'human' && c.status === 'active'
  );

  // Quick stats
  const needsAttention = activeCalls.filter(
    (c) =>
      (c.sentiment !== undefined && c.sentiment < -0.3) ||
      (c.duration && c.duration > 600)
  ).length;

  const filterButtons: { key: ViewFilter; label: string; count?: number }[] = [
    { key: 'all', label: 'All Calls', count: activeCalls.length },
    { key: 'ai-calls', label: 'AI Agents', count: aiCalls.length },
    { key: 'human-calls', label: 'Agents', count: humanCalls.length },
    { key: 'needs-attention', label: 'Attention', count: needsAttention },
  ];

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="p-3 border-b border-gray-700">
        <h2 className="text-sm font-semibold text-white mb-3 flex items-center gap-2">
          <Eye className="w-4 h-4 text-blue-400" />
          Supervisor View
        </h2>

        {/* Filter Buttons */}
        <div className="flex flex-wrap gap-1">
          {filterButtons.map((filter) => (
            <button
              key={filter.key}
              onClick={() => setViewFilter(filter.key)}
              className={`px-2 py-1 text-xs rounded transition-colors ${
                viewFilter === filter.key
                  ? filter.key === 'needs-attention'
                    ? 'bg-red-600 text-white'
                    : 'bg-blue-600 text-white'
                  : 'bg-gray-700 text-gray-400 hover:bg-gray-600'
              }`}
            >
              {filter.label}
              {filter.count !== undefined && filter.count > 0 && (
                <span className="ml-1">({filter.count})</span>
              )}
            </button>
          ))}
        </div>
      </div>

      {/* Quick Stats */}
      <div className="grid grid-cols-3 gap-2 p-3 border-b border-gray-700">
        <div className="text-center p-2 bg-gray-700/50 rounded">
          <div className="text-lg font-bold text-white">{activeCalls.length}</div>
          <div className="text-xs text-gray-400">Active</div>
        </div>
        <div className="text-center p-2 bg-purple-900/30 rounded">
          <div className="text-lg font-bold text-purple-400">
            {aiCalls.length}
          </div>
          <div className="text-xs text-gray-400">AI Calls</div>
        </div>
        <div className="text-center p-2 bg-red-900/30 rounded">
          <div className="text-lg font-bold text-red-400">{needsAttention}</div>
          <div className="text-xs text-gray-400">Attention</div>
        </div>
      </div>

      {/* Call List */}
      <div className="flex-1 overflow-y-auto">
        {filteredCalls.length === 0 ? (
          <div className="p-4 text-center text-gray-400">
            <Eye className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No calls to monitor</p>
          </div>
        ) : (
          <>
            {/* AI Calls Section */}
            {aiCalls.length > 0 && (
              <div className="mb-2">
                <div className="px-3 py-2 text-xs font-semibold text-purple-400 uppercase tracking-wider bg-purple-900/20 flex items-center gap-2">
                  <Bot className="w-3 h-3" />
                  AI Agent Calls ({aiCalls.length})
                </div>
                {aiCalls.map((call) => (
                  <SupervisorCallCard
                    key={call.id}
                    call={call}
                    onClick={() => onSelectCall(call)}
                  />
                ))}
              </div>
            )}

            {/* Human Agent Calls Section */}
            {humanCalls.length > 0 && (
              <div>
                <div className="px-3 py-2 text-xs font-semibold text-blue-400 uppercase tracking-wider bg-blue-900/20 flex items-center gap-2">
                  <User className="w-3 h-3" />
                  Agent Calls ({humanCalls.length})
                </div>
                {humanCalls.map((call) => (
                  <SupervisorCallCard
                    key={call.id}
                    call={call}
                    onClick={() => onSelectCall(call)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function SupervisorCallCard({
  call,
  onClick,
}: {
  call: Call;
  onClick: () => void;
}) {
  const isAI = call.status === 'ai_active';
  const contactName = call.contact?.displayName || call.from_number || 'Unknown';
  const company = call.contact?.company;
  const isVip = call.contact?.isVip;

  // Determine sentiment indicator
  const getSentimentIndicator = () => {
    if (call.sentiment === undefined) return null;
    if (call.sentiment > 0.3)
      return { icon: TrendingUp, color: 'text-green-400', label: 'Positive' };
    if (call.sentiment < -0.3)
      return { icon: TrendingDown, color: 'text-red-400', label: 'Negative' };
    return { icon: Minus, color: 'text-gray-400', label: 'Neutral' };
  };

  const sentiment = getSentimentIndicator();

  // Check if needs attention
  const needsAttention =
    (call.sentiment !== undefined && call.sentiment < -0.3) ||
    (call.duration && call.duration > 600);

  // Format duration
  const formatDuration = (seconds: number) => {
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${String(secs).padStart(2, '0')}`;
  };

  return (
    <button
      onClick={onClick}
      className={`w-full px-3 py-3 text-left hover:bg-gray-700/50 transition-colors border-l-2 ${
        needsAttention ? 'border-red-500 bg-red-900/10' : 'border-transparent'
      }`}
    >
      <div className="flex items-start gap-3">
        {/* Avatar */}
        <div
          className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-medium flex-shrink-0 ${
            isAI ? 'bg-purple-600' : 'bg-blue-600'
          }`}
        >
          {isAI ? <Bot className="w-5 h-5" /> : contactName.charAt(0).toUpperCase()}
        </div>

        {/* Info */}
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="font-medium text-white truncate">{contactName}</span>
            {isVip && (
              <Star className="w-3 h-3 text-yellow-400 fill-yellow-400 flex-shrink-0" />
            )}
            {needsAttention && (
              <AlertTriangle className="w-3 h-3 text-red-400 flex-shrink-0" />
            )}
          </div>

          <div className="flex items-center gap-2 text-xs text-gray-400 mt-0.5">
            {isAI && call.ai_agent_name && (
              <>
                <Bot className="w-3 h-3" />
                <span>{call.ai_agent_name}</span>
                <span>•</span>
              </>
            )}
            {company && (
              <>
                <Building2 className="w-3 h-3" />
                <span className="truncate">{company}</span>
                <span>•</span>
              </>
            )}
            <Clock className="w-3 h-3" />
            <span>{formatDuration(call.duration || 0)}</span>
          </div>

          {/* Last transcript snippet */}
          {call.ai_summary && (
            <div className="mt-1 text-xs text-gray-500 truncate">
              "{call.ai_summary}"
            </div>
          )}
        </div>

        {/* Sentiment and Actions */}
        <div className="flex flex-col items-end gap-2">
          {sentiment && (
            <div className={`flex items-center gap-1 text-xs ${sentiment.color}`}>
              <sentiment.icon className="w-3 h-3" />
              <span>{sentiment.label}</span>
            </div>
          )}

          {isAI && (
            <div className="flex items-center gap-1">
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  onClick();
                }}
                className="p-1 bg-purple-600/30 hover:bg-purple-600/50 text-purple-400 rounded transition-colors"
                title="Inject Message"
              >
                <MessageSquare className="w-3 h-3" />
              </button>
            </div>
          )}
        </div>
      </div>
    </button>
  );
}

export default SupervisorPanel;
