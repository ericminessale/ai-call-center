import React, { useState } from 'react';
import { Phone, PhoneOff, Mic, MicOff, X } from 'lucide-react';
import { cn, formatPhoneNumber, formatDuration } from '../../lib/utils';
import { useCallFabric } from '../../hooks/useCallFabric';

interface PhoneWidgetProps {
  isExpanded: boolean;
  onToggle: () => void;
  onCallStart: (call: any) => void;
}

export const PhoneWidget: React.FC<PhoneWidgetProps> = ({
  isExpanded,
  onToggle,
  onCallStart
}) => {
  const {
    activeCall,
    isOnline,
    isInitializing,
    error,
    goOnline,
    goOffline,
    makeCall,
    hangup,
    answerCall,
    mute,
    unmute,
    isMuted
  } = useCallFabric();

  const [phoneNumber, setPhoneNumber] = useState('');
  const [callDuration, setCallDuration] = useState(0);

  // Update duration for active calls
  React.useEffect(() => {
    if (!activeCall) {
      setCallDuration(0);
      return;
    }

    const interval = setInterval(() => {
      setCallDuration(prev => prev + 1);
    }, 1000);

    return () => clearInterval(interval);
  }, [activeCall]);

  const handleMakeCall = async () => {
    if (!phoneNumber) return;

    try {
      await makeCall(phoneNumber);
      // Create a mock call object for the dashboard
      onCallStart({
        id: `call-${Date.now()}`,
        phoneNumber,
        startTime: new Date().toISOString(),
        status: 'active',
        queueId: 'outbound',
        priority: 'medium'
      });
    } catch (error) {
      console.error('Failed to make call:', error);
    }
  };

  const handleHangup = async () => {
    await hangup();
    setPhoneNumber('');
    setCallDuration(0);
  };

  if (!isExpanded) return null;

  return (
    <div className="fixed top-16 right-6 w-80 bg-white rounded-lg shadow-2xl border border-gray-200 z-50">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-200 bg-gray-50">
        <h3 className="font-semibold text-gray-900">Browser Phone</h3>
        <button
          onClick={onToggle}
          className="p-1 hover:bg-gray-200 rounded transition-colors"
        >
          <X className="w-4 h-4 text-gray-500" />
        </button>
      </div>

      {/* Content */}
      <div className="p-4">
        {/* Status */}
        <div className="flex items-center justify-between mb-4 pb-3 border-b border-gray-100">
          <div className="flex items-center space-x-2">
            <div className={cn(
              "w-2 h-2 rounded-full",
              isOnline ? "bg-green-500 animate-pulse" : "bg-gray-300"
            )} />
            <span className="text-sm font-medium text-gray-700">
              {isOnline ? 'Online' : 'Offline'}
            </span>
          </div>
          <button
            onClick={isOnline ? goOffline : goOnline}
            disabled={isInitializing}
            className={cn(
              "px-3 py-1 text-xs font-medium rounded transition-colors",
              isOnline
                ? "bg-gray-200 text-gray-700 hover:bg-gray-300"
                : "bg-green-600 text-white hover:bg-green-700"
            )}
          >
            {isInitializing ? 'Connecting...' : isOnline ? 'Go Offline' : 'Go Online'}
          </button>
        </div>

        {/* Error Message */}
        {error && (
          <div className="mb-4 p-3 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
            {error}
          </div>
        )}

        {/* Active Call */}
        {activeCall ? (
          <div className="space-y-4">
            <div className="text-center py-4">
              <div className="text-lg font-semibold text-gray-900 mb-1">
                {formatPhoneNumber(activeCall.callerId || 'Unknown')}
              </div>
              <div className="text-3xl font-mono text-gray-900 mb-2">
                {formatDuration(callDuration * 1000)}
              </div>
              <div className="flex items-center justify-center space-x-1 text-sm text-green-600">
                <div className="w-2 h-2 bg-green-500 rounded-full animate-pulse" />
                <span>Connected</span>
              </div>
            </div>

            <div className="flex items-center justify-center space-x-3">
              <button
                onClick={() => isMuted ? unmute() : mute()}
                className={cn(
                  "p-3 rounded-full transition-colors",
                  isMuted
                    ? "bg-yellow-100 text-yellow-700 hover:bg-yellow-200"
                    : "bg-gray-100 text-gray-700 hover:bg-gray-200"
                )}
              >
                {isMuted ? <MicOff className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
              </button>

              <button
                onClick={handleHangup}
                className="p-4 bg-red-600 text-white rounded-full hover:bg-red-700 transition-colors"
              >
                <PhoneOff className="w-6 h-6" />
              </button>
            </div>
          </div>
        ) : (
          /* Dialer */
          <div className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Make a Call
              </label>
              <input
                type="tel"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
                placeholder="+1 (555) 123-4567"
                disabled={!isOnline}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 disabled:bg-gray-100"
                onKeyPress={(e) => {
                  if (e.key === 'Enter') {
                    handleMakeCall();
                  }
                }}
              />
            </div>

            <button
              onClick={handleMakeCall}
              disabled={!isOnline || !phoneNumber}
              className="w-full flex items-center justify-center space-x-2 px-4 py-3 bg-blue-600 text-white rounded-lg hover:bg-blue-700 transition-colors disabled:bg-gray-300 disabled:cursor-not-allowed"
            >
              <Phone className="w-5 h-5" />
              <span className="font-medium">Call</span>
            </button>

            {/* Quick Dial */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Quick Dial
              </label>
              <div className="grid grid-cols-2 gap-2">
                <QuickDialButton
                  label="Sales Queue"
                  number="+15551234567"
                  onClick={setPhoneNumber}
                  disabled={!isOnline}
                />
                <QuickDialButton
                  label="Support Queue"
                  number="+15557654321"
                  onClick={setPhoneNumber}
                  disabled={!isOnline}
                />
              </div>
            </div>

            {/* Recent Numbers */}
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-2">
                Recent
              </label>
              <div className="text-sm text-gray-500 text-center py-4">
                No recent calls
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
};

const QuickDialButton: React.FC<{
  label: string;
  number: string;
  onClick: (number: string) => void;
  disabled?: boolean;
}> = ({ label, number, onClick, disabled }) => (
  <button
    onClick={() => onClick(number)}
    disabled={disabled}
    className="px-3 py-2 bg-gray-100 hover:bg-gray-200 rounded-lg text-sm font-medium text-gray-700 transition-colors disabled:bg-gray-50 disabled:text-gray-400 disabled:cursor-not-allowed"
  >
    {label}
  </button>
);
