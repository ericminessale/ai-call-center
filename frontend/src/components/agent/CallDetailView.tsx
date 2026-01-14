import React, { useState, useEffect } from 'react';
import { Phone, PhoneOff, Pause, Play, Users, MessageSquare, User, Bot, Settings, Send, AlertCircle, ArrowLeft } from 'lucide-react';
import { cn, formatPhoneNumber, formatDuration } from '../../lib/utils';
import type { Call } from '../../types/callcenter';
import api from '../../services/api';

interface CallDetailViewProps {
  call: Call;
  onEndCall: () => void;
  onBack?: () => void;
}

type TabType = 'transcription' | 'customer' | 'ai-tools' | 'history';

export const CallDetailView: React.FC<CallDetailViewProps> = ({ call, onEndCall, onBack }) => {
  const [activeTab, setActiveTab] = useState<TabType>('transcription');
  const [isOnHold, setIsOnHold] = useState(false);
  const [isMuted, setIsMuted] = useState(false);
  const [duration, setDuration] = useState(0);

  const isCallActive = call.status !== 'completed' && call.status !== 'ended';

  // Calculate call duration
  useEffect(() => {
    if (!call.startTime) return;

    const interval = setInterval(() => {
      const start = new Date(call.startTime!).getTime();
      const now = Date.now();
      setDuration(Math.floor((now - start) / 1000));
    }, 1000);

    return () => clearInterval(interval);
  }, [call.startTime]);

  const handleHold = () => {
    // TODO: Integrate with SignalWire Call Fabric
    setIsOnHold(!isOnHold);
  };

  const handleMute = () => {
    // TODO: Integrate with SignalWire Call Fabric
    setIsMuted(!isMuted);
  };

  const handleTransfer = () => {
    // TODO: Open transfer panel
  };

  return (
    <div className="h-full flex flex-col bg-white">
      {/* Call Header */}
      <div className="px-6 py-4 border-b border-gray-200">
        <div className="flex items-center justify-between mb-3">
          <div className="flex items-center space-x-4">
            {onBack && (
              <button
                onClick={onBack}
                className="p-2 hover:bg-gray-100 rounded-lg transition-colors"
                title="Back to call list"
              >
                <ArrowLeft className="w-5 h-5 text-gray-600" />
              </button>
            )}
            <div>
              <h1 className="text-2xl font-semibold text-gray-900">
                {formatPhoneNumber(call.phoneNumber)}
              </h1>
              {call.customerName && (
                <p className="text-sm text-gray-600 mt-1">{call.customerName}</p>
              )}
            </div>
          </div>
          <div className="text-right">
            <div className="text-3xl font-mono text-gray-900">
              {formatDuration(duration * 1000)}
            </div>
            <div className="flex items-center justify-end space-x-2 mt-1">
              <StatusBadge status={call.status} />
              <SentimentBadge sentiment={call.sentiment} />
            </div>
          </div>
        </div>

        {/* Call Metadata */}
        <div className="flex items-center space-x-4 text-sm text-gray-600">
          <div>Queue: <span className="font-medium">{call.queueId}</span></div>
          {call.aiSummary && (
            <div className="flex items-center space-x-1">
              <Bot className="w-4 h-4" />
              <span>AI Handled</span>
            </div>
          )}
          {call.transferCount && call.transferCount > 0 && (
            <div>{call.transferCount} Transfer{call.transferCount > 1 ? 's' : ''}</div>
          )}
        </div>
      </div>

      {/* Tabs */}
      <div className="border-b border-gray-200">
        <nav className="flex space-x-8 px-6" aria-label="Tabs">
          <TabButton
            label="Transcription"
            icon={MessageSquare}
            active={activeTab === 'transcription'}
            onClick={() => setActiveTab('transcription')}
          />
          <TabButton
            label="Customer Info"
            icon={User}
            active={activeTab === 'customer'}
            onClick={() => setActiveTab('customer')}
          />
          <TabButton
            label="Call History"
            icon={Settings}
            active={activeTab === 'history'}
            onClick={() => setActiveTab('history')}
          />
        </nav>
      </div>

      {/* Tab Content */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'transcription' && <TranscriptionTab call={call} />}
        {activeTab === 'customer' && <CustomerInfoTab call={call} />}
        {activeTab === 'history' && <CallHistoryTab phoneNumber={call.phoneNumber} />}
      </div>

      {/* Call Controls */}
      {isCallActive ? (
        <div className="px-6 py-4 border-t border-gray-200 bg-gray-50">
          <div className="flex items-center justify-between">
            <div className="flex items-center space-x-3">
              <ControlButton
                icon={isOnHold ? Play : Pause}
                label={isOnHold ? 'Resume' : 'Hold'}
                onClick={handleHold}
                variant={isOnHold ? 'warning' : 'default'}
              />
              <ControlButton
                icon={isMuted ? Phone : Phone}
                label={isMuted ? 'Unmute' : 'Mute'}
                onClick={handleMute}
                variant={isMuted ? 'warning' : 'default'}
              />
              <ControlButton
                icon={Users}
                label="Transfer"
                onClick={handleTransfer}
              />
            </div>

            <button
              onClick={onEndCall}
              className="px-6 py-3 bg-red-600 text-white rounded-lg hover:bg-red-700 transition-colors flex items-center space-x-2 font-medium"
            >
              <PhoneOff className="w-5 h-5" />
              <span>End Call</span>
            </button>
          </div>
        </div>
      ) : (
        <div className="px-6 py-4 border-t border-gray-200 bg-gray-50">
          <div className="flex items-center justify-center text-gray-500">
            <p className="text-sm font-medium">Call Ended</p>
          </div>
        </div>
      )}
    </div>
  );
};

