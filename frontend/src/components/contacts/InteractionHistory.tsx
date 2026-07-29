import { Bot, User, Clock, PhoneIncoming, PhoneOutgoing, Mic, FileText } from 'lucide-react';
import { Interaction } from '../../types/callcenter';

// Friendly label + color for the call-history status chip. Prefers the
// technical ending classification (endReason) for finished calls — much more
// useful than a bare "ended"/"completed" — and falls back to the live status
// for in-progress calls (and for old rows predating end_reason).
const END_REASON_CHIP: Record<string, { label: string; className: string }> = {
  completed:            { label: 'Completed',     className: 'bg-green-500/20 text-green-400' },
  caller_hangup:        { label: 'Caller hung up', className: 'bg-gray-500/20 text-gray-300' },
  agent_hangup:         { label: 'Agent wrapped',  className: 'bg-gray-500/20 text-gray-300' },
  premature_disconnect: { label: 'Dropped early',  className: 'bg-amber-500/20 text-amber-400' },
  abandoned_in_queue:   { label: 'Abandoned',      className: 'bg-amber-500/20 text-amber-400' },
  // Hold timeout took them off hold — they didn't abandon us, so this reads
  // as a queued follow-up rather than a failure. See queue_dispatch.py.
  callback_scheduled:   { label: 'Callback queued', className: 'bg-blue-500/20 text-blue-400' },
  missed:               { label: 'Missed',         className: 'bg-red-500/20 text-red-400' },
  failed:               { label: 'Failed',         className: 'bg-red-500/20 text-red-400' },
};

function statusChip(interaction: Interaction): { label: string; className: string } {
  const isFinished = interaction.status === 'ended' || interaction.status === 'completed';
  if (isFinished && interaction.endReason && END_REASON_CHIP[interaction.endReason]) {
    return END_REASON_CHIP[interaction.endReason];
  }
  // Live / in-progress, or no end_reason recorded — show the raw status.
  if (interaction.status === 'completed') {
    return { label: 'Completed', className: 'bg-green-500/20 text-green-400' };
  }
  if (interaction.status === 'active' || interaction.status === 'ai_active') {
    return { label: interaction.status === 'ai_active' ? 'AI active' : 'Active', className: 'bg-blue-500/20 text-blue-400' };
  }
  return { label: interaction.status, className: 'bg-gray-500/20 text-gray-400' };
}

interface InteractionHistoryProps {
  interactions: Interaction[];
  isLoading: boolean;
  formatDate: (date?: string) => string;
  formatDuration: (seconds?: number) => string;
  onSelectCall: (interaction: Interaction) => void;
}

