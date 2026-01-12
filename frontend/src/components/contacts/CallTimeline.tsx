import { Bot, User, Clock, ArrowRight } from 'lucide-react';
import { CallLeg } from '../../types/callcenter';

// Re-export CallLeg for convenience
export type { CallLeg };

interface CallTimelineProps {
  legs: CallLeg[];
}

export function CallTimeline({ legs }: CallTimelineProps) {
  if (!legs || legs.length === 0) {
    return null;
  }

  const formatDuration = (seconds?: number) => {
    if (!seconds) return '--';
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
  };

  const formatTime = (dateString?: string) => {
    if (!dateString) return '--';
    return new Date(dateString).toLocaleTimeString();
  };

  const getTransitionLabel = (reason?: string) => {
    switch (reason) {
      case 'takeover':
        return 'Agent took over';
      case 'transfer':
        return 'Transferred';
      case 'customer_request':
        return 'Customer requested';
      case 'hangup':
        return 'Call ended';
      default:
        return reason || '';
    }
  };

  return (
    <div className="mt-4 space-y-1">
      <h4 className="text-sm font-semibold text-gray-300 mb-3">Call Journey</h4>
      <div className="relative">
        {/* Vertical line */}
        <div className="absolute left-4 top-4 bottom-4 w-0.5 bg-gray-700" />

        {legs.map((leg, index) => (
          <div key={leg.id} className="relative flex items-start gap-3 pb-4">
            {/* Icon */}
            <div
              className={`relative z-10 w-8 h-8 rounded-full flex items-center justify-center flex-shrink-0 ${
                leg.legType === 'ai_agent'
                  ? 'bg-purple-500/20 border-2 border-purple-500'
                  : 'bg-green-500/20 border-2 border-green-500'
              }`}
            >
              {leg.legType === 'ai_agent' ? (
                <Bot className="w-4 h-4 text-purple-400" />
              ) : (
                <User className="w-4 h-4 text-green-400" />
              )}
            </div>

            {/* Content */}
            <div className="flex-1 min-w-0 pt-0.5">
              <div className="flex items-center gap-2 flex-wrap">
                {/* Handler name */}
                <span
                  className={`font-medium ${
                    leg.legType === 'ai_agent' ? 'text-purple-400' : 'text-green-400'
                  }`}
                >
                  {leg.legType === 'ai_agent'
                    ? leg.aiAgentName || 'AI Agent'
                    : leg.userName || 'Human Agent'}
                </span>

                {/* Leg number badge */}
                <span className="text-xs px-1.5 py-0.5 bg-gray-700 text-gray-400 rounded">
                  Leg {leg.legNumber}
                </span>

                {/* Status badge */}
                <span
                  className={`text-xs px-1.5 py-0.5 rounded ${
                    leg.status === 'active'
                      ? 'bg-green-500/20 text-green-400'
                      : leg.status === 'connecting'
                      ? 'bg-yellow-500/20 text-yellow-400'
                      : 'bg-gray-600 text-gray-300'
                  }`}
                >
                  {leg.status === 'active' ? 'Active' : leg.status === 'connecting' ? 'Connecting' : 'Completed'}
                </span>
              </div>

              {/* Time and duration */}
              <div className="flex items-center gap-3 mt-1 text-xs text-gray-500">
                <span>{formatTime(leg.startedAt)}</span>
                {leg.duration && (
                  <span className="flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatDuration(leg.duration)}
                  </span>
                )}
              </div>

              {/* Transition reason */}
              {leg.transitionReason && index < legs.length - 1 && (
                <div className="flex items-center gap-1 mt-2 text-xs text-gray-500">
                  <ArrowRight className="w-3 h-3" />
                  {getTransitionLabel(leg.transitionReason)}
                </div>
              )}

              {/* Summary if available */}
              {leg.summary && (
                <div className="mt-2 p-2 bg-gray-800 rounded text-xs text-gray-400">
                  {leg.summary}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>

      {/* Total duration summary */}
      {legs.length > 1 && (
        <div className="mt-2 pt-2 border-t border-gray-700 flex items-center justify-between text-xs text-gray-500">
          <span>{legs.length} handlers involved</span>
          <span className="flex items-center gap-1">
            <Clock className="w-3 h-3" />
            Total: {formatDuration(legs.reduce((sum, leg) => sum + (leg.duration || 0), 0))}
          </span>
        </div>
      )}
    </div>
  );
}

export default CallTimeline;
