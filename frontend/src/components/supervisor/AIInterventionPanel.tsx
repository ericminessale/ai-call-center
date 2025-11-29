import React, { useState, useEffect } from 'react';
import { Brain, Send, Clock, User, Bot, AlertCircle, CheckCircle } from 'lucide-react';
import { cn, formatDuration } from '../../lib/utils';
import api from '../../services/api';

interface AICall {
  call_id: string;
  from: string;
  to: string;
  ai_agent: string;
  duration: number;
  start_time: string;
  transcription: TranscriptionEntry[];
  current_sentiment: number;
  can_inject: boolean;
  metadata: any;
}

interface TranscriptionEntry {
  timestamp: string;
  speaker: string;
  text: string;
  confidence?: number;
  sentiment?: number;
}

interface InjectionHistoryItem {
  timestamp: Date;
  message: string;
  result: boolean;
}

interface MessageTemplate {
  id: string;
  label: string;
  message: string;
  category: string;
}

export const AIInterventionPanel: React.FC = () => {
  const [aiCalls, setAiCalls] = useState<AICall[]>([]);
  const [selectedCall, setSelectedCall] = useState<AICall | null>(null);
  const [systemMessage, setSystemMessage] = useState('');
  const [injectionHistory, setInjectionHistory] = useState<InjectionHistoryItem[]>([]);
  const [templates, setTemplates] = useState<MessageTemplate[]>([]);
  const [isInjecting, setIsInjecting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Poll for active AI calls
  useEffect(() => {
    const fetchActiveCalls = async () => {
      try {
        const response = await api.get('/ai/active-sessions');
        setAiCalls(response.data.active_ai_calls || []);
      } catch (error) {
        console.error('Failed to fetch AI calls:', error);
      }
    };

    // Initial fetch
    fetchActiveCalls();

    // Poll every 2 seconds
    const interval = setInterval(fetchActiveCalls, 2000);

    return () => clearInterval(interval);
  }, []);

  // Load message templates
  useEffect(() => {
    const fetchTemplates = async () => {
      try {
        const response = await api.get('/ai/templates');
        setTemplates(response.data.templates || []);
      } catch (error) {
        console.error('Failed to fetch templates:', error);
      }
    };

    fetchTemplates();
  }, []);

  // Load injection history when call is selected
  useEffect(() => {
    if (selectedCall) {
      const fetchHistory = async () => {
        try {
          const response = await api.get(`/ai/injection-history/${selectedCall.call_id}`);
          setInjectionHistory(response.data.history || []);
        } catch (error) {
          console.error('Failed to fetch injection history:', error);
        }
      };

      fetchHistory();
    }
  }, [selectedCall]);

  const injectSystemMessage = async () => {
    if (!selectedCall || !systemMessage) return;

    setIsInjecting(true);
    setError(null);

    try {
      const response = await api.post('/ai/inject-message', {
        call_id: selectedCall.call_id,
        message: systemMessage,
        role: 'system'
      });

      setInjectionHistory(prev => [...prev, {
        timestamp: new Date(),
        message: systemMessage,
        result: response.data.success
      }]);

      setSystemMessage('');

      // Show success notification
      // toast.success('System message injected - AI behavior updating...');

    } catch (error: any) {
      console.error('Failed to inject message:', error);
      setError(error.response?.data?.error || 'Failed to inject message');
    } finally {
      setIsInjecting(false);
    }
  };

  const getSentimentColor = (sentiment: number) => {
    if (sentiment > 0.5) return 'text-green-600';
    if (sentiment < -0.5) return 'text-red-600';
    return 'text-yellow-600';
  };

  const getSentimentEmoji = (sentiment: number) => {
    if (sentiment > 0.5) return '😊';
    if (sentiment < -0.5) return '😟';
    return '😐';
  };

  return (
    <div className="bg-purple-50 rounded-lg p-4 border border-purple-200">
      <div className="flex items-center mb-4">
        <Brain className="w-5 h-5 text-purple-600 mr-2" />
        <h3 className="font-semibold">AI Call Intervention</h3>
        <span className="ml-auto px-2 py-1 bg-purple-600 text-white text-xs rounded-full">
          {aiCalls.length} Active AI Calls
        </span>
      </div>

      {aiCalls.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          <Bot className="w-12 h-12 mx-auto mb-2 opacity-50" />
          <p className="text-sm">No active AI calls</p>
        </div>
      ) : (
        <div className="grid grid-cols-2 gap-4">
          {/* Active AI Calls List */}
          <div className="space-y-2 max-h-96 overflow-y-auto">
            <h4 className="text-sm font-medium text-gray-700 mb-2">Active Calls</h4>
            {aiCalls.map(call => (
              <div
                key={call.call_id}
                onClick={() => setSelectedCall(call)}
                className={cn(
                  'p-3 bg-white rounded-lg border cursor-pointer transition-all',
                  selectedCall?.call_id === call.call_id
                    ? 'border-purple-400 shadow-md ring-2 ring-purple-200'
                    : 'border-gray-200 hover:border-purple-300'
                )}
              >
                <div className="flex justify-between items-start mb-2">
                  <div className="flex-1">
                    <p className="font-medium text-sm">{call.from}</p>
                    <p className="text-xs text-gray-600 flex items-center mt-1">
                      <Bot className="w-3 h-3 mr-1" />
                      {call.ai_agent}
                    </p>
                  </div>
                  <div className="text-right">
                    <div className="flex items-center text-xs text-gray-500 mb-1">
                      <Clock className="w-3 h-3 mr-1" />
                      {formatDuration(call.duration)}
                    </div>
                    <span className="text-lg">
                      {getSentimentEmoji(call.current_sentiment)}
                    </span>
                  </div>
                </div>

                {/* Live Status Indicator */}
                <div className="flex items-center space-x-2">
                  <div className="flex-1 bg-gray-100 rounded-full h-2 overflow-hidden">
                    <div
                      className="bg-purple-500 h-2 rounded-full animate-pulse"
                      style={{ width: '100%' }}
                    />
                  </div>
                  <span className="text-xs text-purple-600 font-medium">LIVE</span>
                </div>
              </div>
            ))}
          </div>

          {/* Intervention Controls */}
          {selectedCall ? (
            <div className="bg-white rounded-lg p-4 border border-purple-200">
              {/* Call Info Header */}
              <div className="mb-4 pb-3 border-b">
                <div className="flex items-center justify-between mb-2">
                  <h4 className="text-sm font-semibold">{selectedCall.from}</h4>
                  <span className="text-xs text-gray-500">
                    {formatDuration(selectedCall.duration)}
                  </span>
                </div>
                <p className="text-xs text-gray-600">{selectedCall.ai_agent}</p>
              </div>

              {/* Live Transcription Preview */}
              <div className="mb-4">
                <h4 className="text-sm font-medium mb-2 flex items-center">
                  <User className="w-3 h-3 mr-1" />
                  Live Conversation
                </h4>
                <div className="h-32 overflow-y-auto bg-gray-50 rounded p-2 text-xs space-y-1">
                  {selectedCall.transcription && selectedCall.transcription.length > 0 ? (
                    selectedCall.transcription.slice(-10).map((entry, idx) => (
                      <div key={idx} className="flex">
                        <span className={cn(
                          "font-medium mr-2",
                          entry.speaker === 'ai' ? 'text-purple-600' : 'text-blue-600'
                        )}>
                          {entry.speaker === 'ai' ? 'AI:' : 'Caller:'}
                        </span>
                        <span className="text-gray-700">{entry.text}</span>
                      </div>
                    ))
                  ) : (
                    <div className="text-center text-gray-400 py-4">
                      <p>Transcription loading...</p>
                    </div>
                  )}
                </div>
              </div>

              {/* System Message Injection */}
              <div className="mb-4">
                <label className="block text-sm font-medium mb-2">
                  Inject System Message
                </label>
                <textarea
                  value={systemMessage}
                  onChange={(e) => setSystemMessage(e.target.value)}
                  placeholder="e.g., 'Offer the customer a 20% discount' or 'Transfer to billing department'"
                  className="w-full p-2 border rounded-lg resize-none text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
                  rows={3}
                  disabled={isInjecting}
                />

                {/* Quick Templates */}
                <div className="mt-2 flex flex-wrap gap-1">
                  {templates.slice(0, 5).map(template => (
                    <button
                      key={template.id}
                      onClick={() => setSystemMessage(template.message)}
                      className="text-xs px-2 py-1 bg-purple-100 text-purple-700 rounded hover:bg-purple-200 transition-colors"
                      disabled={isInjecting}
                    >
                      {template.label}
                    </button>
                  ))}
                </div>

                {error && (
                  <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded text-xs text-red-700 flex items-center">
                    <AlertCircle className="w-3 h-3 mr-1" />
                    {error}
                  </div>
                )}

                <button
                  onClick={injectSystemMessage}
                  disabled={!systemMessage || isInjecting}
                  className={cn(
                    "mt-3 w-full py-2 rounded-lg font-medium transition-colors flex items-center justify-center",
                    systemMessage && !isInjecting
                      ? "bg-purple-600 text-white hover:bg-purple-700"
                      : "bg-gray-300 text-gray-500 cursor-not-allowed"
                  )}
                >
                  {isInjecting ? (
                    <>
                      <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                      Injecting...
                    </>
                  ) : (
                    <>
                      <Send className="w-4 h-4 mr-2" />
                      Inject Message
                    </>
                  )}
                </button>
              </div>

              {/* Injection History */}
              {injectionHistory.length > 0 && (
                <div>
                  <h4 className="text-sm font-medium mb-2">Injection History</h4>
                  <div className="space-y-1 max-h-24 overflow-y-auto">
                    {injectionHistory.slice(-5).map((item, idx) => (
                      <div key={idx} className="text-xs p-2 bg-gray-50 rounded flex items-start">
                        {item.result ? (
                          <CheckCircle className="w-3 h-3 text-green-500 mr-1 mt-0.5 flex-shrink-0" />
                        ) : (
                          <AlertCircle className="w-3 h-3 text-red-500 mr-1 mt-0.5 flex-shrink-0" />
                        )}
                        <div className="flex-1">
                          <span className="text-gray-500">
                            {new Date(item.timestamp).toLocaleTimeString()}:
                          </span>{' '}
                          <span className="text-gray-700">{item.message}</span>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              )}
            </div>
          ) : (
            <div className="bg-white rounded-lg p-4 border border-purple-200 flex items-center justify-center text-gray-400">
              <div className="text-center">
                <Brain className="w-12 h-12 mx-auto mb-2 opacity-50" />
                <p className="text-sm">Select a call to intervene</p>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
};