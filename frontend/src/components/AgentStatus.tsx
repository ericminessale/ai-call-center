import { useEffect, useState } from 'react';
import { useSocketContext } from '../contexts/SocketContext';
import { useCallFabricContext } from '../contexts/CallFabricContext';
import { Phone, PhoneOff, Coffee, Circle, Loader2 } from 'lucide-react';

type AgentStatusType = 'available' | 'busy' | 'after-call' | 'break' | 'offline';

interface IncomingCall {
  call_id: string;
  call_db_id: number;
  caller_number: string;
  queue_id: string;
  context: Record<string, any>;
  agent_id: number;
  agent_name: string;
}

const statusConfig: Record<AgentStatusType, { label: string; color: string; bgColor: string; icon: typeof Circle }> = {
  available: { label: 'Available', color: 'text-green-600', bgColor: 'bg-green-100', icon: Circle },
  busy: { label: 'Busy', color: 'text-red-600', bgColor: 'bg-red-100', icon: Phone },
  'after-call': { label: 'After Call', color: 'text-orange-600', bgColor: 'bg-orange-100', icon: Phone },
  break: { label: 'On Break', color: 'text-yellow-600', bgColor: 'bg-yellow-100', icon: Coffee },
  offline: { label: 'Offline', color: 'text-gray-600', bgColor: 'bg-gray-100', icon: PhoneOff },
};

export default function AgentStatus() {
  const { connectionStatus } = useSocketContext();
  const {
    agentStatus,
    isChangingStatus,
    isInitializing,
    isOnline,
    setAgentStatus,
    client
  } = useCallFabricContext();

  const [isDropdownOpen, setIsDropdownOpen] = useState(false);
  const [incomingCall, setIncomingCall] = useState<IncomingCall | null>(null);

  // Listen for incoming calls via socket (context handles status events)
  const { socket } = useSocketContext();

  useEffect(() => {
    if (!socket) return;

    const handleIncomingCall = (data: IncomingCall) => {
      console.log('📞 [AgentStatus] Incoming call notification:', data);
      setIncomingCall(data);
      // Auto-dismiss after 30 seconds (matches the SWML connect timeout)
      setTimeout(() => {
        setIncomingCall(prev => prev?.call_id === data.call_id ? null : prev);
      }, 30000);
    };

    socket.on('incoming_call', handleIncomingCall);

    return () => {
      socket.off('incoming_call', handleIncomingCall);
    };
  }, [socket]);

  const changeStatus = async (newStatus: AgentStatusType) => {
    console.log('🔄 [AgentStatus] changeStatus called:', newStatus);
    console.log('  - client:', !!client, 'isOnline:', isOnline);

    if (!client) {
      console.log('❌ [AgentStatus] Call Fabric client not initialized');
      return;
    }

    setIsDropdownOpen(false);

    // Use the unified context function that handles both Call Fabric and Redis
    await setAgentStatus(newStatus);
  };

  const dismissIncomingCall = () => {
    setIncomingCall(null);
  };

  const config = statusConfig[agentStatus];
  const StatusIcon = config.icon;

  // Show initializing state
  if (isInitializing) {
    return (
      <div className="flex items-center space-x-2 px-3 py-1.5 rounded-full bg-gray-100 text-gray-600 text-sm">
        <Loader2 className="h-3 w-3 animate-spin" />
        <span>Initializing...</span>
      </div>
    );
  }

  return (
    <div className="relative flex items-center space-x-3">
      {/* Incoming Call Alert */}
      {incomingCall && (
        <div className="absolute right-0 top-12 z-50 w-72 bg-white rounded-lg shadow-lg border border-green-200 p-4 animate-pulse">
          <div className="flex items-center justify-between mb-2">
            <span className="text-sm font-semibold text-green-700">Incoming Call</span>
            <button
              onClick={dismissIncomingCall}
              className="text-gray-400 hover:text-gray-600"
            >
              &times;
            </button>
          </div>
          <div className="space-y-1 text-sm">
            <p className="font-medium text-gray-900">{incomingCall.caller_number}</p>
            <p className="text-gray-600">Queue: {incomingCall.queue_id}</p>
            {incomingCall.context?.customer_name && (
              <p className="text-gray-600">Name: {incomingCall.context.customer_name}</p>
            )}
          </div>
          <p className="mt-2 text-xs text-gray-500">
            Call will ring on your phone widget
          </p>
        </div>
      )}

      {/* Status Dropdown */}
      <div className="relative">
        <button
          onClick={() => setIsDropdownOpen(!isDropdownOpen)}
          disabled={isChangingStatus}
          className={`flex items-center space-x-2 px-3 py-1.5 rounded-full ${config.bgColor} ${config.color} text-sm font-medium transition-colors hover:opacity-80 disabled:opacity-50`}
        >
          {isChangingStatus ? (
            <Loader2 className="h-3 w-3 animate-spin" />
          ) : (
            <StatusIcon className="h-3 w-3" fill={agentStatus === 'available' ? 'currentColor' : 'none'} />
          )}
          <span>{config.label}</span>
          {isOnline && agentStatus === 'available' && (
            <span className="w-2 h-2 bg-green-500 rounded-full animate-pulse" title="Call Fabric Online" />
          )}
        </button>

        {isDropdownOpen && (
          <div className="absolute right-0 mt-2 w-44 bg-white rounded-lg shadow-lg border border-gray-200 py-1 z-50">
            {(Object.keys(statusConfig) as AgentStatusType[]).map((statusKey) => {
              const itemConfig = statusConfig[statusKey];
              const ItemIcon = itemConfig.icon;
              return (
                <button
                  key={statusKey}
                  onClick={() => changeStatus(statusKey)}
                  disabled={isChangingStatus}
                  className={`w-full flex items-center space-x-2 px-4 py-2 text-sm hover:bg-gray-50 disabled:opacity-50 ${
                    agentStatus === statusKey ? 'bg-gray-50' : ''
                  }`}
                >
                  <ItemIcon className={`h-3 w-3 ${itemConfig.color}`} fill={statusKey === 'available' ? 'currentColor' : 'none'} />
                  <span className={itemConfig.color}>{itemConfig.label}</span>
                  {statusKey === 'available' && (
                    <span className="text-xs text-gray-400 ml-auto">+ Phone</span>
                  )}
                </button>
              );
            })}
          </div>
        )}
      </div>

      {/* Connection indicator */}
      {connectionStatus !== 'connected' && (
        <span className="text-xs text-yellow-600">
          {connectionStatus === 'reconnecting' ? 'Reconnecting...' : 'Disconnected'}
        </span>
      )}
    </div>
  );
}
