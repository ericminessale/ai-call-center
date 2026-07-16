/**
 * CallEventStream - Real-time event feed for active calls
 *
 * Shows a scrollable, timestamped list of call events as they happen:
 * state changes, hold/unhold, record, play, DTMF, AI tool calls,
 * conference events, sentiment updates, etc.
 *
 * Subscribes to `call_event` socket events.
 */

import React, { useState, useEffect, useRef } from 'react';
import {
  Activity,
  Pause,
  Circle,
  Volume2,
  Hash,
  Headphones,
  Users,
  Bot,
  TrendingUp,
  Phone,
  PhoneOff,
  ArrowRightLeft,
  ChevronDown,
  ChevronUp,
  Zap,
} from 'lucide-react';
import { useSocketContext } from '../../contexts/SocketContext';

interface CallEvent {
  call_id: number | string;
  event_type: string;
  data: Record<string, any>;
  timestamp: string;
}

interface CallEventStreamProps {
  callId: number | string;
  callSid?: string;
  maxEvents?: number;
}

const EVENT_CONFIG: Record<string, { icon: React.ElementType; color: string; label: string }> = {
  state_change: { icon: Phone, color: 'text-blue-400', label: 'Status' },
  hold: { icon: Pause, color: 'text-yellow-400', label: 'Hold' },
  record: { icon: Circle, color: 'text-red-400', label: 'Record' },
  play: { icon: Volume2, color: 'text-green-400', label: 'Play' },
  dtmf: { icon: Hash, color: 'text-gray-400', label: 'DTMF' },
  monitor: { icon: Headphones, color: 'text-indigo-400', label: 'Monitor' },
  conference: { icon: Users, color: 'text-amber-400', label: 'Conference' },
  ai_tool_call: { icon: Bot, color: 'text-purple-400', label: 'AI Tool' },
  sentiment: { icon: TrendingUp, color: 'text-cyan-400', label: 'Sentiment' },
  transcription: { icon: Activity, color: 'text-gray-500', label: 'Transcript' },
  transfer: { icon: ArrowRightLeft, color: 'text-orange-400', label: 'Transfer' },
  ended: { icon: PhoneOff, color: 'text-red-500', label: 'Ended' },
};

function formatEventTime(timestamp: string): string {
  const date = new Date(timestamp);
  return date.toLocaleTimeString('en-US', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
    hour12: false,
  });
}

function getEventDescription(event: CallEvent): string {
  const { event_type, data } = event;

  switch (event_type) {
    case 'state_change':
      return `Status changed to ${data.status || data.new_status || 'unknown'}`;
    case 'hold':
      return data.action === 'hold'
        ? `Call placed on hold by ${data.agent || 'agent'}`
        : `Call resumed by ${data.agent || 'agent'}`;
    case 'record':
      return data.action === 'start'
        ? `Recording started by ${data.agent || 'agent'}`
        : `Recording stopped by ${data.agent || 'agent'}`;
    case 'play':
      if (data.action === 'play_tts') return `TTS: "${data.text?.substring(0, 50)}..."`;
      return `Audio playing: ${data.url || 'file'}`;
    case 'dtmf':
      return `DTMF sent: ${data.digits}`;
    case 'monitor':
      return data.action === 'start'
        ? `${data.agent || 'Supervisor'} started monitoring (${data.monitor_type || 'tap'})`
        : `${data.agent || 'Supervisor'} stopped monitoring`;
    case 'conference':
      if (data.action === 'backup_requested') return `${data.requesting_agent} requested backup agent`;
      if (data.action === 'escalation_requested') return `${data.requesting_agent} escalated to ${data.supervisor}${data.whisper_mode ? ' (whisper)' : ''}`;
      if (data.action === 'participant_joined') return `${data.participant || 'Someone'} joined the conference`;
      if (data.action === 'participant_left') return `${data.participant || 'Someone'} left the conference`;
      return `Conference event: ${data.action || 'update'}`;
    case 'ai_tool_call':
      return `AI called: ${data.function_name || data.tool || 'function'}`;
    case 'sentiment':
      const score = data.score || data.sentiment_score;
      const label = score > 0.3 ? 'positive' : score < -0.3 ? 'negative' : 'neutral';
      return `Sentiment: ${label} (${typeof score === 'number' ? score.toFixed(2) : score})${data.reason ? ` - ${data.reason}` : ''}`;
    case 'transfer':
      return `Transfer to ${data.destination || data.target || 'agent'}`;
    case 'ended':
      return `Call ended: ${data.reason || 'hangup'}`;
    default:
      return JSON.stringify(data).substring(0, 80);
  }
}

export default function CallEventStream({ callId, callSid, maxEvents = 100 }: CallEventStreamProps) {
  const [events, setEvents] = useState<CallEvent[]>([]);
  const [expanded, setExpanded] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);
  const { socket } = useSocketContext();

  useEffect(() => {
    if (!socket) return;

    const handleCallEvent = (event: CallEvent) => {
      // Filter to events for this call
      const eventCallId = String(event.call_id);
      if (eventCallId !== String(callId) && eventCallId !== callSid) return;

      setEvents(prev => {
        const updated = [...prev, event];
        // Keep only the most recent events
        return updated.slice(-maxEvents);
      });
    };

    socket.on('call_event', handleCallEvent);
    return () => {
      socket.off('call_event', handleCallEvent);
    };
  }, [socket, callId, callSid, maxEvents]);

  // Auto-scroll to bottom on new events
  useEffect(() => {
    if (scrollRef.current && expanded) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [events, expanded]);

  return (
    <div className="mt-3">
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 text-xs text-gray-400 hover:text-gray-300 transition-colors mb-1"
      >
        {expanded ? <ChevronUp className="w-3 h-3" /> : <ChevronDown className="w-3 h-3" />}
        <Zap className="w-3 h-3 text-amber-400" />
        <span className="uppercase tracking-wider font-medium">Event Stream</span>
        {events.length > 0 && (
          <span className="px-1.5 py-0.5 bg-gray-700 text-gray-400 text-[10px] rounded-full">
            {events.length}
          </span>
        )}
      </button>

      {expanded && (
        <div
          ref={scrollRef}
          className="max-h-48 overflow-y-auto bg-gray-900/50 border border-gray-700/50 rounded-lg"
        >
          {events.length === 0 ? (
            <div className="p-3 text-center text-gray-500 text-xs">
              Waiting for call events...
            </div>
          ) : (
            <div className="divide-y divide-gray-800/50">
              {events.map((event, idx) => {
                const config = EVENT_CONFIG[event.event_type] || { icon: Activity, color: 'text-gray-400', label: event.event_type };
                const Icon = config.icon;

                return (
                  <div key={idx} className="flex items-start gap-2 px-3 py-1.5 hover:bg-gray-800/30">
                    <span className="text-[10px] text-gray-500 font-mono mt-0.5 shrink-0">
                      {formatEventTime(event.timestamp)}
                    </span>
                    <Icon className={`w-3 h-3 mt-0.5 shrink-0 ${config.color}`} />
                    <span className="text-xs text-gray-300 leading-tight">
                      {getEventDescription(event)}
                    </span>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      )}
    </div>
  );
}