const TabButton: React.FC<{
  label: string;
  icon: React.ComponentType<{ className?: string }>;
  active: boolean;
  onClick: () => void;
}> = ({ label, icon: Icon, active, onClick }) => (
  <button
    onClick={onClick}
    className={cn(
      "flex items-center space-x-2 py-4 px-1 border-b-2 font-medium text-sm transition-colors",
      active
        ? "border-blue-500 text-blue-600"
        : "border-transparent text-gray-500 hover:text-gray-700 hover:border-gray-300"
    )}
  >
    <Icon className="w-4 h-4" />
    <span>{label}</span>
  </button>
);

const ControlButton: React.FC<{
  icon: React.ComponentType<{ className?: string }>;
  label: string;
  onClick: () => void;
  variant?: 'default' | 'warning';
}> = ({ icon: Icon, label, onClick, variant = 'default' }) => (
  <button
    onClick={onClick}
    className={cn(
      "flex items-center space-x-2 px-4 py-2 rounded-lg font-medium text-sm transition-colors",
      variant === 'warning'
        ? "bg-yellow-100 text-yellow-700 hover:bg-yellow-200"
        : "bg-white border border-gray-300 text-gray-700 hover:bg-gray-50"
    )}
  >
    <Icon className="w-4 h-4" />
    <span>{label}</span>
  </button>
);

const StatusBadge: React.FC<{ status: string }> = ({ status }) => {
  const colors = {
    waiting: 'bg-yellow-100 text-yellow-800',
    active: 'bg-green-100 text-green-800',
    ai_active: 'bg-purple-100 text-purple-800',
    completed: 'bg-gray-100 text-gray-800'
  };

  return (
    <span className={cn("px-2 py-1 rounded-full text-xs font-medium", colors[status as keyof typeof colors] || colors.waiting)}>
      {status.replace('_', ' ')}
    </span>
  );
};

const SentimentBadge: React.FC<{ sentiment?: number }> = ({ sentiment }) => {
  if (sentiment === undefined) return null;

  const getColor = () => {
    if (sentiment > 0.5) return 'bg-green-100 text-green-800';
    if (sentiment < -0.5) return 'bg-red-100 text-red-800';
    return 'bg-gray-100 text-gray-800';
  };

  const getLabel = () => {
    if (sentiment > 0.5) return 'Positive';
    if (sentiment < -0.5) return 'Negative';
    return 'Neutral';
  };

  return (
    <span className={cn("px-2 py-1 rounded-full text-xs font-medium", getColor())}>
      {getLabel()}
    </span>
  );
};