export function InteractionHistory({
  interactions,
  isLoading,
  formatDate,
  formatDuration,
  onSelectCall,
}: InteractionHistoryProps) {
  if (isLoading) {
    return (
      <div className="p-8 text-center text-gray-400">
        <div className="animate-spin w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full mx-auto mb-2" />
        Loading history...
      </div>
    );
  }

  if (interactions.length === 0) {
    return (
      <div className="p-8 text-center text-gray-400">
        <Clock className="w-12 h-12 mx-auto mb-2 opacity-50" />
        <p>No call history yet</p>
      </div>
    );
  }

  // Helper to render handler chain from legs
  const renderHandlerChain = (interaction: Interaction) => {
    const legs = interaction.legs;
    if (!legs || legs.length === 0) {
      if (interaction.handlerType === 'ai') {
        return (
          <span className="flex items-center gap-1 px-2 py-0.5 bg-purple-500/20 text-purple-400 text-xs rounded-full">
            <Bot className="w-3 h-3" />
            {interaction.aiAgentName || 'AI'}
          </span>
        );
      }
      return null;
    }

    if (legs.length === 1) {
      const leg = legs[0];
      return (
        <span className={`flex items-center gap-1 px-2 py-0.5 text-xs rounded-full ${
          leg.legType === 'ai_agent' ? 'bg-purple-500/20 text-purple-400' : 'bg-green-500/20 text-green-400'
        }`}>
          {leg.legType === 'ai_agent' ? <Bot className="w-3 h-3" /> : <User className="w-3 h-3" />}
          {leg.legType === 'ai_agent' ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}
        </span>
      );
    }

    return (
      <div className="flex items-center gap-1">
        {legs.map((leg, idx) => (
          <div key={leg.id} className="flex items-center">
            <span className={`flex items-center gap-0.5 px-1.5 py-0.5 text-xs rounded ${
              leg.legType === 'ai_agent' ? 'bg-purple-500/20 text-purple-400' : 'bg-green-500/20 text-green-400'
            }`}>
              {leg.legType === 'ai_agent' ? <Bot className="w-3 h-3" /> : <User className="w-3 h-3" />}
              <span className="hidden sm:inline">
                {leg.legType === 'ai_agent' ? (leg.aiAgentName || 'AI') : (leg.userName || 'Agent')}
              </span>
            </span>
            {idx < legs.length - 1 && (
              <span className="text-gray-500 mx-0.5">&rarr;</span>
            )}
          </div>
        ))}
      </div>
    );
  };

  return (
    <div className="divide-y divide-gray-700">
      {interactions.map((interaction) => (
        <div
          key={interaction.id}
          className="p-4 hover:bg-gray-800/50 transition-colors cursor-pointer"
          onClick={() => onSelectCall(interaction)}
        >
          <div className="flex items-start gap-3">
            {/* Direction Icon */}
            <div className={`w-10 h-10 rounded-full flex items-center justify-center ${
              interaction.direction === 'inbound' ? 'bg-blue-500/20' : 'bg-green-500/20'
            }`}>
              {interaction.direction === 'inbound' ? (
                <PhoneIncoming className={`w-5 h-5 ${
                  interaction.direction === 'inbound' ? 'text-blue-400' : 'text-green-400'
                }`} />
              ) : (
                <PhoneOutgoing className="w-5 h-5 text-green-400" />
              )}
            </div>

            {/* Call Info */}
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="font-medium text-white">
                  {interaction.direction === 'inbound' ? 'Inbound' : 'Outbound'} Call
                </span>
                {renderHandlerChain(interaction)}
                {(() => {
                  const chip = statusChip(interaction);
                  return (
                    <span className={`px-2 py-0.5 text-xs rounded-full ${chip.className}`}>
                      {chip.label}
                    </span>
                  );
                })()}
              </div>

              <div className="flex items-center gap-4 mt-1 text-sm text-gray-400">
                <span>{formatDate(interaction.createdAt)}</span>
                {interaction.duration && (
                  <span className="flex items-center gap-1">
                    <Clock className="w-3 h-3" />
                    {formatDuration(interaction.duration)}
                  </span>
                )}
                {interaction.transcriptionActive && (
                  <span className="flex items-center gap-1 text-green-400">
                    <Mic className="w-3 h-3" />
                    Transcribed
                  </span>
                )}
                {interaction.legs && interaction.legs.length > 1 && (
                  <span className="flex items-center gap-1 text-orange-400">
                    <User className="w-3 h-3" />
                    {interaction.legs.length} handlers
                  </span>
                )}
              </div>

              {/* AI Summary */}
              {interaction.summary && (
                <div className="mt-2 p-2 bg-gray-700/50 rounded-lg text-sm text-gray-300">
                  <FileText className="w-4 h-4 inline-block mr-1 text-gray-400" />
                  {interaction.summary}
                </div>
              )}
            </div>

            {/* Sentiment indicator */}
            {interaction.sentimentScore != null && (
              <div className={`text-sm font-medium ${
                interaction.sentimentScore > 0.3 ? 'text-green-400' :
                interaction.sentimentScore < -0.3 ? 'text-red-400' :
                'text-gray-400'
              }`}>
                {interaction.sentimentScore > 0 ? '+' : ''}{interaction.sentimentScore.toFixed(1)}
              </div>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}
