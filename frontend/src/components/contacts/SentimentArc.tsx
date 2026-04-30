import { useMemo } from 'react';
import {
  ResponsiveContainer,
  AreaChart,
  Area,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  ReferenceLine,
  Dot,
} from 'recharts';
import { Activity, TrendingUp, TrendingDown, Minus } from 'lucide-react';

export interface SentimentSegment {
  /** "positive" | "negative" | "neutral" — string labels from the transcription model. */
  sentiment?: string | null;
  /** ISO timestamp the segment was created. */
  timestamp?: string | null;
  /** Optional speaker tag (caller / agent / ai). */
  speaker?: string | null;
  /** Optional snippet — used as tooltip context. */
  text?: string | null;
}

interface SentimentArcProps {
  segments: SentimentSegment[];
  /** Overall call sentiment in [-1, 1]. */
  overallScore?: number | null;
}

const SENTIMENT_TO_VALUE: Record<string, number> = {
  positive: 1,
  neutral: 0,
  negative: -1,
};

const COLORS = {
  positive: '#3FB77E',
  neutral: '#9CA3AF',
  negative: '#F0516E',
  axis: '#4B5563',
  grid: '#374151',
  area: '#8A7BFF',
};

interface ChartPoint {
  index: number;
  /** Numeric sentiment in [-1, 1]. */
  value: number;
  /** Original label. */
  label: 'positive' | 'neutral' | 'negative';
  /** Speaker tag if any. */
  speaker?: string;
  /** First ~80 chars of segment text. */
  snippet?: string;
  /** Pretty time string (h:mm:ss). */
  time?: string;
}

function classifyScore(score: number): { label: string; color: string; Icon: typeof TrendingUp } {
  if (score > 0.3) return { label: 'Positive', color: 'text-green-400', Icon: TrendingUp };
  if (score < -0.3) return { label: 'Negative', color: 'text-red-400', Icon: TrendingDown };
  return { label: 'Neutral', color: 'text-gray-400', Icon: Minus };
}

export function SentimentArc({ segments, overallScore }: SentimentArcProps) {
  const data = useMemo<ChartPoint[]>(() => {
    let runningTotal = 0;
    const points: ChartPoint[] = [];
    segments.forEach((seg, idx) => {
      const raw = (seg.sentiment ?? '').toLowerCase();
      if (!(raw in SENTIMENT_TO_VALUE)) return;
      const value = SENTIMENT_TO_VALUE[raw];
      runningTotal += value;
      // Use a simple windowed running average for smoothing — keeps the chart from jittering on every utterance.
      const windowSize = Math.min(3, points.length + 1);
      const windowSum = points.slice(-windowSize + 1).reduce((acc, p) => acc + p.value, value);
      const smoothed = windowSum / windowSize;
      points.push({
        index: idx,
        value: smoothed,
        label: raw as ChartPoint['label'],
        speaker: seg.speaker ?? undefined,
        snippet: seg.text ? seg.text.slice(0, 80) + (seg.text.length > 80 ? '…' : '') : undefined,
        time: seg.timestamp ? new Date(seg.timestamp).toLocaleTimeString() : undefined,
      });
    });
    return points;
    // We intentionally don't expose runningTotal — overallScore from the backend is the source of truth.
    void runningTotal;
  }, [segments]);

  // No tagged sentiment data — render a friendly "not available" pane instead of an empty chart.
  if (data.length === 0) {
    return (
      <div className="rounded-lg bg-gray-900/50 border border-gray-800 p-5">
        <div className="flex items-center gap-2 text-sm font-semibold text-white mb-1">
          <Activity className="w-4 h-4 text-purple-400" />
          Sentiment arc
        </div>
        <p className="text-xs text-gray-500">
          No sentiment data tagged for this call.
        </p>
      </div>
    );
  }

  const overall = typeof overallScore === 'number' ? overallScore : null;
  const overallMeta = overall !== null ? classifyScore(overall) : null;

  return (
    <div className="rounded-lg bg-gray-900/50 border border-gray-800 p-5">
      <div className="flex items-center justify-between mb-3">
        <div className="flex items-center gap-2 text-sm font-semibold text-white">
          <Activity className="w-4 h-4 text-purple-400" />
          Sentiment arc
        </div>
        {overall !== null && overallMeta && (
          <div className={`flex items-center gap-1.5 text-xs ${overallMeta.color}`}>
            <overallMeta.Icon className="w-3.5 h-3.5" />
            <span>{overallMeta.label}</span>
            <span className="text-gray-500">·</span>
            <span className="font-mono">{overall > 0 ? '+' : ''}{overall.toFixed(2)}</span>
          </div>
        )}
      </div>

      <div className="h-32 -ml-2">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 4, right: 8, left: 0, bottom: 4 }}>
            <defs>
              <linearGradient id="sentiment-fill" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor={COLORS.area} stopOpacity={0.35} />
                <stop offset="100%" stopColor={COLORS.area} stopOpacity={0.0} />
              </linearGradient>
            </defs>
            <CartesianGrid stroke={COLORS.grid} strokeDasharray="2 4" vertical={false} />
            <XAxis dataKey="index" hide />
            <YAxis
              domain={[-1, 1]}
              ticks={[-1, 0, 1]}
              tick={{ fill: COLORS.axis, fontSize: 10 }}
              axisLine={false}
              tickLine={false}
              tickFormatter={(v) => (v === 1 ? '+' : v === -1 ? '−' : '·')}
              width={20}
            />
            <ReferenceLine y={0} stroke={COLORS.axis} strokeDasharray="2 2" />
            <Tooltip
              cursor={{ stroke: COLORS.area, strokeOpacity: 0.4 }}
              content={({ active, payload }) => {
                if (!active || !payload || payload.length === 0) return null;
                const point = payload[0].payload as ChartPoint;
                return (
                  <div className="rounded border border-gray-700 bg-gray-900 px-2.5 py-1.5 text-xs shadow-lg">
                    <div style={{ color: COLORS[point.label] }} className="font-semibold capitalize">
                      {point.label}
                      {point.speaker ? <span className="text-gray-400"> · {point.speaker}</span> : null}
                    </div>
                    {(point.snippet || point.time) && (
                      <div className="text-gray-400 mt-0.5">
                        {point.snippet ?? point.time}
                      </div>
                    )}
                  </div>
                );
              }}
            />
            <Area
              type="monotone"
              dataKey="value"
              stroke={COLORS.area}
              strokeWidth={2}
              fill="url(#sentiment-fill)"
              dot={(props) => {
                const { cx, cy, payload } = props;
                const point = payload as ChartPoint;
                return (
                  <Dot
                    key={`sentiment-dot-${point.index}`}
                    cx={cx}
                    cy={cy}
                    r={3}
                    fill={COLORS[point.label]}
                    stroke={COLORS[point.label]}
                  />
                );
              }}
              activeDot={{ r: 5 }}
              isAnimationActive={false}
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>

      {/* Quick legend — counts per label */}
      <div className="mt-2 flex items-center gap-3 text-[11px] text-gray-500">
        {(['positive', 'neutral', 'negative'] as const).map((label) => {
          const count = data.filter((p) => p.label === label).length;
          if (count === 0) return null;
          return (
            <span key={label} className="flex items-center gap-1.5">
              <span
                className="inline-block w-2 h-2 rounded-full"
                style={{ background: COLORS[label] }}
              />
              <span className="capitalize">{label}</span>
              <span className="font-mono text-gray-600">×{count}</span>
            </span>
          );
        })}
        <span className="ml-auto text-gray-600">{data.length} segments</span>
      </div>
    </div>
  );
}
