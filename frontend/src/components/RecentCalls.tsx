import { useState, useEffect } from 'react';
import { useNavigate } from 'react-router-dom';
import { callsApi, socketService } from '../services/api';
import { Call } from '../types';
import { Phone, Clock, Calendar, Search, ChevronRight, Loader2, FileText, Mic } from 'lucide-react';
import { format, formatDistanceToNow } from 'date-fns';

export default function RecentCalls() {
  const navigate = useNavigate();
  const [calls, setCalls] = useState<Call[]>([]);
  const [isLoading, setIsLoading] = useState(true);
  const [searchTerm, setSearchTerm] = useState('');
  const [debouncedSearchTerm, setDebouncedSearchTerm] = useState('');
  const [page, setPage] = useState(1);
  const [totalPages, setTotalPages] = useState(1);

  // Debounce search term
  useEffect(() => {
    const timer = setTimeout(() => {
      setDebouncedSearchTerm(searchTerm);
    }, 300);

    return () => clearTimeout(timer);
  }, [searchTerm]);

  useEffect(() => {
    setPage(1); // Reset to first page when search changes
  }, [debouncedSearchTerm]);

  useEffect(() => {
    loadCalls();
  }, [page, debouncedSearchTerm]);

  // Listen for summary events to refresh the call list
  useEffect(() => {
    const socket = socketService.getSocket();

    const handleSummary = (data: any) => {
      // Update the call with the new summary
      setCalls(prevCalls =>
        prevCalls.map(call =>
          call.signalwire_call_sid === data.call_sid
            ? { ...call, summary: data.summary }
            : call
        )
      );
    };

    socket.on('summary', handleSummary);

    return () => {
      socket.off('summary', handleSummary);
    };
  }, []);

  const loadCalls = async () => {
    setIsLoading(true);
    try {
      const response = await callsApi.list(page, 20, debouncedSearchTerm);
      // Filter out active calls for recent calls section
      const recentOnly = response.data.calls.filter(c =>
        !['created', 'ringing', 'answered'].includes(c.status.toLowerCase())
      );
      setCalls(recentOnly);
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
    return 'bg-gray-100 text-gray-800';
  };

  if (isLoading && calls.length === 0) {
    return (
      <div className="bg-white rounded-lg shadow-lg p-6">
        <h2 className="text-xl font-bold mb-4">Recent Calls</h2>
        <div className="flex items-center justify-center py-8">
          <Loader2 className="animate-spin h-8 w-8 text-gray-400" />
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold">Recent Calls</h2>
        <div className="relative">
          <Search className="absolute left-3 top-1/2 transform -translate-y-1/2 h-4 w-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search calls..."
            value={searchTerm}
            onChange={(e) => setSearchTerm(e.target.value)}
            className="pl-9 pr-3 py-2 border rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500 w-64"
          />
        </div>
      </div>

      {calls.length === 0 ? (
        <div className="text-center py-8 text-gray-500">
          {debouncedSearchTerm ? (
            <>
              <Search className="h-12 w-12 mx-auto mb-3 text-gray-400" />
              <p>No calls found matching "{debouncedSearchTerm}"</p>
            </>
          ) : (
            <>
              <Phone className="h-12 w-12 mx-auto mb-3 text-gray-400" />
              <p>No recent calls</p>
              <p className="text-sm mt-1">Completed calls will appear here</p>
            </>
          )}
        </div>
      ) : (
        <div className="space-y-3 max-h-96 overflow-y-auto">
          {calls.map((call) => (
            <div
              key={call.id}
              onClick={() => navigate(`/call/${call.signalwire_call_sid}`)}
              className="border rounded-lg p-3 hover:shadow-md transition-shadow cursor-pointer"
            >
              <div className="flex items-center justify-between">
                <div className="flex-1">
                  <div className="flex items-center space-x-3">
                    <Phone className="h-4 w-4 text-gray-400" />
                    <div className="flex-1">
                      <p className="font-medium text-gray-900 text-sm">{call.destination}</p>
                      <div className="flex items-center space-x-3 text-xs text-gray-500 mt-1">
                        <span className="flex items-center">
                          <Calendar className="h-3 w-3 mr-1" />
                          {format(new Date(call.created_at), 'MMM d, h:mm a')}
                        </span>
                        <span className="flex items-center">
                          <Clock className="h-3 w-3 mr-1" />
                          {formatDuration(call.duration)}
                        </span>
                      </div>
                      {call.summary && (
                        <div className="mt-2 text-xs text-gray-600 italic truncate">
                          <FileText className="inline h-3 w-3 mr-1" />
                          {call.summary.substring(0, 100)}...
                        </div>
                      )}
                    </div>
                  </div>
                </div>

                <div className="flex items-center space-x-2">
                  {call.transcription_active && (
                    <Mic className="h-3 w-3 text-purple-500" />
                  )}
                  {call.recording_url && (
                    <span className="text-xs px-2 py-1 bg-green-100 text-green-800 rounded-full">
                      Recorded
                    </span>
                  )}
                  <ChevronRight className="h-4 w-4 text-gray-400" />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}

      {totalPages > 1 && (
        <div className="flex justify-center space-x-2 mt-4">
          <button
            onClick={() => setPage(p => Math.max(1, p - 1))}
            disabled={page === 1}
            className="px-3 py-1 text-xs border rounded-md disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Previous
          </button>
          <span className="px-3 py-1 text-xs">
            Page {page} of {totalPages}
          </span>
          <button
            onClick={() => setPage(p => Math.min(totalPages, p + 1))}
            disabled={page === totalPages}
            className="px-3 py-1 text-xs border rounded-md disabled:opacity-50 disabled:cursor-not-allowed hover:bg-gray-50"
          >
            Next
          </button>
        </div>
      )}
    </div>
  );
}