import { useState } from 'react';
import { Phone, X, Circle, Lock, PhoneCall } from 'lucide-react';
import { logger } from '../../lib/logger';
import { useAuthStore } from '../../stores/authStore';
import { useVerifyStore } from '../../stores/verifyStore';

type AgentStatusType = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

interface QuickDialDropdownProps {
  callFabric: {
    isOnline: boolean;
    isInitializing: boolean;
    isChangingStatus: boolean;
    agentStatus: AgentStatusType;
    makeCall: (number: string, context?: any) => Promise<any>;
    setAgentStatus: (status: AgentStatusType) => Promise<void>;
  };
  onClose: () => void;
  onCallStarted?: (phoneNumber: string) => void;
}

export function QuickDialDropdown({ callFabric, onClose, onCallStarted }: QuickDialDropdownProps) {
  const [phoneNumber, setPhoneNumber] = useState('');
  const [isDialing, setIsDialing] = useState(false);

  // Hosted-demo outbound is verify-first: the only reachable number is the
  // visitor's own verified phone (via "Have the AI call me"). Free-form dial
  // isn't part of that model, so in demo we replace the dialer with an honest
  // gated notice instead of buttons that always 403.
  const isDemo = useAuthStore((s) => !!s.runtimeConfig?.demo_mode);
  const isVisitor = useAuthStore((s) => s.user != null && s.user.workspace_id != null);
  const verified = useVerifyStore((s) => s.verified);
  const demoDialLocked = isDemo && isVisitor;

  const quickDialNumbers = [
    { label: 'Sales Queue', number: '+15551234567' },
    { label: 'Support Queue', number: '+15557654321' },
  ];

  const handleDial = async (number: string) => {
    // If not online, set status to available first (which goes online)
    if (!callFabric.isOnline) {
      await callFabric.setAgentStatus('available');
    }

    setIsDialing(true);

    // Navigate to contact page IMMEDIATELY before making the call
    // This way the UI shows the outbound call state right away
    onCallStarted?.(number);
    onClose();

    try {
      await callFabric.makeCall(number);
    } catch (error) {
      logger.error('Failed to dial:', error);
      // Error will be shown in ContactDetailView via callFabric.error
    } finally {
      setIsDialing(false);
    }
  };

  const handleToggleOnline = async () => {
    if (callFabric.isOnline) {
      // Going offline sets status to offline
      await callFabric.setAgentStatus('offline');
    } else {
      // Going online sets status to available
      await callFabric.setAgentStatus('available');
    }
  };

  return (
    <div className="absolute top-full right-0 mt-2 w-72 bg-gray-800 rounded-lg shadow-xl border border-gray-700 z-50">
      {/* Header */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-gray-700">
        <div className="flex items-center gap-2">
          <Phone className="w-4 h-4 text-gray-400" />
          <span className="text-sm font-medium text-white">Quick Dial</span>
        </div>
        <button
          onClick={onClose}
          className="p-1 hover:bg-gray-700 rounded transition-colors"
        >
          <X className="w-4 h-4 text-gray-400" />
        </button>
      </div>

      {/* Status Toggle */}
      <div className="px-4 py-3 border-b border-gray-700">
        <button
          onClick={handleToggleOnline}
          disabled={callFabric.isInitializing || callFabric.isChangingStatus}
          className={`w-full flex items-center justify-center gap-2 px-3 py-2 rounded-lg transition-colors ${
            callFabric.isOnline
              ? 'bg-green-600/20 border border-green-600 text-green-400 hover:bg-green-600/30'
              : 'bg-gray-700 border border-gray-600 text-gray-400 hover:bg-gray-600'
          }`}
        >
          <Circle
            className={`w-2.5 h-2.5 ${
              callFabric.isOnline ? 'fill-green-400 text-green-400' : 'fill-gray-500 text-gray-500'
            }`}
          />
          <span className="text-sm font-medium">
            {callFabric.isInitializing || callFabric.isChangingStatus
              ? 'Connecting...'
              : callFabric.isOnline
              ? `Online (${callFabric.agentStatus})`
              : 'Go Online'}
          </span>
        </button>
      </div>

      {/* Dialer — replaced by a verify-first notice in the hosted demo */}
      {demoDialLocked ? (
        <div className="p-4">
          {!verified ? (
            <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-3 text-center">
              <Lock className="mx-auto mb-2 h-4 w-4 text-amber-400" />
              <p className="text-sm font-medium text-gray-200">Calling is locked</p>
              <p className="mt-1 text-xs leading-relaxed text-gray-400">
                Verify your phone (the card at the top of the dashboard) to
                enable calling — the demo places calls to your verified number.
              </p>
            </div>
          ) : (
            <div className="rounded-lg border border-gray-700 bg-gray-900/40 p-3 text-center">
              <PhoneCall className="mx-auto mb-2 h-4 w-4 text-teal-400" />
              <p className="text-sm font-medium text-gray-200">Outbound goes to your verified number</p>
              <p className="mt-1 text-xs leading-relaxed text-gray-400">
                In the demo the only reachable number is your own. Use
                <span className="text-gray-200"> “Have the AI call me” </span>
                in the top bar to place an outbound call.
              </p>
            </div>
          )}
        </div>
      ) : (
        <>
          <div className="p-4 space-y-3">
            {/* Phone Number Input */}
            <div className="flex gap-2">
              <input
                type="tel"
                value={phoneNumber}
                onChange={(e) => setPhoneNumber(e.target.value)}
                placeholder="Enter phone number"
                className="flex-1 px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm placeholder-gray-400 focus:outline-none focus:border-blue-500"
                disabled={!callFabric.isOnline || isDialing}
              />
              <button
                onClick={() => handleDial(phoneNumber)}
                disabled={!phoneNumber || !callFabric.isOnline || isDialing}
                className="px-3 py-2 bg-green-600 hover:bg-green-700 disabled:bg-gray-600 disabled:cursor-not-allowed text-white rounded-lg transition-colors"
              >
                <Phone className="w-4 h-4" />
              </button>
            </div>

            {/* Quick Dial Buttons */}
            <div className="space-y-2">
              <div className="text-xs text-gray-500 uppercase tracking-wider">Quick Dial</div>
              {quickDialNumbers.map((item) => (
                <button
                  key={item.number}
                  onClick={() => handleDial(item.number)}
                  disabled={!callFabric.isOnline || isDialing}
                  className="w-full flex items-center justify-between px-3 py-2 bg-gray-700 hover:bg-gray-600 disabled:bg-gray-700 disabled:opacity-50 disabled:cursor-not-allowed rounded-lg transition-colors"
                >
                  <span className="text-sm text-white">{item.label}</span>
                  <span className="text-xs text-gray-400">{item.number}</span>
                </button>
              ))}
            </div>
          </div>

          {/* Footer hint */}
          <div className="px-4 py-2 bg-gray-900/50 border-t border-gray-700 rounded-b-lg">
            <p className="text-xs text-gray-500 text-center">
              Tip: Click a contact to call directly from their profile
            </p>
          </div>
        </>
      )}
    </div>
  );
}

export default QuickDialDropdown;
