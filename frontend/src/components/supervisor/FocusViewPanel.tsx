import React from 'react';
import { Eye, MessageSquare, PhoneCall, Phone, Clock, User, TrendingUp, TrendingDown } from 'lucide-react';
import { cn, formatPhoneNumber, formatDuration } from '../../lib/utils';
import type { AgentWithCall } from '../../pages/SupervisorDashboard';

interface FocusViewPanelProps {
  agent: AgentWithCall | null;
  onMonitor: (agentId: string) => void;
  onWhisper: (agentId: string) => void;
  onBarge: (agentId: string) => void;
}

export const FocusViewPanel: React.FC<FocusViewPanelProps> = ({
  agent,
  onMonitor,
  onWhisper,
  onBarge
}) => {
  if (!agent) {
    return (
      <div className="h-full flex items-center justify-center">
        <div className="text-center text-gray-400">
          <Eye className="w-12 h-12 mx-auto mb-3 opacity-50" />
          <p className="text-sm">Select an agent to monitor</p>
        </div>
      </div>
    );
  }

  const getSentimentColor = (sentiment?: number) => {
    if (sentiment === undefined) return 'text-gray-600';
    if (sentiment > 0.5) return 'text-green-600';
    if (sentiment < -0.5) return 'text-red-600';
    return 'text-yellow-600';
  };

  const getSentimentLabel = (sentiment?: number) => {
    if (sentiment === undefined) return 'Unknown';
    if (sentiment > 0.5) return 'Positive';
    if (sentiment < -0.5) return 'Negative';
    return 'Neutral';
  };

  const getSentimentEmoji = (sentiment?: number) => {
    if (sentiment === undefined) return '😐';
    if (sentiment > 0.5) return '😊';
    if (sentiment < -0.5) return '😟';
    return '😐';
  };

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 py-3 border-b border-gray-200">
        <h3 className="text-sm font-semibold text-gray-900">Focus View</h3>
        <p className="text-xs text-gray-600">{agent.name}</p>
      </div>

      {/* Content */}
      <div className="flex-1 overflow-y-auto p-4 space-y-4">
        {/* Agent Status */}
        <div className="bg-gray-50 rounded-lg p-3">
          <div className="flex items-center justify-between mb-2">
            <span className="text-xs font-medium text-gray-600">Status</span>
            <span className={cn(
              "text-xs font-semibold uppercase",
              agent.activeCall ? "text-green-600" : "text-gray-600"
            )}>
              {agent.activeCall ? 'On Call' : agent.status}
            </span>
          </div>
          <div className="flex items-center space-x-2 text-sm">
            <User className="w-4 h-4 text-gray-400" />
            <span className="text-gray-700">{agent.email}</span>
          </div>
          <div className="flex items-center space-x-2 text-sm mt-1">
            <span className="text-gray-600">Queues:</span>
            <span className="text-gray-900 font-medium">{agent.queues.join(', ')}</span>
          </div>
        </div>

        {/* Active Call Info */}
        {agent.activeCall ? (
          <>
            <div className="bg-blue-50 rounded-lg p-3 border border-blue-200">
              <div className="text-xs font-medium text-blue-900 mb-2">Active Call</div>

              {/* Phone Number */}
              <div className="flex items-center space-x-2 mb-2">
                <Phone className="w-4 h-4 text-blue-600" />
                <span className="text-sm font-semibold text-blue-900">
                  {formatPhoneNumber(agent.activeCall.phoneNumber)}
                </span>
              </div>

              {/* Duration */}
              <div className="flex items-center space-x-2 mb-2">
                <Clock className="w-4 h-4 text-blue-600" />
                <span className="text-sm text-blue-900">
                  {formatDuration((agent.callDuration || 0) * 1000)}
                </span>
              </div>

              {/* Queue */}
              {agent.activeCall.queueId && (
                <div className="text-xs text-blue-700">
                  Queue: {agent.activeCall.queueId}
                </div>
              )}
            </div>

            {/* Sentiment Analysis */}
            {agent.sentiment !== undefined && (
              <div className="bg-white rounded-lg p-3 border border-gray-200">
                <div className="text-xs font-medium text-gray-600 mb-2">
                  Sentiment Analysis
                </div>
                <div className="flex items-center justify-between">
                  <div className="flex items-center space-x-2">
                    <span className="text-3xl">{getSentimentEmoji(agent.sentiment)}</span>
                    <div>
                      <div className={cn("text-sm font-semibold", getSentimentColor(agent.sentiment))}>
                        {getSentimentLabel(agent.sentiment)}
                      </div>
                      <div className="text-xs text-gray-500">
                        Score: {agent.sentiment.toFixed(2)}
                      </div>
                    </div>
                  </div>
                  {agent.sentiment < -0.3 ? (
                    <TrendingDown className="w-5 h-5 text-red-500" />
                  ) : agent.sentiment > 0.3 ? (
                    <TrendingUp className="w-5 h-5 text-green-500" />
                  ) : null}
                </div>

                {/* Sentiment Alert */}
                {agent.sentiment < -0.5 && (
                  <div className="mt-3 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-800">
                    ⚠️ Negative sentiment detected - consider intervention
                  </div>
                )}
              </div>
            )}

            {/* Live Transcription Preview */}
            <div className="bg-gray-900 rounded-lg p-3">
              <div className="text-xs font-medium text-gray-300 mb-2">
                Live Transcription
              </div>
              {agent.activeCall.transcription && agent.activeCall.transcription.length > 0 ? (
                <div className="space-y-2 max-h-40 overflow-y-auto">
                  {agent.activeCall.transcription.slice(-5).map((entry, idx) => (
                    <div key={idx} className="text-xs">
                      <span className={cn(
                        "font-semibold",
                        entry.speaker === 'agent' ? 'text-blue-400' : 'text-green-400'
                      )}>
                        {entry.speaker === 'agent' ? 'Agent:' : 'Caller:'}
                      </span>
                      <span className="text-gray-300 ml-2">{entry.text}</span>
                    </div>
                  ))}
                </div>
              ) : (
                <div className="text-xs text-gray-500">
                  No transcription available
                </div>
              )}
            </div>

            {/* AI Coaching Suggestions */}
            <div className="bg-purple-50 rounded-lg p-3 border border-purple-200">
              <div className="text-xs font-medium text-purple-900 mb-2">
                💡 AI Coaching Suggestions
              </div>
              <ul className="space-y-1 text-xs text-purple-800">
                {agent.sentiment !== undefined && agent.sentiment < -0.3 ? (
                  <>
                    <li>• Acknowledge customer's frustration</li>
                    <li>• Use empathetic language</li>
                    <li>• Offer concrete solution steps</li>
                  </>
                ) : (
                  <>
                    <li>• Maintain positive rapport</li>
                    <li>• Confirm customer satisfaction</li>
                    <li>• Offer additional assistance</li>
                  </>
                )}
              </ul>
            </div>
          </>
        ) : (
          <div className="text-center text-gray-400 py-8">
            <Phone className="w-8 h-8 mx-auto mb-2 opacity-50" />
            <p className="text-sm">No active call</p>
          </div>
        )}
      </div>

      {/* Intervention Controls */}
      {agent.activeCall && (
        <div className="px-4 py-3 border-t border-gray-200 bg-gray-50">
          <div className="text-xs font-medium text-gray-600 mb-2">
            Intervention Controls
          </div>
          <div className="grid grid-cols-3 gap-2">
            <button
              onClick={() => onMonitor(agent.id)}
              className="flex flex-col items-center justify-center p-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors"
            >
              <Eye className="w-5 h-5 mb-1" />
              <span className="text-xs font-medium">Monitor</span>
            </button>
            <button
              onClick={() => onWhisper(agent.id)}
              className="flex flex-col items-center justify-center p-3 bg-white border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <MessageSquare className="w-5 h-5 mb-1" />
              <span className="text-xs font-medium">Whisper</span>
            </button>
            <button
              onClick={() => onBarge(agent.id)}
              className="flex flex-col items-center justify-center p-3 bg-white border border-gray-300 text-gray-700 rounded-lg hover:bg-gray-50 transition-colors"
            >
              <PhoneCall className="w-5 h-5 mb-1" />
              <span className="text-xs font-medium">Barge-In</span>
            </button>
          </div>
        </div>
      )}
    </div>
  );
};
