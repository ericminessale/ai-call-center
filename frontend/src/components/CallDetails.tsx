import { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { callsApi, transcriptionApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
import { Call, Transcription } from '../types';
import { ArrowLeft, Phone, Clock, Calendar, Mic, MicOff, FileText, Loader2, Play, Download, AlertCircle } from 'lucide-react';
import { format } from 'date-fns';

interface TranscriptionEvent {
  call_sid: string;
  text: string;
  confidence: number;
  is_final: boolean;
  sequence: number;
  role: string;
  timestamp?: number;
}

export default function CallDetails() {
  const { callSid } = useParams<{ callSid: string }>();
  const navigate = useNavigate();
  const { socket } = useSocketContext();
  const [call, setCall] = useState<Call | null>(null);
  const [transcriptions, setTranscriptions] = useState<Transcription[]>([]);
  const [liveTranscript, setLiveTranscript] = useState<string>('');
  const [isLoading, setIsLoading] = useState(true);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);

  // Load call details
  useEffect(() => {
    if (!callSid) return;
    loadCallDetails();
  }, [callSid]);

  // Socket event listeners
  useEffect(() => {
    if (!callSid || !socket) return;

    // Join the call-specific room
    socket.emit('join_call', { call_sid: callSid });

    // Listen for transcription events
    socket.on('transcription', handleTranscription);
    socket.on('call_status', handleCallStatus);
    socket.on('summary', handleSummary);

    return () => {
      socket.off('transcription', handleTranscription);
      socket.off('call_status', handleCallStatus);
      socket.off('summary', handleSummary);
      socket.emit('leave_call', { call_sid: callSid });
    };
  }, [callSid, socket]);

  const loadCallDetails = async () => {
    if (!callSid) return;

    setIsLoading(true);
    try {
      const response = await callsApi.get(callSid);
      setCall(response.data.call);
      setTranscriptions(response.data.transcriptions || []);
      // Only set transcribing if call is active AND transcription is active
      const isCallActive = ['created', 'ringing', 'answered'].includes(response.data.call.status.toLowerCase());
      setIsTranscribing(response.data.call.transcription_active && isCallActive);
      setSummary(response.data.call.summary);
    } catch (error) {
      console.error('Failed to load call details:', error);
    } finally {
      setIsLoading(false);
    }
  };

  const handleTranscription = (data: TranscriptionEvent) => {
    if (data.call_sid === callSid) {
      // Add to live transcript
      setLiveTranscript(prev => {
        const prefix = data.role === 'remote-caller' ? 'Caller: ' : 'Agent: ';
        return prev + '\n' + prefix + data.text;
      });

      // Add to transcriptions list if final
      if (data.is_final) {
        setTranscriptions(prev => [...prev, {
          id: `live-${Date.now()}`,
          call_id: callSid!,
          transcript: data.text,
          confidence: data.confidence,
          is_final: true,
          sequence_number: data.sequence,
          language: 'en-US',
          created_at: new Date().toISOString()
        }]);
      }
    }
  };

  const handleCallStatus = (data: { call_sid: string; status: string }) => {
    if (data.call_sid === callSid && call) {
      setCall({ ...call, status: data.status });
      if (data.status === 'ended') {
        setIsTranscribing(false);
      }
    }
  };

  const handleSummary = (data: { call_sid: string; summary: string }) => {
    if (data.call_sid === callSid) {
      setSummary(data.summary);
      if (call) {
        setCall({ ...call, summary: data.summary });
      }
      console.log('Received summary:', data.summary);
    }
  };

  const toggleTranscription = async () => {
    if (!callSid || !call) return;

    try {
      const action = isTranscribing ? 'stop' : 'start';
      await transcriptionApi.control(callSid, action);
      setIsTranscribing(!isTranscribing);
      setCall({ ...call, transcription_active: !isTranscribing });
    } catch (error) {
      console.error('Failed to toggle transcription:', error);
    }
  };

  const requestSummary = async () => {
    if (!callSid) return;

    try {
      await transcriptionApi.control(callSid, 'summarize');
    } catch (error) {
      console.error('Failed to request summary:', error);
    }
  };

  const endCall = async () => {
    if (!callSid || !call) return;

    try {
      await callsApi.end(callSid);
      setCall({ ...call, status: 'ended' });
      setIsTranscribing(false);
    } catch (error) {
      console.error('Failed to end call:', error);
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

  if (isLoading) {
    return (
      <div className="min-h-screen bg-gray-50 flex items-center justify-center">
        <Loader2 className="animate-spin h-8 w-8 text-gray-400" />
      </div>
    );
  }

  if (!call) {
    return (
      <div className="min-h-screen bg-gray-50 p-6">
        <div className="max-w-7xl mx-auto">
          <button
            onClick={() => navigate('/')}
            className="flex items-center text-gray-600 hover:text-gray-900 mb-6"
          >
            <ArrowLeft className="h-5 w-5 mr-2" />
            Back to Dashboard
          </button>
          <div className="bg-white rounded-lg shadow-lg p-6 text-center">
            <p className="text-gray-500">Call not found</p>
          </div>
        </div>
      </div>
    );
  }

  const isActive = ['created', 'ringing', 'answered'].includes(call.status.toLowerCase());

  return (
    <div className="min-h-screen bg-gray-50 p-6">
      <div className="max-w-7xl mx-auto">
        <button
          onClick={() => navigate('/')}
          className="flex items-center text-gray-600 hover:text-gray-900 mb-6"
        >
          <ArrowLeft className="h-5 w-5 mr-2" />
          Back to Dashboard
        </button>

        {/* Call Header */}
        <div className="bg-white rounded-lg shadow-lg p-6 mb-6">
          <div className="flex items-center justify-between">
            <div>
              <div className="flex items-center space-x-4">
                <Phone className="h-8 w-8 text-gray-400" />
                <div>
                  <h1 className="text-2xl font-bold text-gray-900">{call.destination}</h1>
                  <div className="flex items-center space-x-4 text-sm text-gray-500 mt-1">
                    <span className="flex items-center">
                      <Calendar className="h-3 w-3 mr-1" />
                      {format(new Date(call.created_at), 'MMM d, yyyy h:mm a')}
                    </span>
                    <span className="flex items-center">
                      <Clock className="h-3 w-3 mr-1" />
                      {formatDuration(call.duration)}
                    </span>
                  </div>
                </div>
              </div>
            </div>
            <div className="flex items-center space-x-3">
              <span className={`px-3 py-1 text-sm font-medium rounded-full ${getStatusColor(call.status)}`}>
                {call.status}
              </span>
              {isTranscribing && isActive && (
                <span className="px-3 py-1 text-sm font-medium rounded-full bg-red-100 text-red-800 animate-pulse">
                  Live Transcribing
                </span>
              )}
            </div>
          </div>

          {/* Call Controls */}
          {isActive && (
            <div className="mt-6 flex space-x-3">
              <button
                onClick={toggleTranscription}
                className={`flex items-center px-4 py-2 text-sm font-medium text-white rounded-md ${
                  isTranscribing
                    ? 'bg-yellow-600 hover:bg-yellow-700'
                    : 'bg-blue-600 hover:bg-blue-700'
                }`}
              >
                {isTranscribing ? (
                  <>
                    <MicOff className="h-4 w-4 mr-2" />
                    Stop Transcription
                  </>
                ) : (
                  <>
                    <Mic className="h-4 w-4 mr-2" />
                    Start Transcription
                  </>
                )}
              </button>
              <button
                onClick={requestSummary}
                className="flex items-center px-4 py-2 text-sm font-medium text-white bg-purple-600 hover:bg-purple-700 rounded-md"
              >
                <FileText className="h-4 w-4 mr-2" />
                Get Summary
              </button>
              <button
                onClick={endCall}
                className="flex items-center px-4 py-2 text-sm font-medium text-white bg-red-600 hover:bg-red-700 rounded-md"
              >
                <Phone className="h-4 w-4 mr-2" />
                End Call
              </button>
            </div>
          )}
        </div>

        {/* Live Transcript */}
        {isActive && (isTranscribing || liveTranscript) && (
          <div className="bg-white rounded-lg shadow-lg p-6 mb-6">
            <h2 className="text-xl font-bold mb-4 flex items-center">
              <span className="animate-pulse text-red-500 mr-2">●</span>
              Live Transcript
            </h2>
            <div className="bg-gray-50 rounded-lg p-4 max-h-96 overflow-y-auto">
              <pre className="whitespace-pre-wrap font-mono text-sm text-gray-700">
                {liveTranscript || 'Waiting for speech...'}
              </pre>
            </div>
          </div>
        )}

        {/* Recording Player */}
        {call.recording_url && (
          <div className="bg-white rounded-lg shadow-lg p-6 mb-6">
            <h2 className="text-xl font-bold mb-4 flex items-center">
              <Play className="h-5 w-5 mr-2" />
              Call Recording
            </h2>
            <div className="bg-gray-50 rounded-lg p-4">
              <audio controls className="w-full">
                <source src={call.recording_url} type="audio/mpeg" />
                Your browser does not support the audio element.
              </audio>
              <div className="mt-3 flex justify-between items-center">
                <span className="text-sm text-gray-600">
                  Duration: {formatDuration(call.duration)}
                </span>
                <a
                  href={call.recording_url}
                  download
                  className="flex items-center text-blue-600 hover:text-blue-800 text-sm"
                >
                  <Download className="h-4 w-4 mr-1" />
                  Download Recording
                </a>
              </div>
            </div>
          </div>
        )}

        {/* Summary */}
        {(summary || call.summary) && (
          <div className="bg-white rounded-lg shadow-lg p-6 mb-6">
            <h2 className="text-xl font-bold mb-4 flex items-center">
              <FileText className="h-5 w-5 mr-2" />
              Call Summary
            </h2>
            <div className="bg-blue-50 border-l-4 border-blue-400 p-4">
              <div className="flex">
                <AlertCircle className="h-5 w-5 text-blue-400 mr-2 flex-shrink-0 mt-0.5" />
                <div className="text-gray-700 whitespace-pre-wrap">
                  {summary || call.summary}
                </div>
              </div>
            </div>
          </div>
        )}

        {/* Transcriptions */}
        <div className="bg-white rounded-lg shadow-lg p-6">
          <h2 className="text-xl font-bold mb-4">Transcription History</h2>
          {transcriptions.length === 0 ? (
            <p className="text-gray-500 text-center py-8">
              No transcriptions yet. Start transcribing to see the conversation here.
            </p>
          ) : (
            <div className="space-y-3">
              {transcriptions.map((trans, index) => (
                <div key={trans.id || index} className="border-l-4 border-blue-500 pl-4 py-2">
                  <p className="text-gray-900">{trans.transcript}</p>
                  <div className="flex items-center space-x-4 mt-1">
                    <span className="text-xs text-gray-500">
                      Confidence: {(trans.confidence * 100).toFixed(1)}%
                    </span>
                    <span className="text-xs text-gray-500">
                      {format(new Date(trans.created_at), 'h:mm:ss a')}
                    </span>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}