import {
  Bot,
  CheckCircle2,
  Clock3,
  ListOrdered,
  Loader2,
  Pause,
  Radio,
  RotateCcw,
  UserRound,
  UsersRound,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { format } from 'date-fns';
import type { ReactNode } from 'react';
import type {
  CallTimelineResponse,
  HandlingSegmentTimeline,
  QueueAttemptTimeline,
} from '../../types/callcenter';

interface CallJourneyProps {
  timeline: CallTimelineResponse | null;
  isLoading?: boolean;
  error?: string | null;
  variant?: 'dark' | 'light';
}

type JourneyEvent =
  | { kind: 'queue'; timestamp: string; attempt: QueueAttemptTimeline }
  | { kind: 'segment'; timestamp: string; segment: HandlingSegmentTimeline };

const sentenceCase = (value?: string | null) => {
  if (!value) return '';
  const text = value.replace(/[_-]+/g, ' ').replace(/:/g, ': ');
  return text.charAt(0).toUpperCase() + text.slice(1);
};

export const formatJourneyDuration = (seconds?: number | null) => {
  if (seconds == null) return '—';
  if (seconds < 60) return `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  const remainder = seconds % 60;
  if (minutes < 60) return remainder ? `${minutes}m ${remainder}s` : `${minutes}m`;
  const hours = Math.floor(minutes / 60);
  const minuteRemainder = minutes % 60;
  return minuteRemainder ? `${hours}h ${minuteRemainder}m` : `${hours}h`;
};

const formatTimestamp = (value?: string | null) => {
  if (!value) return '—';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '—' : format(date, 'MMM d, h:mm:ss a');
};

const buildEvents = (timeline: CallTimelineResponse): JourneyEvent[] => [
  ...timeline.queueAttempts.map((attempt): JourneyEvent => ({
    kind: 'queue',
    timestamp: attempt.enteredAt,
    attempt,
  })),
  ...timeline.handlingSegments.map((segment): JourneyEvent => ({
    kind: 'segment',
    timestamp: segment.startedAt,
    segment,
  })),
].sort((a, b) => new Date(a.timestamp).getTime() - new Date(b.timestamp).getTime());

const segmentPresentation = (segment: HandlingSegmentTimeline) => {
  const presentations: Record<HandlingSegmentTimeline['type'], {
    title: string;
    actor: string;
    icon: LucideIcon;
  }> = {
    ai: {
      title: 'AI handling',
      actor: segment.aiAgentName || 'AI agent',
      icon: Bot,
    },
    human: {
      title: 'Human handling',
      actor: segment.agentName || (segment.agentId ? `Agent ${segment.agentId}` : 'Human agent'),
      icon: UserRound,
    },
    hold: {
      title: 'Caller on hold',
      actor: segment.agentName || 'Hold interval',
      icon: Pause,
    },
    consultation: {
      title: 'Consultation',
      actor: segment.agentName || 'Consulting agent',
      icon: UsersRound,
    },
  };
  return presentations[segment.type];
};

const queueOutcome = (attempt: QueueAttemptTimeline) => {
  if (attempt.acceptedAt) {
    return attempt.acceptedAgentName
      ? `Accepted by ${attempt.acceptedAgentName}`
      : 'Accepted by an agent';
  }
  if (!attempt.exitedAt) return 'Waiting in queue';
  return sentenceCase(attempt.exitReason) || 'Exited queue';
};

export function CallJourney({
  timeline,
  isLoading = false,
  error = null,
  variant = 'dark',
}: CallJourneyProps) {
  const isDark = variant === 'dark';
  const shell = isDark
    ? 'border border-rule rounded-lg bg-canvas-raised p-3'
    : 'bg-white border border-gray-200 rounded-lg shadow-lg p-6';
  const primary = isDark ? 'text-ink' : 'text-gray-900';
  const secondary = isDark ? 'text-ink-dim' : 'text-gray-500';
  const divider = isDark ? 'border-rule' : 'border-gray-200';
  const events = timeline ? buildEvents(timeline) : [];
  const totalTrackedSeconds = timeline?.handlingSegments.reduce(
    (sum, segment) => sum + (segment.durationSeconds || 0),
    0,
  ) || 0;
  const longestWait = timeline?.queueAttempts.reduce(
    (largest, attempt) => Math.max(largest, attempt.waitSeconds || 0),
    0,
  ) || 0;
  const transport = timeline?.transport
    || timeline?.queueAttempts.find((attempt) => attempt.transport)?.transport;

  if (isLoading) {
    return (
      <div className={shell}>
        <div className={`flex items-center gap-2 ${primary}`}>
          <Loader2 className="w-4 h-4 animate-spin" />
          <span className="text-[12.5px] font-semibold">Loading call journey…</span>
        </div>
      </div>
    );
  }

  return (
    <div className={shell}>
      <div className="flex items-center justify-between gap-3">
        <div className={`inline-flex items-center gap-2 font-semibold ${primary} ${isDark ? 'text-[12.5px]' : 'text-xl'}`}>
          <Radio className={isDark ? 'w-3.5 h-3.5' : 'w-5 h-5'} />
          Call Journey
        </div>
        {transport && (
          <span className={`rounded px-2 py-1 text-[10.5px] font-medium uppercase tracking-wide ${
            isDark
              ? 'bg-canvas-elevated text-ink-muted'
              : 'bg-gray-100 text-gray-600'
          }`}>
            {transport}
          </span>
        )}
      </div>

      {error && events.length === 0 ? (
        <div className={`py-7 text-center text-sm ${secondary}`}>
          Journey temporarily unavailable. The rest of the call record is unaffected.
        </div>
      ) : events.length === 0 ? (
        <div className={`py-7 text-center ${secondary}`}>
          <Clock3 className="w-5 h-5 mx-auto mb-2 opacity-60" />
          <p className="text-sm">Detailed journey data wasn’t recorded for this legacy call.</p>
        </div>
      ) : (
        <>
          <div className={`grid grid-cols-1 sm:grid-cols-3 gap-2 mt-3 pb-3 border-b ${divider}`}>
            {[
              ['Queue visits', timeline?.queueAttempts.length || 0],
              ['Longest wait', formatJourneyDuration(longestWait)],
              ['Tracked time', formatJourneyDuration(totalTrackedSeconds)],
            ].map(([label, value]) => (
              <div key={label} className={isDark ? 'bg-canvas px-2.5 py-2 rounded-md' : 'bg-gray-50 px-3 py-2 rounded-md'}>
                <div className={`text-[10.5px] ${secondary}`}>{label}</div>
                <div className={`mt-0.5 text-sm font-semibold ${primary}`}>{value}</div>
              </div>
            ))}
          </div>

          <div className="relative mt-3">
            <div className={`absolute left-[15px] top-5 bottom-5 w-px ${isDark ? 'bg-rule-strong' : 'bg-gray-200'}`} />
            <div className="space-y-1">
              {events.map((event) => {
                if (event.kind === 'queue') {
                  const { attempt } = event;
                  const active = !attempt.exitedAt;
                  return (
                    <div key={`queue-${attempt.id}`} className="relative flex gap-3 pb-3">
                      <div className={`relative z-10 w-8 h-8 rounded-full border flex items-center justify-center flex-shrink-0 ${
                        isDark
                          ? 'bg-wait-glow border-wait/60 text-wait-soft'
                          : 'bg-amber-50 border-amber-300 text-amber-700'
                      }`}>
                        {attempt.attemptNumber > 1
                          ? <RotateCcw className="w-3.5 h-3.5" />
                          : <ListOrdered className="w-3.5 h-3.5" />}
                      </div>
                      <div className="min-w-0 flex-1 pt-0.5">
                        <div className="flex items-start justify-between gap-2">
                          <div>
                            <div className={`text-sm font-medium ${primary}`}>
                              {attempt.queueDisplayName || sentenceCase(attempt.queueSlug)} queue
                              {attempt.attemptNumber > 1 && (
                                <span className={`ml-1.5 text-[10.5px] ${secondary}`}>visit {attempt.attemptNumber}</span>
                              )}
                            </div>
                            <div className={`text-[11px] mt-0.5 ${secondary}`}>
                              {formatTimestamp(attempt.enteredAt)}
                            </div>
                          </div>
                          <span className={`max-w-[45%] text-right text-[10.5px] font-medium ${
                            active
                              ? isDark ? 'text-wait-soft' : 'text-amber-700'
                              : attempt.acceptedAt
                                ? isDark ? 'text-live-soft' : 'text-emerald-700'
                                : secondary
                          }`}>
                            {queueOutcome(attempt)}
                          </span>
                        </div>
                        <div className="flex flex-wrap gap-1.5 mt-2">
                          {attempt.waitSeconds != null && (
                            <JourneyChip dark={isDark}>Wait {formatJourneyDuration(attempt.waitSeconds)}</JourneyChip>
                          )}
                          {attempt.offerCount > 0 && (
                            <JourneyChip dark={isDark}>{attempt.offerCount} offer{attempt.offerCount === 1 ? '' : 's'}</JourneyChip>
                          )}
                          {attempt.declinedOfferCount > 0 && (
                            <JourneyChip dark={isDark}>{attempt.declinedOfferCount} declined</JourneyChip>
                          )}
                          {attempt.priority != null && (
                            <JourneyChip dark={isDark}>Priority {attempt.priority}</JourneyChip>
                          )}
                          {attempt.routingStrategy && (
                            <JourneyChip dark={isDark}>{sentenceCase(attempt.routingStrategy)}</JourneyChip>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                }

                const { segment } = event;
                const presentation = segmentPresentation(segment);
                const SegmentIcon = presentation.icon;
                const colors = segment.type === 'ai'
                  ? isDark ? 'bg-ai-glow border-ai/60 text-ai-soft' : 'bg-teal-50 border-teal-300 text-teal-700'
                  : segment.type === 'human'
                    ? isDark ? 'bg-live-glow border-live/60 text-live-soft' : 'bg-emerald-50 border-emerald-300 text-emerald-700'
                    : isDark ? 'bg-canvas-elevated border-rule-strong text-ink-muted' : 'bg-gray-100 border-gray-300 text-gray-600';
                return (
                  <div key={`segment-${segment.id}`} className="relative flex gap-3 pb-3">
                    <div className={`relative z-10 w-8 h-8 rounded-full border flex items-center justify-center flex-shrink-0 ${colors}`}>
                      <SegmentIcon className="w-3.5 h-3.5" />
                    </div>
                    <div className="min-w-0 flex-1 pt-0.5">
                      <div className="flex items-start justify-between gap-2">
                        <div>
                          <div className={`text-sm font-medium ${primary}`}>{presentation.title}</div>
                          <div className={`text-[11px] mt-0.5 ${secondary}`}>
                            {presentation.actor} · {formatTimestamp(segment.startedAt)}
                          </div>
                        </div>
                        <div className={`inline-flex items-center gap-1 text-[11px] ${secondary}`}>
                          {segment.endedAt ? <CheckCircle2 className="w-3 h-3" /> : <Radio className="w-3 h-3" />}
                          {segment.durationSeconds != null
                            ? formatJourneyDuration(segment.durationSeconds)
                            : 'In progress'}
                        </div>
                      </div>
                      {segment.endReason && (
                        <div className={`text-[10.5px] mt-1 ${secondary}`}>
                          {sentenceCase(segment.endReason)}
                        </div>
                      )}
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function JourneyChip({
  children,
  dark,
}: {
  children: ReactNode;
  dark: boolean;
}) {
  return (
    <span className={`rounded px-1.5 py-0.5 text-[10.5px] ${
      dark ? 'bg-canvas-elevated text-ink-muted' : 'bg-gray-100 text-gray-600'
    }`}>
      {children}
    </span>
  );
}