const TranscriptionTab: React.FC<{ call: Call }> = ({ call }) => {
  const transcriptionEndRef = React.useRef<HTMLDivElement>(null);
  const [systemMessage, setSystemMessage] = useState('');
  const [isSending, setIsSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [success, setSuccess] = useState(false);

  const quickTemplates = [
    { label: 'Offer Discount', message: 'The customer qualifies for a 20% discount. Offer this to help close the sale.' },
    { label: 'Transfer to Human', message: 'This customer needs specialized help. Transfer them to a human agent now.' },
    { label: 'Apologize', message: 'Acknowledge the customer\'s frustration with empathy and apologize for any inconvenience.' },
    { label: 'Gather Details', message: 'Ask more specific questions to better understand the customer\'s needs.' },
    { label: 'Close Sale', message: 'The customer is ready. Move confidently toward completing the sale.' }
  ];

  // Auto-scroll to bottom when new transcriptions arrive
  useEffect(() => {
    if (call.transcription && call.transcription.length > 0) {
      transcriptionEndRef.current?.scrollIntoView({ behavior: 'smooth' });
    }
  }, [call.transcription?.length]);

  const sendSystemMessage = async () => {
    if (!systemMessage.trim()) return;

    setIsSending(true);
    setError(null);
    setSuccess(false);

    try {
      // Use the SignalWire call_sid (not database ID) for the API call
      const callSid = call.signalwire_call_sid || call.call_sid;

      console.log('🎯 [AI MESSAGE] Preparing to send system message');
      console.log('🎯 [AI MESSAGE] Call object:', call);
      console.log('🎯 [AI MESSAGE] Using call_sid:', callSid);
      console.log('🎯 [AI MESSAGE] Message:', systemMessage);

      const payload = {
        call_id: callSid,
        message: systemMessage,
        role: 'system'
      };

      console.log('🎯 [AI MESSAGE] Full payload:', payload);
      console.log('🎯 [AI MESSAGE] Sending to endpoint: /api/ai/inject-message');

      const response = await api.post('/api/ai/inject-message', payload);

      console.log('🎯 [AI MESSAGE] Response:', response);

      setSuccess(true);
      setSystemMessage('');

      // Clear success message after 3 seconds
      setTimeout(() => setSuccess(false), 3000);
    } catch (err: any) {
      console.error('🎯 [AI MESSAGE] Failed to send AI message:', err);
      console.error('🎯 [AI MESSAGE] Error response:', err.response);
      console.error('🎯 [AI MESSAGE] Error data:', err.response?.data);
      console.error('🎯 [AI MESSAGE] Error status:', err.response?.status);
      setError(err.response?.data?.error || 'Failed to send message to AI agent');
    } finally {
      setIsSending(false);
    }
  };

  const handleKeyPress = (e: React.KeyboardEvent) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) {
      sendSystemMessage();
    }
  };

  // Only show AI controls if call is AI-handled (status contains 'ai' or has ai_handled flag)
  const isAICall = call.status?.toLowerCase().includes('ai') || call.aiHandled;

  return (
    <div className="space-y-4 flex flex-col h-full">
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-lg font-semibold">Live Transcription</h3>
        <div className="flex items-center space-x-2 text-sm text-green-600">
          <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
          <span>Recording</span>
        </div>
      </div>

      <div className="bg-gray-900 rounded-lg p-4 min-h-[400px] max-h-[600px] overflow-y-auto font-mono text-sm scroll-smooth">
        {call.transcription && call.transcription.length > 0 ? (
          <div className="space-y-3">
            {call.transcription.map((entry, idx) => (
              <div key={entry.id || idx} className="flex flex-col space-y-1">
                <div className="flex items-center space-x-2">
                  <span className={cn(
                    "font-semibold",
                    entry.speaker === 'agent' ? 'text-blue-400' : 'text-green-400'
                  )}>
                    {entry.speaker === 'agent' ? 'Agent:' : 'Caller:'}
                  </span>
                  <span className="text-xs text-gray-500">
                    {new Date(entry.timestamp).toLocaleTimeString()}
                  </span>
                </div>
                <p className="text-gray-300 pl-4">{entry.text}</p>
              </div>
            ))}
            <div ref={transcriptionEndRef} />
          </div>
        ) : (
          <div className="flex items-center justify-center h-64 text-gray-500">
            Waiting for conversation...
          </div>
        )}
      </div>

      {call.aiSummary && (
        <div className="bg-blue-50 border border-blue-200 rounded-lg p-4">
          <div className="flex items-start space-x-2">
            <Bot className="w-5 h-5 text-blue-600 mt-0.5 flex-shrink-0" />
            <div>
              <h4 className="font-medium text-blue-900 mb-1">AI Summary</h4>
              <p className="text-sm text-blue-800">{call.aiSummary}</p>
            </div>
          </div>
        </div>
      )}

      {/* AI System Message Controls - Only show for AI calls */}
      {isAICall && (
        <div className="border-t border-gray-200 pt-4 mt-auto">
          <div className="bg-purple-50 border border-purple-200 rounded-lg p-3 mb-3">
            <div className="flex items-start space-x-2">
              <Bot className="w-4 h-4 text-purple-600 mt-0.5 flex-shrink-0" />
              <p className="text-xs text-purple-800">
                Send instructions to guide the AI agent's behavior during this call
              </p>
            </div>
          </div>

          {/* Quick Action Buttons */}
          <div className="flex flex-wrap gap-2 mb-3">
            {quickTemplates.map((template, idx) => (
              <button
                key={idx}
                onClick={() => setSystemMessage(template.message)}
                className="text-xs px-3 py-1.5 bg-purple-100 text-purple-700 rounded-md hover:bg-purple-200 transition-colors"
                disabled={isSending}
              >
                {template.label}
              </button>
            ))}
          </div>

          {/* Text Input and Send Button */}
          <div className="flex gap-2">
            <input
              type="text"
              value={systemMessage}
              onChange={(e) => setSystemMessage(e.target.value)}
              onKeyPress={handleKeyPress}
              placeholder="Type message to AI agent... (Ctrl+Enter to send)"
              className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-purple-500"
              disabled={isSending}
            />
            <button
              onClick={sendSystemMessage}
              disabled={!systemMessage.trim() || isSending}
              className={cn(
                "px-4 py-2 rounded-lg font-medium transition-colors flex items-center",
                systemMessage.trim() && !isSending
                  ? "bg-purple-600 text-white hover:bg-purple-700"
                  : "bg-gray-300 text-gray-500 cursor-not-allowed"
              )}
            >
              {isSending ? (
                <>
                  <div className="animate-spin rounded-full h-4 w-4 border-b-2 border-white mr-2" />
                  Sending
                </>
              ) : (
                <>
                  <Send className="w-4 h-4 mr-1" />
                  Send
                </>
              )}
            </button>
          </div>

          {/* Error Message */}
          {error && (
            <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded-lg text-xs text-red-700 flex items-center">
              <AlertCircle className="w-3 h-3 mr-1 flex-shrink-0" />
              {error}
            </div>
          )}

          {/* Success Message */}
          {success && (
            <div className="mt-2 p-2 bg-green-50 border border-green-200 rounded-lg text-xs text-green-700 flex items-center">
              <Bot className="w-3 h-3 mr-1 flex-shrink-0" />
              Message sent to AI agent successfully!
            </div>
          )}
        </div>
      )}
    </div>
  );
};

