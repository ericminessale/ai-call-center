import { useState, useEffect, useRef } from 'react';
import { PhoneIncoming, PhoneOutgoing, Mic, Play, Download, User } from 'lucide-react';
import { Interaction, CallLeg } from '../../types/callcenter';
import type { CallTimelineResponse } from '../../types/callcenter';
import api, { callsApi } from '../../services/api';
import { logger } from '../../lib/logger';
import { CallTimeline } from './CallTimeline';
import { CallJourney } from './CallJourney';
import { AISummaryDisplay } from './ContactDetailView';
import { SentimentArc, SentimentSegment } from './SentimentArc';
import { WrapUpPanel } from './WrapUpPanel';
import {
  Chip,
  ContextBox,
  TranscriptUtterance,
  TranscriptDivider,
  AI_GLYPH,
  type RestraintStatus,
} from '../restraint';

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
  const [timeline, setTimeline] = useState<CallTimelineResponse | null>(null);
  const [isLoadingTimeline, setIsLoadingTimeline] = useState(true);
  const [timelineError, setTimelineError] = useState<string | null>(null);
  const scrollRef = useRef<HTMLDivElement>(null);

  // Fetch transcriptions and legs for this call
  useEffect(() => {
    const fetchCallDetails = async () => {
      setIsLoadingTranscriptions(true);
      setIsLoadingTimeline(true);
      setTimelineError(null);
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

        try {
          const timelineResponse = await callsApi.getTimeline(interaction.signalwireCallSid);
          setTimeline(timelineResponse.data);
        } catch (timelineLoadError) {
          logger.debug('No measured journey data available for this call');
          setTimeline(null);
          setTimelineError('unavailable');
        }
      } catch (error) {
        logger.error('Failed to load call details:', error);
        setTranscriptions([]);
        setLegs([]);
      } finally {
        setIsLoadingTranscriptions(false);
        setIsLoadingTimeline(false);
      }
    };

    fetchCallDetails();
  }, [interaction.signalwireCallSid]);

  // Sentiment score → Restraint status dot + label (color only signals deviation).
  const sentimentStatus: RestraintStatus | null =
    interaction.sentimentScore == null ? null :
    interaction.sentimentScore > 0.3 ? 'success' :
    interaction.sentimentScore < -0.3 ? 'error' :
    'neutral';

  const directionLabel = interaction.direction === 'inbound' ? 'Inbound call' : 'Outbound call';
  const hasMeasuredJourney = !!timeline && (
    timeline.queueAttempts.length > 0 || timeline.handlingSegments.length > 0
  );

  return (
    /* Flows naturally; the parent tab pane (overflow-y-auto) does the scrolling
       — like the History/Notes tabs. Avoids the old fixed-height layout that
       squeezed the telemetry into a cramped internal-scroll window and clipped
       the Wrap-up panel. */
    <div className="flex flex-col bg-canvas min-h-full">
      {/* Header — handler progression chip flow */}
      <div className="px-5 py-4 border-b border-rule">
        <div className="flex items-center gap-3 flex-wrap">
          {/* Direction glyph */}
          <div className="w-9 h-9 rounded-md border border-rule-strong bg-canvas-raised flex items-center justify-center text-ink-muted flex-shrink-0">
            {interaction.direction === 'inbound'
              ? <PhoneIncoming className="w-4 h-4" />
              : <PhoneOutgoing className="w-4 h-4" />}
          </div>

          <div className="min-w-0">
            <div className="text-[15px] font-semibold text-ink leading-none">{directionLabel}</div>
            <div className="mono text-[11.5px] text-ink-dim mt-1">
              {formatDate(interaction.createdAt)} · {formatDuration(interaction.duration)}
            </div>
          </div>

          {/* Handler chain: AI agent → human, status + sentiment */}
          <div className="flex items-center gap-2 flex-wrap ml-auto">
            {interaction.handlerType === 'ai' && (
              <Chip ai>{interaction.aiAgentName || 'AI Agent'}</Chip>
            )}
            {legs.length > 1 && <span className="text-ink-dim text-xs">→</span>}
            {interaction.handlerType !== 'ai' && (
              <Chip>
                <User className="w-3 h-3" />
                Human agent
              </Chip>
            )}
            {interaction.status && (
              <Chip
                dot={
                  interaction.status === 'completed' ? 'success'
                  : interaction.status === 'failed' || interaction.status === 'abandoned' ? 'error'
                  : 'neutral'
                }
                className="capitalize"
              >
                {interaction.status.replace(/_/g, ' ')}
              </Chip>
            )}
            {sentimentStatus && (
              <Chip dot={sentimentStatus}>
                {interaction.sentimentScore! > 0 ? '+' : ''}{interaction.sentimentScore!.toFixed(1)}
              </Chip>
            )}
          </div>
        </div>

        {/* AI Summary */}
        {interaction.summary && (
          <div className="mt-3 border border-rule rounded-lg bg-canvas-raised p-3">
            <div className="text-[11px] font-medium text-ink-dim mb-1">AI summary</div>
            <div className="text-sm text-ink-muted leading-relaxed">
              <AISummaryDisplay summary={interaction.summary} />
            </div>
          </div>
        )}
      </div>

      {/* Two-column body: transcript (left) + telemetry (right). The transcript
          is just a chat log, so it's a comfortable reading column — NOT the
          dominant surface. Telemetry (recording, timeline, sentiment, wrap-up)
          gets the larger share rather than being squeezed into a 320px margin. */}
      <div className="grid grid-cols-[minmax(0,1fr)_minmax(0,1.15fr)] gap-4 p-5 items-stretch">
        {/* LEFT — Transcript feed (caps at a readable height; scrolls internally
            for long transcripts, the page scrolls for everything else). */}
        <div className="border border-rule rounded-lg bg-canvas-raised flex flex-col overflow-hidden">
          <div className="flex items-center justify-between px-4 h-10 border-b border-rule flex-shrink-0">
            <span className="text-[13px] font-semibold text-ink">Transcript</span>
            {transcriptions.length > 0 && (
              <span className="text-[11px] font-medium text-ink-dim">{transcriptions.length} utterances</span>
            )}
          </div>
          <div className="flex-1 overflow-y-auto px-4 py-2 min-h-0">
            {isLoadingTranscriptions ? (
              <div className="flex items-center justify-center h-32 text-ink-dim text-sm gap-2">
                <div className="animate-spin w-4 h-4 border-2 border-ink-dim border-t-transparent rounded-full" />
                Loading transcription…
              </div>
            ) : transcriptions.length > 0 ? (
              <div className="divide-y divide-rule/40">
                {transcriptions.map((entry, idx) => {
                  if (entry.speaker === 'system') {
                    // Synthetic marker row (AI→human handoff) — a seam, not speech.
                    return <TranscriptDivider key={idx} label={entry.text} />;
                  }
                  const isAgent = entry.speaker === 'agent' || entry.speaker === 'ai';
                  const speakerLabel = entry.speaker === 'agent' ? 'Agent' : entry.speaker === 'ai' ? 'AI' : 'Caller';
                  return (
                    <TranscriptUtterance
                      key={idx}
                      speaker={entry.speaker === 'ai' ? `${AI_GLYPH} AI` : speakerLabel}
                      role={isAgent ? 'agent' : 'caller'}
                      text={entry.text}
                      timestamp={entry.timestamp ? new Date(entry.timestamp).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' }) : undefined}
                    />
                  );
                })}
                <div ref={scrollRef} />
              </div>
            ) : (
              <div className="flex flex-col items-center justify-center h-32 text-center text-ink-dim">
                <Mic className="w-6 h-6 mb-2 opacity-50" />
                <p className="text-sm">No transcription available for this call</p>
              </div>
            )}
          </div>
        </div>

        {/* RIGHT — telemetry: recording, timeline, sentiment, wrap-up. Flows
            fully (no internal scroll/clip); the page scrolls to reveal all of it. */}
        <div className="flex flex-col gap-3">
          {/* Recording */}
          {interaction.recordingUrl && (
            <div className="border border-rule rounded-lg bg-canvas-raised p-3">
              <div className="flex items-center justify-between mb-2">
                <span className="inline-flex items-center gap-1.5 text-[12.5px] font-semibold text-ink">
                  <Play className="w-3.5 h-3.5 text-ink-muted" />
                  Recording
                </span>
                <a
                  href={interaction.recordingUrl}
                  download
                  className="inline-flex items-center gap-1.5 text-xs text-ink-dim hover:text-ink transition-colors"
                  title="Download recording"
                >
                  <Download className="w-3.5 h-3.5" />
                  Download
                </a>
              </div>
              <audio
                controls
                preload="metadata"
                className="w-full h-9"
              >
                <source src={interaction.recordingUrl} type="audio/mpeg" />
                <source src={interaction.recordingUrl} type="audio/wav" />
                Your browser doesn&apos;t support audio playback.
              </audio>
              <div className="mt-2 mono text-xs text-ink-dim">
                Duration: {formatDuration(interaction.duration)}
              </div>
            </div>
          )}

          {/* Call metadata grid */}
          <ContextBox
            title="Details"
            items={[
              { key: 'From', value: <span className="mono">{interaction.fromNumber || '—'}</span> },
              { key: 'To', value: <span className="mono">{interaction.destination || '—'}</span> },
              { key: 'Status', value: <span className="capitalize">{interaction.status || '—'}</span> },
              { key: 'Handler', value: <span className="capitalize">{interaction.handlerType || '—'}</span> },
            ]}
          />

          {/* Measured journey; pre-migration calls fall back to legacy legs. */}
          {(isLoadingTimeline || hasMeasuredJourney || legs.length === 0) && (
            <CallJourney
              timeline={timeline}
              isLoading={isLoadingTimeline}
              error={timelineError}
            />
          )}
          {!isLoadingTimeline && !hasMeasuredJourney && legs.length > 0 && (
            <div className="border border-rule rounded-lg bg-canvas-raised p-3">
              <div className="text-[12.5px] font-semibold text-ink mb-2">Legacy handler journey</div>
              <CallTimeline
                legs={legs}
                showHeading={false}
                callEnded={!!interaction.endedAt || ['completed', 'ended', 'failed', 'abandoned', 'no_answer', 'missed', 'canceled'].includes(interaction.status || '')}
              />
            </div>
          )}

          {/* Sentiment arc — kept (PRESERVE) */}
          {!isLoadingTranscriptions && (
            <div className="border border-rule rounded-lg bg-canvas-raised p-3">
              <SentimentArc
                segments={transcriptions as SentimentSegment[]}
                overallScore={interaction.sentimentScore}
              />
            </div>
          )}

          {/* Wrap-up panel — only renders for ended/wrapping calls (Tier 2a) */}
          <WrapUpPanel interaction={interaction} onUpdate={onInteractionPatch} />
        </div>
      </div>
    </div>
  );
}
