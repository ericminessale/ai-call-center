import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { callsApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
import { Call } from '../types';
import { Phone, Clock, Calendar, ChevronRight, Loader2, Radio } from 'lucide-react';
import { format, formatDistanceToNow } from 'date-fns';

export default function CallsList() {
  const navigate = useNavigate();
  const { socket } = useSocketContext();
  const [calls, setCalls] = useState<Call[]>([]);
  const [activeCalls, setActiveCalls] = useState<Call[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  useEffect(() => {
    loadCalls();

    // Refresh calls periodically for active calls
    const interval = setInterval(() => {
      if (calls.some(c => ['created', 'ringing', 'answered'].includes(c.status.toLowerCase()))) {
        loadCalls();
      }
    }, 5000);

    return () => {
      clearInterval(interval);
    };
  }, [page]);

  // Listen for call status updates via WebSocket
  useEffect(() => {
    if (!socket) return;

    socket.on('call_status', handleCallStatus);

    return () => {
      socket.off('call_status', handleCallStatus);
    };
  }, [socket]);

  const handleCallStatus = (data: { call_sid: string; status: string }) => {
    setCalls(prev => prev.map(call =>
      call.signalwire_call_sid === data.call_sid
        ? { ...call, status: data.status }
        : call
    ));
  };

  const loadCalls = async () => {
    setIsLoading(true);
    try {
      const response = await callsApi.list(page, 10);
      const allCalls = response.data.calls;
      setCalls(allCalls);

      // Filter active calls
      const active = allCalls.filter(c =>
        ['created', 'ringing', 'answered'].includes(c.status.toLowerCase())
      );
      setActiveCalls(active);

      setTotalPages(response.data.pages);
    } catch (error) {
      console.error('Failed to load calls:', error);
    } finally {
      setIsLoading(false);
    }
  };

  const formatDuration = (seconds?: number) => {
    if (!seconds) return '-';
    const minutes = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${minutes}:${secs.toString().padStart(2, '0')}`;
  };

  const getStatusColor = (status: string) => {
    switch (status.toLowerCase()) {
      case 'ended':
        return 'bg-green-100 text-green-800';
      case 'created':
        return 'bg-gray-100 text-gray-800';
      case 'ringing':
        return 'bg-yellow-100 text-yellow-800';
      case 'answered':
        return 'bg-blue-100 text-blue-800';
      default:
        return 'bg-gray-100 text-gray-800';
    }
  };

  if (isLoading && calls.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow-lg p-6">
        <div className="flex items-center justify-center py-8">
          <Loader2 className="animate-spin h-8 w-8 text-gray-400" />
        </div>
      </div>
    );
  }

  // Display active calls or all calls
  const displayCalls = activeCalls.length > 0 ? activeCalls : calls;

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      <h2 className="text-2xl font-bold mb-6">
        {activeCalls.length > 0 ? (
          <span className="flex items-center">
            <Radio className="h-6 w-6 mr-2 text-red-500 animate-pulse" />
            Active Calls
          </span>
        ) : (
          'Recent Calls'
        )}
      </h2>

      {displayCalls.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          <Phone className="h-12 w-12 mx-auto mb-3 text-gray-400" />
          <p>No calls yet</p>
          <p className="text-sm">Start making calls to see them here</p>
        </div>
      ) : (
        <div className="space-y-4">
          {displayCalls.map((call) => {
            const isActive = ['created', 'ringing', 'answered'].includes(call.status.toLowerCase());

            return (
              <div
                key={call.id}
                onClick={() => navigate(`/call/${call.signalwire_call_sid}`)}
                className="border rounded-lg p-4 hover:shadow-md transition-shadow cursor-pointer"
              >
                <div className="flex items-center justify-between">
                  <div className="flex-1">
                    <div className="flex items-center space-x-4">
                      {isActive ? (
                        <Radio className="h-5 w-5 text-red-500 animate-pulse" />
                      ) : (
                        <Phone className="h-5 w-5 text-gray-400" />
                      )}
                      <div>
                        <p className="font-medium text-gray-900">{call.destination}</p>
                        <div className="flex items-center space-x-4 text-sm text-gray-500">
                          <span className="flex items-center">
                            <Calendar className="h-3 w-3 mr-1" />
                            {format(new Date(call.created_at), 'MMM d, yyyy h:mm a')}
                          </span>
                          <span className="flex items-center">
                            <Clock className="h-3 w-3 mr-1" />
                            {formatDuration(call.duration)}
                          </span>
                          {call.created_at && (
                            <span className="text-xs">
                              {formatDistanceToNow(new Date(call.created_at), { addSuffix: true })}
                            </span>
                          )}
                        </div>
                      </div>
                    </div>
                  </div>

                  <div className="flex items-center space-x-3">
                    <span className={`px-2 py-1 text-xs font-medium rounded-full ${getStatusColor(call.status)}`}>
                      {call.status}
                    </span>
                    {call.transcription_active && isActive && (
                      <span className="px-2 py-1 text-xs font-medium rounded-full bg-red-100 text-red-800 animate-pulse">
                        Live
                      </span>
                    )}
                    {call.transcription_active && !isActive && (
                      <span className="px-2 py-1 text-xs font-medium rounded-full bg-purple-100 text-purple-800">
                        Transcribed
                      </span>
                    )}
                    <ChevronRight className="h-5 w-5 text-gray-400" />
                  </div>
                </div>
              </div>
            );
          })}

          {totalPages > 1 && (
            <div className="flex justify-center space-x-2 mt-6">
              <button
                onClick={() => setPage(p => Math.max(1, p - 1))}
                disabled={page === 1}
                className="px-3 py-1 text-sm border rounded-md disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
              >
                Previous
              </button>
              <span className="px-3 py-1 text-sm">
                Page {page} of {totalPages}
              </span>
              <button
                onClick={() => setPage(p => Math.min(totalPages, p + 1))}
                disabled={page === totalPages}
                className="px-3 py-1 text-sm border rounded-md disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
              >
                Next
              </button>
            </div>
          )}
        </div>
      )}
    </div>
  );
}