const CustomerInfoTab: React.FC<{ call: Call }> = ({ call }) => (
  <div className="space-y-6">
    <div>
      <h3 className="text-lg font-semibold mb-4">Customer Information</h3>
      <div className="grid grid-cols-2 gap-4">
        <InfoField label="Phone Number" value={formatPhoneNumber(call.phoneNumber)} />
        <InfoField label="Name" value={call.customerName || 'Unknown'} />
        <InfoField label="Queue" value={call.queueId} />
        <InfoField label="Priority" value={call.priority || 'Normal'} />
      </div>
    </div>

    <div>
      <h4 className="font-medium mb-2">Notes</h4>
      <textarea
        className="w-full h-32 p-3 border border-gray-300 rounded-lg resize-none focus:outline-none focus:ring-2 focus:ring-blue-500"
        placeholder="Add notes about this call..."
      />
      <button className="mt-2 px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors">
        Save Notes
      </button>
    </div>
  </div>
);

const CallHistoryTab: React.FC<{ phoneNumber: string }> = ({ phoneNumber }) => (
  <div className="space-y-4">
    <h3 className="text-lg font-semibold">Previous Calls from {formatPhoneNumber(phoneNumber)}</h3>
    <div className="text-sm text-gray-500">
      No previous calls found.
    </div>
  </div>
);

const InfoField: React.FC<{ label: string; value: string }> = ({ label, value }) => (
  <div>
    <label className="block text-sm font-medium text-gray-600 mb-1">{label}</label>
    <div className="text-sm text-gray-900 font-medium">{value}</div>
  </div>
);
