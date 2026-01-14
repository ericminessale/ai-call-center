import { useState, useEffect } from 'react';
import CallControl from '../components/CallControl';
import ActiveCall from '../components/ActiveCall';
import RecentCalls from '../components/RecentCalls';
import Layout from '../components/Layout';
import { callsApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
import { Call } from '../types';

export default function Dashboard() {
  const { socket } = useSocketContext();
  const [activeCall, setActiveCall] = useState<Call | null>(null);
  const [refreshTrigger, setRefreshTrigger] = useState(0);
  const [isLoadingActive, setIsLoadingActive] = useState(false);

  // Load initial active call
  useEffect(() => {
    loadActiveCall();
  }, []);

  // Socket event listeners
  useEffect(() => {
    if (!socket) return;

    // Listen for new calls
    const handleCallInitiated = (data: any) => {
      // Don't reload, the call is already set by handleCallStart
      console.log('Call initiated:', data);
    };

    // Listen for call status changes
    const handleCallStatus = (data: any) => {
      if (data.status === 'ended') {
        setActiveCall(null);
        setRefreshTrigger(prev => prev + 1);
      } else if (['created', 'ringing', 'answered'].includes(data.status)) {
        // If we have full call data from webhook, use it
        if (data.destination && data.user_id) {
          const updatedCall: Call = {
            id: data.call_sid,
            signalwire_call_sid: data.call_sid,
            destination: data.destination,
            destination_type: data.destination_type,
            status: data.status,
            transcription_active: data.transcription_active,
            user_id: data.user_id,
            created_at: data.created_at || new Date().toISOString(),
            answered_at: data.answered_at,
            ended_at: data.ended_at
          };
          setActiveCall(updatedCall);
        } else {
          // Update existing call status only
          setActiveCall(prev => {
            if (prev && prev.signalwire_call_sid === data.call_sid) {
              return { ...prev, status: data.status };
            }
            return prev;
          });
        }
      }
    };

    socket.on('call_initiated', handleCallInitiated);
    socket.on('call_status', handleCallStatus);

    return () => {
      socket.off('call_initiated', handleCallInitiated);
      socket.off('call_status', handleCallStatus);
    };
  }, [socket]);

  const loadActiveCall = async () => {
    // Prevent duplicate requests
    if (isLoadingActive) return;

    setIsLoadingActive(true);
    try {
      const response = await callsApi.list(1, 10);
      const activeCalls = response.data.calls.filter(c =>
        ['created', 'ringing', 'answered'].includes(c.status.toLowerCase())
      );
      if (activeCalls.length > 0) {
        console.log('Found active call:', activeCalls[0]);
        setActiveCall(activeCalls[0]);
      } else {
        console.log('No active calls found');
        setActiveCall(null);
      }
    } catch (error) {
      console.error('Failed to load active call:', error);
      setActiveCall(null); // Clear on error
    } finally {
      setIsLoadingActive(false);
    }
  };

  const handleCallStart = (call: Call) => {
    console.log('Setting active call:', call);
    setActiveCall(call);
    // Don't reload from API, we already have the call
  };

  const handleCallEnd = () => {
    setActiveCall(null);
    setRefreshTrigger(prev => prev + 1);
  };

  return (
    <Layout>
      <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
        {/* Left Column - Call Control */}
        <div className="lg:col-span-1">
          {!activeCall ? (
            <CallControl onCallStart={handleCallStart} onCallEnd={handleCallEnd} />
          ) : (
            <div className="bg-white rounded-lg shadow-lg p-6">
              <h2 className="text-xl font-bold mb-4 text-gray-400">Call In Progress</h2>
              <p className="text-gray-500">Control the call using the Active Call panel</p>
            </div>
          )}
        </div>

        {/* Right Column - Active Call and Recent Calls */}
        <div className="lg:col-span-2 space-y-6">
          {/* Active Call Section */}
          <ActiveCall activeCall={activeCall} onCallEnd={handleCallEnd} />

          {/* Recent Calls Section */}
          <RecentCalls key={refreshTrigger} />
        </div>
      </div>
    </Layout>
  );
}