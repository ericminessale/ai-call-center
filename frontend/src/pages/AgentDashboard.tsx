import React, { useState, useEffect } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import { useSocket } from '../hooks/useSocket';
import { useAuthStore } from '../stores/authStore';
import { AgentTopNav } from '../components/agent/AgentTopNav';
import { CallList } from '../components/agent/CallList';
import { CallDetailView } from '../components/agent/CallDetailView';
import { PhoneWidget } from '../components/agent/PhoneWidget';
import type { Call, AgentStatus } from '../types/callcenter';

export const AgentDashboard: React.FC = () => {
  const navigate = useNavigate();
  const { callId } = useParams<{ callId?: string }>();
  const socket = useSocket();
  const { user } = useAuthStore();

  // Agent state
  const [agentStatus, setAgentStatus] = useState<AgentStatus>('offline');
  const [statusStartTime, setStatusStartTime] = useState<Date>(new Date());

  // Call state
  const [calls, setCalls] = useState<Call[]>([]);
  const [selectedCall, setSelectedCall] = useState<Call | null>(null);
  const [filters, setFilters] = useState({
    waiting: true,
    aiActive: true,
    myCalls: true,
    completed: false
  });

  // Use a ref to always have the latest filters value in WebSocket handlers
  const filtersRef = React.useRef(filters);
  React.useEffect(() => {
    filtersRef.current = filters;
  }, [filters]);

  // Phone widget state
  const [isPhoneExpanded, setIsPhoneExpanded] = useState(false);

  // Stats
  const [stats, setStats] = useState({
    callsToday: 0,
    avgHandleTime: 0,
    fcr: 0,
    csat: 0
  });

  // Call counts (total, not filtered)
  const [callCounts, setCallCounts] = useState({
    waiting: 0,
    aiActive: 0,
    completed: 0
  });

  // Load calls on mount and when filters change
  useEffect(() => {
    loadCalls();
  }, [filters]);

  // Load call counts separately (always, regardless of filters)
  useEffect(() => {
    loadCallCounts();
  }, []); // Only on mount

  // Reload calls when socket connects AND authenticates to catch any missed events
  useEffect(() => {
    if (!socket) return;

    console.log('🔄 Setting up socket authenticated listener');

    const handleAuthenticated = () => {
      console.log('🔄 Socket authenticated, reloading calls');
      loadCalls();
    };

    socket.on('authenticated', handleAuthenticated);

    return () => {
      socket.off('authenticated', handleAuthenticated);
    };
  }, [socket]);

  // Load call counts
  const loadCallCounts = async () => {
    try {
      const token = localStorage.getItem('access_token');

      console.log('📊 [COUNTS] Loading call counts for user:', user?.id);

      // Fetch counts for each status
      const [waitingRes, aiActiveRes, completedRes] = await Promise.all([
        fetch(`/api/calls?status=waiting&agent_id=${user?.id || ''}&per_page=1`, {
          headers: { 'Authorization': `Bearer ${token}` }
        }),
        fetch(`/api/calls?status=ai_active&agent_id=${user?.id || ''}&per_page=1`, {
          headers: { 'Authorization': `Bearer ${token}` }
        }),
        fetch(`/api/calls?status=completed&agent_id=${user?.id || ''}&per_page=1`, {
          headers: { 'Authorization': `Bearer ${token}` }
        })
      ]);

      const [waitingData, aiActiveData, completedData] = await Promise.all([
        waitingRes.json(),
        aiActiveRes.json(),
        completedRes.json()
      ]);

      console.log('📊 [COUNTS] Waiting response:', waitingData);
      console.log('📊 [COUNTS] AI Active response:', aiActiveData);
      console.log('📊 [COUNTS] Completed response:', completedData);

      const newCounts = {
        waiting: waitingData.total || 0,
        aiActive: aiActiveData.total || 0,
        completed: completedData.total || 0
      };

      console.log('📊 [COUNTS] Setting new counts:', newCounts);
      setCallCounts(newCounts);
    } catch (error) {
      console.error('📋 [API] Failed to load call counts:', error);
    }
  };

  // WebSocket subscriptions
  useEffect(() => {
    console.log('🔌 [EFFECT] Setting up WebSocket listeners, socket exists:', !!socket, 'socket.id:', socket?.id);
    if (!socket) {
      console.log('❌ [EFFECT] No socket available, skipping listener setup');
      return;
    }

    // Only proceed if socket is connected (has an ID)
    if (!socket.id) {
      console.log('⏳ [EFFECT] Socket exists but not connected yet (no socket.id), skipping listener setup');
      return;
    }

    console.log('✅ [EFFECT] Socket connected with ID:', socket.id, '- registering listeners');

    // Remove any existing listeners first to prevent duplicates
    socket.off('call_update');
    socket.off('call_assigned');
    socket.off('call_ended');
    socket.off('agent_stats');
    socket.off('transcription');
    socket.off('summary');
    socket.off('reconnect');

    // Call updates
    const handleCallUpdate = (data: any) => {
      console.log('📞 [WS EVENT] ========== HANDLER INVOKED ==========');
      console.log('📞 [WS EVENT] Received call_update event:', JSON.stringify(data, null, 2));

      // Use filtersRef.current to get the latest filters value
      const currentFilters = filtersRef.current;
      console.log('📞 [WS EVENT] Current active filters:', currentFilters);

      // Check if this call matches our active filters
      const callMatchesFilters =
        (currentFilters.waiting && data.call.status === 'waiting') ||
        (currentFilters.aiActive && data.call.status === 'ai_active') ||
        (currentFilters.completed && (data.call.status === 'completed' || data.call.status === 'ended'));

      console.log('📞 [WS EVENT] Call matches active filters:', callMatchesFilters);

      if (callMatchesFilters) {
        setCalls(prev => {
          const index = prev.findIndex(c => c.id === data.call.id);
          if (index >= 0) {
            console.log('✏️ [WS EVENT] Updating existing call:', data.call.id, 'from status:', prev[index].status, 'to:', data.call.status);
            const updated = [...prev];
            updated[index] = data.call;
            console.log('✏️ [WS EVENT] Updated calls array:', updated.map(c => ({ id: c.id, status: c.status })));
            return updated;
          }
          console.log('➕ [WS EVENT] Adding new call:', data.call.id, 'Status:', data.call.status);
          const newCalls = [...prev, data.call];
          console.log('➕ [WS EVENT] New calls array:', newCalls.map(c => ({ id: c.id, status: c.status })));
          return newCalls;
        });
      } else {
        console.log('⏭️ [WS EVENT] Call does not match filters, removing from list if present');
        setCalls(prev => prev.filter(c => c.id !== data.call.id));
      }

      // Refresh counts when call updates
      console.log('📊 [WS EVENT] Refreshing call counts...');
      loadCallCounts();
    };

    console.log('🎯 [EFFECT] Registering call_update handler on socket.id:', socket.id);
    socket.on('call_update', handleCallUpdate);
    console.log('🎯 [EFFECT] call_update handler registered on socket.id:', socket.id);

    // New call assigned
    socket.on('call_assigned', (data: any) => {
      setCalls(prev => [...prev, data.call]);
      // Auto-select if agent available
      if (agentStatus === 'available') {
        setSelectedCall(data.call);
        navigate(`/dashboard/${data.call.id}`);
      }
    });

    // Call ended
    console.log('🎯 [EFFECT] Registering call_ended handler');
    socket.on('call_ended', (data: any) => {
      console.log('🏁 [WS EVENT] Received call_ended event:', data);
      console.log('📞 Call ended event:', data);
      // Update the call in the list to show completed status
      setCalls(prev => prev.map(c =>
        c.id === data.callId
          ? { ...c, status: 'completed' as const }
          : c
      ));
      // Update selected call using functional update (no selectedCall dependency needed)
      setSelectedCall(prev => {
        if (prev && prev.id === data.callId) {
          return { ...prev, status: 'completed' as const };
        }
        return prev;
      });
      // Refresh counts when call ends
      loadCallCounts();
    });

    // Stats update
    socket.on('agent_stats', (data: any) => {
      setStats(data);
    });

    // Transcription updates
    console.log('🎯 [EFFECT] Registering transcription handler');
    socket.on('transcription', (data: any) => {
      console.log('📝 [WS EVENT] Received transcription event:', data);

      const transcriptionId = `${data.call_sid}-${data.sequence}`;
      const newTranscription = {
        id: transcriptionId,
        speaker: data.role === 'remote-caller' ? 'caller' : 'agent',
        text: data.text,
        timestamp: new Date().toISOString(),
        confidence: data.confidence
      };

      // Only update selectedCall if this transcription is for the currently selected call
      // Don't update calls array to avoid triggering other effects
      setSelectedCall(prev => {
        if (prev && (prev.id === data.call_sid || prev.call_sid === data.call_sid || prev.signalwire_call_sid === data.call_sid)) {
          // Check if this transcription already exists (prevent duplicates)
          const existing = prev.transcription?.find(t => t.id === transcriptionId);
          if (existing) {
            console.log(`📝 [TRANSCRIPTION] Skipping duplicate in selectedCall ${transcriptionId}`);
            return prev;
          }
          console.log(`📝 [TRANSCRIPTION] Updating selectedCall ${prev.id} with new transcription`, newTranscription);
          const updated = {
            ...prev,
            transcription: [...(prev.transcription || []), newTranscription]
          };
          console.log(`📝 [TRANSCRIPTION] Updated selectedCall transcription count: ${updated.transcription.length}`);
          console.log(`📝 [TRANSCRIPTION] Latest transcription:`, newTranscription);
          return updated;
        }
        console.log(`📝 [TRANSCRIPTION] Ignoring transcription for non-selected call ${data.call_sid}`);
        return prev;
      });
    });

    // Summary updates
    socket.on('summary', (data: any) => {
      console.log('📊 [WS EVENT] Received summary event:', data);

      // Update the call in the calls array
      setCalls(prev => prev.map(call => {
        if (call.id === data.call_sid || call.signalwire_call_sid === data.call_sid) {
          return {
            ...call,
            aiSummary: data.summary
          };
        }
        return call;
      }));

      // Update selected call using functional update (no selectedCall dependency needed)
      setSelectedCall(prev => {
        if (prev && (prev.id === data.call_sid || prev.signalwire_call_sid === data.call_sid)) {
          return {
            ...prev,
            aiSummary: data.summary
          };
        }
        return prev;
      });
    });

    // Reconnection handler - reload calls to catch any missed events
    socket.on('reconnect', (attemptNumber: number) => {
      console.log('🔄 [WS EVENT] Socket reconnected after', attemptNumber, 'attempts - reloading calls and counts');
      loadCalls();
      loadCallCounts();
    });

    return () => {
      console.log('🧹 [CLEANUP] Removing WebSocket listeners');
      socket.off('call_update', handleCallUpdate);
      socket.off('call_assigned');
      socket.off('call_ended');
      socket.off('agent_stats');
      socket.off('transcription');
      socket.off('summary');
      socket.off('reconnect');
    };
  }, [socket, socket?.id, agentStatus]);  // Re-run when socket or socket.id changes

  // Load call from URL parameter
  useEffect(() => {
    if (callId) {
      // Only update selectedCall if:
      // 1. We don't have a selected call yet, OR
      // 2. The selected call ID doesn't match the URL callId
      // This prevents overwriting selectedCall when calls array updates from WebSocket events
      if (!selectedCall || selectedCall.id !== callId) {
        const call = calls.find(c => c.id === callId);
        if (call) {
          console.log('🔄 [URL SYNC] Setting selectedCall from URL:', callId);
          setSelectedCall(call);
        } else {
          // Load call from API if not in memory
          console.log('🔄 [URL SYNC] Call not in memory, loading from API:', callId);
          loadCallById(callId);
        }
      }
    } else if (selectedCall) {
      // Only clear selectedCall if we actually have one
      console.log('🔄 [URL SYNC] No callId in URL, clearing selectedCall');
      setSelectedCall(null);
    }
  }, [callId]);

  const loadCalls = async () => {
    try {
      console.log('📋 [API] loadCalls() called');
      console.log('📋 [API] Current filters:', JSON.stringify(filters, null, 2));
      console.log('📋 [API] Current user:', user?.id, user?.email);

      const token = localStorage.getItem('access_token');
      console.log('📋 [API] Token exists:', !!token);

      const params = new URLSearchParams();

      if (filters.waiting) params.append('status', 'waiting');
      if (filters.aiActive) params.append('status', 'ai_active');
      if (filters.myCalls) params.append('agent_id', user?.id || '');
      if (filters.completed) params.append('status', 'completed');

      const url = `/api/calls?${params.toString()}`;
      console.log('📋 [API] Fetching from URL:', url);

      const response = await fetch(url, {
        headers: { 'Authorization': `Bearer ${token}` }
      });

      console.log('📋 [API] Response status:', response.status);

      if (response.ok) {
        const data = await response.json();
        console.log('📋 [API] Response data:', JSON.stringify(data, null, 2));
        console.log('📋 [API] Loaded', data.calls?.length || 0, 'calls');

        if (data.calls && data.calls.length > 0) {
          console.log('📋 [API] First call sample:', JSON.stringify(data.calls[0], null, 2));
        }

        // Map dashboard_status to status and from_number to phoneNumber for frontend use
        const mappedCalls = (data.calls || []).map((call: any) => ({
          ...call,
          phoneNumber: call.from_number || call.destination || 'Unknown',  // Use caller's number, fallback to destination
          status: call.dashboard_status || call.status  // Use dashboard_status if available
        }));

        console.log('📋 [API] Mapped calls:', mappedCalls.map((c: any) => ({ id: c.id, phoneNumber: c.phoneNumber, status: c.status })));

        setCalls(mappedCalls);

        // Update counts after loading calls
        loadCallCounts();
      } else {
        const errorText = await response.text();
        console.error('📋 [API] Failed to load calls, status:', response.status, 'error:', errorText);
      }
    } catch (error) {
      console.error('📋 [API] Exception in loadCalls:', error);
    }
  };

  const loadCallById = async (id: string) => {
    try {
      const token = localStorage.getItem('access_token');
      const response = await fetch(`/api/calls/${id}`, {
        headers: { 'Authorization': `Bearer ${token}` }
      });

      if (response.ok) {
        const data = await response.json();
        console.log('📞 [API] loadCallById response:', data);

        // Map backend transcriptions to frontend format
        const transcriptions = (data.transcriptions || []).map((t: any) => ({
          id: `${data.call.signalwire_call_sid}-${t.sequence_number}`,
          speaker: t.speaker || 'unknown',
          text: t.transcript,
          timestamp: t.created_at,
          confidence: t.confidence
        }));

        console.log('📝 [API] Mapped transcriptions:', transcriptions);

        // Map dashboard_status to status and include all necessary fields
        // Use from_number as phoneNumber for inbound calls, destination for outbound
        const phoneNumber = data.call.from_number || data.call.destination || 'Unknown';

        const mappedCall = {
          ...data.call,
          phoneNumber: phoneNumber,
          call_sid: data.call.signalwire_call_sid,
          status: data.call.dashboard_status || data.call.status,
          transcription: transcriptions,
          aiSummary: data.call.summary
        };

        console.log('📞 [API] Mapped phone number:', phoneNumber, 'from_number:', data.call.from_number, 'destination:', data.call.destination);

        console.log('📞 [API] Setting selectedCall with transcriptions:', mappedCall);
        setSelectedCall(mappedCall);
      }
    } catch (error) {
      console.error('Failed to load call:', error);
    }
  };

  const handleStatusChange = (newStatus: AgentStatus) => {
    setAgentStatus(newStatus);
    setStatusStartTime(new Date());

    const token = localStorage.getItem('access_token');
    socket?.emit('agent_status', { status: newStatus, token });
  };

  const handleCallSelect = (call: Call) => {
    setSelectedCall(call);
    navigate(`/dashboard/${call.id}`);
  };

  const handleTakeCall = async (call: Call) => {
    try {
      const token = localStorage.getItem('access_token');
      const response = await fetch(`/api/calls/${call.id}/take`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });

      if (response.ok) {
        setAgentStatus('busy');
        setSelectedCall(call);
        navigate(`/dashboard/${call.id}`);
      }
    } catch (error) {
      console.error('Failed to take call:', error);
    }
  };

  const handleEndCall = async (call: Call) => {
    try {
      const token = localStorage.getItem('access_token');
      await fetch(`/api/calls/${call.id}/end`, {
        method: 'POST',
        headers: {
          'Authorization': `Bearer ${token}`,
          'Content-Type': 'application/json'
        }
      });

      // Don't change agent status for AI calls - agent wasn't on the call
      // Only change status if this was a human-answered call
      if (call.status !== 'ai_active') {
        setAgentStatus('after-call');
      }

      // Don't navigate away - let the call_ended WebSocket event update the UI
      // The call will transition to 'completed' status and stay visible
    } catch (error) {
      console.error('Failed to end call:', error);
      // On error, show error message but don't navigate away
    }
  };

  return (
    <div className="h-screen flex flex-col bg-gray-50">
      {/* Top Navigation */}
      <AgentTopNav
        agentName={user?.email || 'Agent'}
        agentStatus={agentStatus}
        onStatusChange={handleStatusChange}
        stats={stats}
        onPhoneToggle={() => setIsPhoneExpanded(!isPhoneExpanded)}
        isPhoneExpanded={isPhoneExpanded}
      />

      {/* Main Content: Full-screen toggle between Call List and Detail View */}
      <div className="flex-1 overflow-hidden">
        {selectedCall ? (
          // Call Detail View - Full Screen
          <CallDetailView
            call={selectedCall}
            onEndCall={() => handleEndCall(selectedCall)}
            onBack={() => {
              setSelectedCall(null);
              navigate('/dashboard');
            }}
          />
        ) : (
          // Call List - Full Screen with Filters
          <div className="h-full flex flex-col bg-white">
            {/* Filter Bar */}
            <div className="px-6 py-4 border-b border-gray-200 bg-gray-50">
              <div className="flex items-center space-x-4">
                <h2 className="text-lg font-semibold text-gray-900">Calls</h2>
                <div className="flex-1 flex items-center space-x-3">
                  <FilterButton
                    label="Waiting"
                    count={callCounts.waiting}
                    active={filters.waiting}
                    onClick={() => setFilters({ ...filters, waiting: !filters.waiting })}
                  />
                  <FilterButton
                    label="AI Active"
                    count={callCounts.aiActive}
                    active={filters.aiActive}
                    onClick={() => setFilters({ ...filters, aiActive: !filters.aiActive })}
                  />
                  <FilterButton
                    label="My Calls"
                    active={filters.myCalls}
                    onClick={() => setFilters({ ...filters, myCalls: !filters.myCalls })}
                  />
                  <FilterButton
                    label="Completed"
                    count={callCounts.completed}
                    active={filters.completed}
                    onClick={() => setFilters({ ...filters, completed: !filters.completed })}
                  />
                </div>
              </div>
            </div>

            {/* Call List - Now takes full center area */}
            <div className="flex-1 overflow-y-auto">
              <CallList
                calls={calls}
                selectedCall={selectedCall}
                filters={filters}
                onFilterChange={setFilters}
                onCallSelect={handleCallSelect}
                onTakeCall={handleTakeCall}
              />
            </div>
          </div>
        )}
      </div>

      {/* Phone Widget (Collapsible) */}
      <PhoneWidget
        isExpanded={isPhoneExpanded}
        onToggle={() => setIsPhoneExpanded(!isPhoneExpanded)}
        onCallStart={(call) => {
          setSelectedCall(call);
          navigate(`/dashboard/${call.id}`);
          setIsPhoneExpanded(false);
        }}
      />
    </div>
  );
};

// Filter Button Component
const FilterButton: React.FC<{
  label: string;
  count?: number;
  active: boolean;
  onClick: () => void;
}> = ({ label, count, active, onClick }) => (
  <button
    onClick={onClick}
    className={`px-4 py-2 rounded-lg font-medium text-sm transition-colors flex items-center space-x-2 ${
      active
        ? 'bg-blue-100 text-blue-700 border border-blue-300'
        : 'bg-white text-gray-600 border border-gray-300 hover:bg-gray-50'
    }`}
  >
    <span>{label}</span>
    {count !== undefined && (
      <span className={`px-2 py-0.5 rounded-full text-xs font-bold ${
        active ? 'bg-blue-200 text-blue-800' : 'bg-gray-200 text-gray-700'
      }`}>
        {count}
      </span>
    )}
  </button>
);
