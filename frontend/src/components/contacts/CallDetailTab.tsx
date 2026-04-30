import { useState, useEffect, useRef } from 'react';
import { PhoneIncoming, PhoneOutgoing, Bot, Mic, Play, Download } from 'lucide-react';
import { Interaction, CallLeg } from '../../types/callcenter';
import api from '../../services/api';
import { logger } from '../../lib/logger';
import { CallTimeline } from './CallTimeline';
import { AISummaryDisplay } from './ContactDetailView';
import { SentimentArc, SentimentSegment } from './SentimentArc';
import { WrapUpPanel } from './WrapUpPanel';

interface CallDetailTabProps {
  interaction: Interaction;
  formatDate: (date?: string) => string;
  formatDuration: (seconds?: number) => string;
  /** Optional: parent passes a callback so wrap-up edits propagate to the
   *  surrounding interaction list without a full refetch. */
  onInteractionPatch?: (patch: Partial<Interaction>) => void;
}

export function CallDetailTab({
  interaction,
  formatDate,
  formatDuration,
  onInteractionPatch,
}: CallDetailTabProps) {
  const [transcriptions, setTranscriptions] = useState<{ speaker: string; text: string; timestamp: string; sentiment?: string | null }[]>([]);
  const [legs, setLegs] = useState<CallLeg[]>([]);
  const [isLoadingTranscriptions, setIsLoadingTranscriptions] = useState(true);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Fetch transcriptions and legs for this call
  useEffect(() => {
    const fetchCallDetails = async () => {
      setIsLoadingTranscriptions(true);
      try {
        const response = await api.get(`/api/calls/${interaction.signalwireCallSid}`);
        const data = response.data.transcriptions || [];
        setTranscriptions(data.map((t: any) => ({
          speaker: t.speaker || 'caller',
          text: t.transcript || t.text,
          timestamp: t.createdAt || t.created_at,
          sentiment: t.sentiment ?? null,
        })));

        try {
          const legsResponse = await api.get(`/api/calls/${interaction.signalwireCallSid}/legs`);
          setLegs(legsResponse.data.legs || []);
        } catch (legsError) {
          logger.debug('No legs data available for this call');
          setLegs([]);
        }
      } catch (error) {
        logger.error('Failed to load call details:', error);
        setTranscriptions([]);
        setLegs([]);
      } finally {
        setIsLoadingTranscriptions(false);
      }
    };

    fetchCallDetails();
  }, [interaction.signalwireCallSid]);

  return (
    <div className="h-full flex flex-col">
      {/* Call Info Header */}
      <div className="p-4 border-b border-gray-700 bg-gray-800/50">
        <div className="flex items-center gap-3 mb-3">
          <div className={`w-12 h-12 rounded-full flex items-center justify-center ${
            interaction.direction === 'inbound' ? 'bg-blue-500/20' : 'bg-green-500/20'
          }`}>
            {interaction.direction === 'inbound' ? (
              <PhoneIncoming className="w-6 h-6 text-blue-400" />
            ) : (
              <PhoneOutgoing className="w-6 h-6 text-green-400" />
            )}
          </div>
          <div>
            <h3 className="text-lg font-semibold text-white">
              {interaction.direction === 'inbound' ? 'Inbound' : 'Outbound'} Call
            </h3>
            <p className="text-sm text-gray-400">
              {formatDate(interaction.createdAt)} • {formatDuration(interaction.duration)}
            </p>
          </div>
          {interaction.handlerType === 'ai' && (
            <span className="flex items-center gap-1 px-3 py-1 bg-purple-500/20 text-purple-400 text-sm rounded-full ml-auto">
              <Bot className="w-4 h-4" />
              {interaction.aiAgentName || 'AI Agent'}
            </span>
          )}
        </div>

        {/* Call Details Grid */}
        <div className="grid grid-cols-2 gap-4 text-sm">
          <div>
            <span className="text-gray-400">From:</span>
            <span className="text-white ml-2">{interaction.fromNumber || '--'}</span>
          </div>
          <div>
            <span className="text-gray-400">To:</span>
            <span className="text-white ml-2">{interaction.destination || '--'}</span>
          </div>
          <div>
            <span className="text-gray-400">Status:</span>
            <span className="text-white ml-2 capitalize">{interaction.status}</span>
          </div>
          <div>
            <span className="text-gray-400">Handler:</span>
            <span className="text-white ml-2 capitalize">{interaction.handlerType}</span>
          </div>
          {interaction.sentimentScore != null && (
            <div>
              <span className="text-gray-500">Sentiment:</span>
              <span className={`ml-2 ${
                interaction.sentimentScore > 0.3 ? 'text-green-400' :
                interaction.sentimentScore < -0.3 ? 'text-red-400' :
                'text-gray-300'
              }`}>
                {interaction.sentimentScore > 0 ? '+' : ''}{interaction.sentimentScore.toFixed(1)}
              </span>
            </div>
          )}
        </div>

        {/* Summary if available */}
        {interaction.summary && (
          <div className="mt-4 p-3 bg-gray-900 rounded-lg">
            <h4 className="text-sm font-medium text-gray-300 mb-1">AI Summary</h4>
            <div className="text-sm text-gray-300">
              <AISummaryDisplay summary={interaction.summary} />
            </div>
          </div>
        )}

        {/* Call Journey Timeline */}
        {legs.length > 0 && (
          <CallTimeline legs={legs} />
        )}

        {/* Sentiment Arc — only render once we have segments to map */}
        {!isLoadingTranscriptions && (
          <div className="mt-4">
            <SentimentArc
              segments={transcriptions as SentimentSegment[]}
              overallScore={interaction.sentimentScore}
            />
          </div>
        )}

        {/* Wrap-up panel — only renders for ended/wrapping calls (Tier 2a) */}
        <div className="mt-4">
          <WrapUpPanel interaction={interaction} onUpdate={onInteractionPatch} />
        </div>
      </div>

      {/* Transcription Section */}
      <div className="flex-[2] min-h-[300px] flex flex-col">
        <h4 className="text-sm font-semibold text-white px-4 py-3 border-b border-gray-700 bg-gray-800/80 sticky top-0 z-10">Call Transcription</h4>
        <div className="flex-1 p-4 overflow-y-auto">

        {isLoadingTranscriptions ? (
          <div className="flex items-center justify-center h-32 text-gray-400">
            <div className="animate-spin w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full mr-2" />
            Loading transcription...
          </div>
        ) : transcriptions.length > 0 ? (
          <div className="bg-gray-900 rounded-lg p-4 space-y-3 font-mono text-sm">
            {transcriptions.map((entry, idx) => (
              <div key={idx} className="flex flex-col space-y-1">
                <div className="flex items-center space-x-2">
                  <span className={`font-semibold ${
                    entry.speaker === 'agent' || entry.speaker === 'ai' ? 'text-purple-400' : 'text-blue-400'
                  }`}>
                    {entry.speaker === 'agent' ? 'Agent:' : entry.speaker === 'ai' ? 'AI:' : 'Caller:'}
                  </span>
                  {entry.timestamp && (
                    <span className="text-xs text-gray-400">
                      {new Date(entry.timestamp).toLocaleTimeString()}
                    </span>
                  )}
                </div>
                <p className="text-gray-300 pl-4">{entry.text}</p>
              </div>
            ))}
            <div ref={scrollRef} />
          </div>
        ) : (
          <div className="bg-gray-900 rounded-lg p-8 text-center text-gray-500">
            <Mic className="w-10 h-10 mx-auto mb-2 opacity-50" />
            <p>No transcription available for this call</p>
          </div>
        )}
        </div>
      </div>

      {/* Recording playback if available */}
      {interaction.recordingUrl && (
        <div className="p-4 border-t border-gray-700 bg-gray-800/30">
          <div className="flex items-center justify-between mb-2">
            <h4 className="text-sm font-semibold text-white flex items-center gap-2">
              <Play className="w-4 h-4 text-blue-400" />
              Call Recording
            </h4>
            <a
              href={interaction.recordingUrl}
              download
              className="flex items-center gap-1.5 text-xs text-gray-400 hover:text-blue-300 transition-colors"
              title="Download recording"
            >
              <Download className="w-3.5 h-3.5" />
              Download
            </a>
          </div>
          <audio
            controls
            preload="metadata"
            className="w-full h-10 [&::-webkit-media-controls-panel]:bg-gray-900"
          >
            <source src={interaction.recordingUrl} type="audio/mpeg" />
            <source src={interaction.recordingUrl} type="audio/wav" />
            Your browser doesn&apos;t support audio playback.
          </audio>
          <div className="mt-2 text-xs text-gray-500">
            Duration: {formatDuration(interaction.duration)}
          </div>
        </div>
      )}
    </div>
  );
}
