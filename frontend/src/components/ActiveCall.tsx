import { useState, useEffect, useRef } from 'react';
import { Call } from '../types';
import { callsApi, transcriptionApi } from '../services/api';
import { useSocketContext } from '../contexts/SocketContext';
import { Phone, PhoneOff, Mic, MicOff, FileText, Radio, Clock, AlertCircle } from 'lucide-react';
import { format } from 'date-fns';
import toast from 'react-hot-toast';

interface ActiveCallProps {
  activeCall: Call | null;
  onCallEnd?: () => void;
}

interface TranscriptionLine {
  id: string;
  text: string;
  role: string;
  confidence: number;
  timestamp: number;
}

export default function ActiveCall({ activeCall, onCallEnd }: ActiveCallProps) {
  const { socket } = useSocketContext();
  const [transcriptions, setTranscriptions] = useState<TranscriptionLine[]>([]);
  const [isTranscribing, setIsTranscribing] = useState(false);
  const [summary, setSummary] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [isSummarizing, setIsSummarizing] = useState(false);
  const transcriptionBoxRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    console.log('ActiveCall received:', activeCall);
    if (activeCall) {
      // Only show as transcribing if call is answered AND transcription is active
      const isCallAnswered = activeCall.status.toLowerCase() === 'answered';
      setIsTranscribing(activeCall.transcription_active && isCallAnswered);
      setSummary(activeCall.summary || null);

      // Start duration timer only for active calls
      const isCallActive = ['created', 'ringing', 'answered'].includes(activeCall.status.toLowerCase());
      if (isCallActive) {
        const startTime = new Date(activeCall.answered_at || activeCall.created_at).getTime();
        const timer = setInterval(() => {
          const now = Date.now();
          setDuration(Math.floor((now - startTime) / 1000));
        }, 1000);

        return () => clearInterval(timer);
      }
    } else {
      setTranscriptions([]);
      setSummary(null);
      setDuration(0);
      setIsTranscribing(false);
    }
  }, [activeCall]);

  useEffect(() => {
    if (!activeCall || !socket) return;

    // Join call room using the SignalWire call_id as the channel
    const token = localStorage.getItem('access_token');
    if (token) {
      socket.emit('join_call', {
        call_sid: activeCall.signalwire_call_sid,
        token: token
      });
    }

    // Listen for transcription events
    const handleTranscription = (data: any) => {
      if (data.call_sid === activeCall.signalwire_call_sid) {
        const newLine: TranscriptionLine = {
          id: `${Date.now()}-${Math.random()}`,
          text: data.text,
          role: data.role || 'unknown',
          confidence: data.confidence || 0,
          timestamp: data.timestamp || Date.now()
        };
        setTranscriptions(prev => [...prev, newLine]);
      }
    };

    // Listen for call status
    const handleCallStatus = (data: any) => {
      // Use activeCall from props instead of local call state for comparison
      if (activeCall && data.call_sid === activeCall.signalwire_call_sid) {
        console.log('Call status updated:', data.status);

        // Update transcription state based on call status - only when answered
        const isCallAnswered = data.status.toLowerCase() === 'answered';
        setIsTranscribing(activeCall.transcription_active && isCallAnswered);

        if (data.status === 'ended') {
          setIsTranscribing(false);
          if (onCallEnd) onCallEnd();
        }
      }
    };

    // Listen for summary
    const handleSummary = (data: any) => {
      if (data.call_sid === activeCall.signalwire_call_sid) {
        setSummary(data.summary);
        toast.success('Call summary generated!');
      }
    };

    socket.on('transcription', handleTranscription);
    socket.on('call_status', handleCallStatus);
    socket.on('summary', handleSummary);

    return () => {
      socket.off('transcription', handleTranscription);
      socket.off('call_status', handleCallStatus);
      socket.off('summary', handleSummary);
      if (activeCall && activeCall.signalwire_call_sid) {
        socket.emit('leave_call', { call_sid: activeCall.signalwire_call_sid });
      }
    };
  }, [activeCall, onCallEnd, socket]);

  // Auto-scroll transcription box to bottom when new transcriptions arrive
  useEffect(() => {
    if (transcriptionBoxRef.current) {
      transcriptionBoxRef.current.scrollTop = transcriptionBoxRef.current.scrollHeight;
    }
  }, [transcriptions]);

  const toggleTranscription = async () => {
    if (!activeCall || !activeCall.signalwire_call_sid) {
      console.error('No call or call SID:', activeCall);
      toast.error('No active call');
      return;
    }

    try {
      const action = isTranscribing ? 'stop' : 'start';
      await transcriptionApi.control(activeCall.signalwire_call_sid, action);
      setIsTranscribing(!isTranscribing);
      toast.success(`Transcription ${action === 'start' ? 'started' : 'stopped'}`);
    } catch (error) {
      console.error('Toggle transcription error:', error);
      toast.error('Failed to toggle transcription');
    }
  };

  const requestSummary = async () => {
    if (!activeCall || isSummarizing) return;

    try {
      setIsSummarizing(true);
      await transcriptionApi.control(activeCall.signalwire_call_sid, 'summarize');
      toast.info('Generating summary...');
      // Keep button disabled for 5 seconds to prevent rapid clicks
      setTimeout(() => setIsSummarizing(false), 5000);
    } catch (error) {
      toast.error('Failed to request summary');
      setIsSummarizing(false);
    }
  };

  const endCall = async () => {
    if (!activeCall || !activeCall.signalwire_call_sid) {
      console.error('No call or call SID:', activeCall);
      toast.error('No active call');
      return;
    }

    try {
      await callsApi.end(activeCall.signalwire_call_sid);
      toast.success('Call ended');
      if (onCallEnd) onCallEnd();
    } catch (error) {
      console.error('End call error:', error);
      toast.error('Failed to end call');
    }
  };

  const formatDuration = (seconds: number) => {
    const hours = Math.floor(seconds / 3600);
    const minutes = Math.floor((seconds % 3600) / 60);
    const secs = seconds % 60;

    if (hours > 0) {
      return `${hours}:${minutes.toString().padStart(2, '0')}:${secs.toString().padStart(2, '0')}`;
    }
    return `${minutes}:${secs.toString().padStart(2, '0')}`;
  };

  // Button enable states:
  // - End Call: always enabled when there's an active call
  // - Transcription controls: enabled when receiving transcription events (transcriptions.length > 0)
  const hasTranscriptions = transcriptions.length > 0;
  const canControlTranscription = hasTranscriptions;
  const isCallActive = activeCall && activeCall.status && ['created', 'ringing', 'answered'].includes(activeCall.status.toLowerCase());

  if (!activeCall) {
    return (
      <div className="bg-white rounded-lg shadow-lg p-6">
        <h2 className="text-xl font-bold mb-4 text-gray-400">No Active Call</h2>
        <div className="text-center py-8 text-gray-500">
          <Phone className="h-12 w-12 mx-auto mb-3 text-gray-400" />
          <p>Start a call to see live transcription</p>
        </div>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-lg shadow-lg p-6">
      {/* Header */}
      <div className="flex items-center justify-between mb-4">
        <h2 className="text-xl font-bold flex items-center">
          <Radio className="h-5 w-5 mr-2 text-red-500 animate-pulse" />
          Active Call
        </h2>
        <span className="text-lg font-mono text-gray-700">
          <Clock className="inline h-4 w-4 mr-1" />
          {formatDuration(duration)}
        </span>
      </div>

      {/* Call Info */}
      <div className="bg-gray-50 rounded-lg p-4 mb-4">
        <div className="flex items-center justify-between mb-2">
          <span className="text-gray-600">Destination:</span>
          <span className="font-medium">{activeCall.destination}</span>
        </div>
        <div className="flex items-center justify-between">
          <span className="text-gray-600">Status:</span>
          <span className={`px-2 py-1 text-xs font-medium rounded-full ${
            activeCall.status === 'answered' ? 'bg-green-100 text-green-800' :
            activeCall.status === 'ringing' ? 'bg-yellow-100 text-yellow-800' :
            'bg-gray-100 text-gray-800'
          }`}>
            {activeCall.status}
          </span>
        </div>
      </div>

      {/* Controls */}
      <div className="grid grid-cols-2 gap-2 mb-4">
        <button
          onClick={toggleTranscription}
          disabled={!canControlTranscription}
          className={`flex items-center justify-center px-3 py-2 text-sm font-medium text-white rounded-md ${
            !canControlTranscription
              ? 'bg-gray-400 cursor-not-allowed'
              : isTranscribing
              ? 'bg-yellow-600 hover:bg-yellow-700'
              : 'bg-blue-600 hover:bg-blue-700'
          }`}
        >
          {isTranscribing ? (
            <>
              <MicOff className="h-4 w-4 mr-1" />
              Stop
            </>
          ) : (
            <>
              <Mic className="h-4 w-4 mr-1" />
              Start
            </>
          )}
        </button>
        <button
          onClick={requestSummary}
          disabled={!canControlTranscription || isSummarizing}
          className={`flex items-center justify-center px-3 py-2 text-sm font-medium text-white rounded-md ${
            !canControlTranscription || isSummarizing
              ? 'bg-gray-400 cursor-not-allowed'
              : 'bg-purple-600 hover:bg-purple-700'
          }`}
        >
          <FileText className="h-4 w-4 mr-1" />
          {isSummarizing ? 'Summarizing...' : 'Summarize'}
        </button>
        <button
          onClick={endCall}
          disabled={false}
          className={`col-span-2 flex items-center justify-center px-3 py-2 text-sm font-medium text-white rounded-md bg-red-600 hover:bg-red-700`}
        >
          <PhoneOff className="h-4 w-4 mr-1" />
          End Call
        </button>
      </div>

      {/* Summary */}
      {summary && (
        <div className="bg-blue-50 border-l-4 border-blue-400 p-3 mb-4">
          <div className="flex">
            <AlertCircle className="h-5 w-5 text-blue-400 mr-2 flex-shrink-0" />
            <div>
              <p className="text-sm font-medium text-blue-800 mb-1">Summary</p>
              <p className="text-sm text-gray-700">{summary}</p>
            </div>
          </div>
        </div>
      )}

      {/* Live Transcription */}
      <div>
        <h3 className="text-sm font-semibold text-gray-700 mb-2 flex items-center">
          {isTranscribing && (
            <span className="animate-pulse text-red-500 mr-2">●</span>
          )}
          Live Transcription
        </h3>
        <div ref={transcriptionBoxRef} className="bg-gray-900 text-green-400 rounded-lg p-3 h-64 overflow-y-auto font-mono text-sm">
          {transcriptions.length === 0 ? (
            <div className="text-gray-500 text-center py-8">
              {isTranscribing ? 'Waiting for speech...' : 'Transcription not active'}
            </div>
          ) : (
            <div className="space-y-2">
              {transcriptions.map((line) => (
                <div key={line.id} className="break-words">
                  <span className={`${
                    line.role === 'remote-caller' ? 'text-blue-400' : 'text-green-400'
                  } font-semibold`}>
                    {line.role === 'remote-caller' ? 'Caller' : 'Agent'}:
                  </span>{' '}
                  <span className="text-gray-300">{line.text}</span>
                  {line.confidence > 0 && (
                    <span className="text-gray-600 text-xs ml-2">
                      ({(line.confidence * 100).toFixed(0)}%)
                    </span>
                  )}
                </div>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}