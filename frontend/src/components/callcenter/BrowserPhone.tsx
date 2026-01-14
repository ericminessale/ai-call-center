import React, { useState, useEffect } from 'react';
import { Phone, PhoneOff, Mic, MicOff, Pause, Play, Hash, PhoneIncoming, PhoneOutgoing } from 'lucide-react';
import { useCallFabric } from '../../hooks/useCallFabric';
import { cn, formatPhoneNumber, formatDuration } from '../../lib/utils';

interface BrowserPhoneProps {
  onCallStart?: (call: any) => void;
  onCallEnd?: () => void;
  className?: string;
}

export const BrowserPhone: React.FC<BrowserPhoneProps> = ({
  onCallStart,
  onCallEnd,
  className
}) => {
  const {
    activeCall,
    isOnline,
    isInitializing,
    error,
    callState,
    goOnline,
    goOffline,
    makeCall,
    hangup,
    answerCall,
    mute,
    unmute,
    hold,
    unhold,
    sendDigits
  } = useCallFabric();

  const [phoneNumber, setPhoneNumber] = useState('');
  const [isMuted, setIsMuted] = useState(false);
  const [isOnHold, setIsOnHold] = useState(false);
  const [callDuration, setCallDuration] = useState(0);
  const [showDialpad, setShowDialpad] = useState(false);

  // Update call duration
  useEffect(() => {
    if (callState === 'active' && activeCall) {
      const interval = setInterval(() => {
        const duration = Math.floor((Date.now() - activeCall.startTime.getTime()) / 1000);
        setCallDuration(duration);
      }, 1000);

      return () => clearInterval(interval);
    } else {
      setCallDuration(0);
    }
  }, [callState, activeCall]);

  // Handle call events
  useEffect(() => {
    if (callState === 'active' && onCallStart) {
      onCallStart(activeCall);
    } else if (callState === 'idle' && onCallEnd) {
      onCallEnd();
    }
  }, [callState]);

  const handleDial = async () => {
    if (!phoneNumber) return;
    await makeCall(phoneNumber);
    setPhoneNumber('');
  };

  const handleMute = async () => {
    if (isMuted) {
      await unmute();
    } else {
      await mute();
    }
    setIsMuted(!isMuted);
  };

  const handleHold = async () => {
    if (isOnHold) {
      await unhold();
    } else {
      await hold();
    }
    setIsOnHold(!isOnHold);
  };

  const dialpadButtons = [
    '1', '2', '3',
    '4', '5', '6',
    '7', '8', '9',
    '*', '0', '#'
  ];

  const handleDialpadClick = async (digit: string) => {
    if (callState === 'active') {
      // Send DTMF during call
      await sendDigits(digit);
    } else {
      // Add to phone number
      setPhoneNumber(prev => prev + digit);
    }
  };

  return (
    <div className={cn("bg-white rounded-lg shadow-sm border", className)}>
      {/* Status Bar */}
      <div className="p-4 border-b">
        <div className="flex items-center justify-between">
          <div className="flex items-center space-x-3">
            <div className={cn(
              "w-3 h-3 rounded-full",
              isOnline ? "bg-green-500 animate-pulse" : "bg-gray-300"
            )} />
            <span className="text-sm font-medium">
              {isInitializing ? 'Initializing...' : isOnline ? 'Phone Online' : 'Phone Offline'}
            </span>
          </div>
          <button
            onClick={isOnline ? goOffline : goOnline}
            disabled={isInitializing}
            className={cn(
              "px-3 py-1 rounded text-sm font-medium transition-colors",
              isOnline
                ? "bg-red-100 text-red-700 hover:bg-red-200"
                : "bg-green-100 text-green-700 hover:bg-green-200",
              isInitializing && "opacity-50 cursor-not-allowed"
            )}
          >
            {isOnline ? 'Go Offline' : 'Go Online'}
          </button>
        </div>
        {error && (
          <div className="mt-2 text-sm text-red-600">{error}</div>
        )}
      </div>

      {/* Active Call Display */}
      {activeCall && (
        <div className="p-4 border-b bg-gray-50">
          <div className="flex items-center justify-between mb-2">
            <div className="flex items-center space-x-2">
              {activeCall.direction === 'inbound' ? (
                <PhoneIncoming className="w-4 h-4 text-blue-600" />
              ) : (
                <PhoneOutgoing className="w-4 h-4 text-green-600" />
              )}
              <span className="text-sm text-gray-600">
                {activeCall.direction === 'inbound' ? 'Incoming Call' : 'Outgoing Call'}
              </span>
            </div>
            <span className="text-sm font-mono">
              {formatDuration(callDuration)}
            </span>
          </div>
          <div className="font-medium text-lg">
            {formatPhoneNumber(activeCall.callerId)}
          </div>
          {activeCall.aiContext && (
            <div className="mt-2 p-2 bg-blue-50 rounded text-sm">
              <span className="font-medium">AI Context: </span>
              {activeCall.aiContext.summary || 'Available'}
            </div>
          )}
        </div>
      )}

      {/* Call Controls */}
      {callState !== 'idle' && (
        <div className="p-4 border-b">
          <div className="flex items-center justify-around">
            {callState === 'ringing' && activeCall?.direction === 'inbound' && (
              <button
                onClick={answerCall}
                className="p-3 bg-green-500 text-white rounded-full hover:bg-green-600 transition-colors"
                title="Answer"
              >
                <Phone className="w-5 h-5" />
              </button>
            )}

            <button
              onClick={handleMute}
              className={cn(
                "p-3 rounded-full transition-colors",
                isMuted
                  ? "bg-red-500 text-white hover:bg-red-600"
                  : "bg-gray-200 hover:bg-gray-300"
              )}
              title={isMuted ? "Unmute" : "Mute"}
            >
              {isMuted ? <MicOff className="w-5 h-5" /> : <Mic className="w-5 h-5" />}
            </button>

            <button
              onClick={handleHold}
              className={cn(
                "p-3 rounded-full transition-colors",
                isOnHold
                  ? "bg-yellow-500 text-white hover:bg-yellow-600"
                  : "bg-gray-200 hover:bg-gray-300"
              )}
              title={isOnHold ? "Resume" : "Hold"}
            >
              {isOnHold ? <Play className="w-5 h-5" /> : <Pause className="w-5 h-5" />}
            </button>

            <button
              onClick={() => setShowDialpad(!showDialpad)}
              className="p-3 bg-gray-200 rounded-full hover:bg-gray-300 transition-colors"
              title="Dialpad"
            >
              <Hash className="w-5 h-5" />
            </button>

            <button
              onClick={hangup}
              className="p-3 bg-red-500 text-white rounded-full hover:bg-red-600 transition-colors"
              title="Hang Up"
            >
              <PhoneOff className="w-5 h-5" />
            </button>
          </div>
        </div>
      )}

      {/* Dialer / Dialpad */}
      <div className="p-4">
        {callState === 'idle' ? (
          // Phone number input for new calls
          <div className="space-y-3">
            <input
              type="tel"
              value={phoneNumber}
              onChange={(e) => setPhoneNumber(e.target.value)}
              onKeyPress={(e) => e.key === 'Enter' && handleDial()}
              placeholder="Enter phone number..."
              className="w-full px-3 py-2 border rounded-lg focus:outline-none focus:ring-2 focus:ring-blue-500"
              disabled={!isOnline}
            />
            <button
              onClick={handleDial}
              disabled={!isOnline || !phoneNumber}
              className={cn(
                "w-full py-2 rounded-lg font-medium transition-colors",
                isOnline && phoneNumber
                  ? "bg-green-500 text-white hover:bg-green-600"
                  : "bg-gray-200 text-gray-400 cursor-not-allowed"
              )}
            >
              <Phone className="w-4 h-4 inline mr-2" />
              Call
            </button>
          </div>
        ) : showDialpad ? (
          // DTMF Dialpad for active calls
          <div className="grid grid-cols-3 gap-2">
            {dialpadButtons.map(digit => (
              <button
                key={digit}
                onClick={() => handleDialpadClick(digit)}
                className="p-3 bg-gray-100 rounded-lg hover:bg-gray-200 transition-colors font-semibold"
              >
                {digit}
              </button>
            ))}
          </div>
        ) : null}
      </div>

      {/* Quick Actions */}
      {isOnline && callState === 'idle' && (
        <div className="p-4 border-t bg-gray-50">
          <div className="text-xs text-gray-600 mb-2">Quick Dial</div>
          <div className="flex flex-wrap gap-2">
            <button
              onClick={() => makeCall('/public/queue-sales')}
              className="px-3 py-1 bg-blue-100 text-blue-700 rounded text-sm hover:bg-blue-200"
            >
              Sales Queue
            </button>
            <button
              onClick={() => makeCall('/public/queue-support')}
              className="px-3 py-1 bg-purple-100 text-purple-700 rounded text-sm hover:bg-purple-200"
            >
              Support Queue
            </button>
            <button
              onClick={() => makeCall('/public/ai-test')}
              className="px-3 py-1 bg-indigo-100 text-indigo-700 rounded text-sm hover:bg-indigo-200"
            >
              Test AI Agent
            </button>
          </div>
        </div>
      )}
    </div>
  );
};