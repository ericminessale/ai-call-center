import { useEffect, useState } from 'react';
import { Call } from '../../types/callcenter';
import { callsApi, queueApi } from '../../services/api';
import ObserverControls from '../shared/ObserverControls';
import { FloorStatGrid, StatusDot, QueueDepthRow, AttentionBar, Button, AI_GLYPH } from '../restraint';
import type { FloorStatTileProps, RestraintStatus } from '../restraint';

interface DashboardChartsProps {
  activeCalls: Call[];
  queuedCalls: Call[];
  /** Opens a call in the detail pane (used by the attention bar). */
  onSelectCall?: (call: Call) => void;
}

// Wallboard row from GET /api/queues/wallboard (IMP-18)
interface WallboardRow {
  slug: string;
  display_name: string;
  depth: number;
  average_wait_seconds: number;
  longest_wait_seconds: number;
  available_agents: number;
  service_level: number | null;
  sla_threshold_seconds: number;
  offered_24h: number;
  answered_24h: number;
  abandoned_24h: number;
  abandon_rate: number | null;
}

// Day aggregate from GET /api/calls/cost-summary (IMP-01)
interface CostSummaryData {
  total_estimated: number;
  call_count: number;
  ai_minutes: number;
  human_minutes: number;
}

function formatWaitShort(seconds: number): string {
  const s = Math.max(0, Math.round(seconds || 0));
  const m = Math.floor(s / 60);
  return m > 0 ? `${m}m ${s % 60}s` : `${s}s`;
}

export function DashboardCharts({ activeCalls, queuedCalls, onSelectCall }: DashboardChartsProps) {
  // Self-fetched operational surfaces: SLA wallboard (IMP-18) and today's
  // estimated spend (IMP-01). The socket queue_update events don't carry
  // these, so light polling keeps the floor view honest.
  const [wallboard, setWallboard] = useState<WallboardRow[]>([]);
  const [costSummary, setCostSummary] = useState<CostSummaryData | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      queueApi.getWallboard()
        .then((res) => { if (!cancelled) setWallboard(res.data.queues || []); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 15000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      callsApi.costSummary()
        .then((res) => { if (!cancelled) setCostSummary(res.data); })
        .catch(() => {});
    };
    load();
    const id = setInterval(load, 60000);
    return () => { cancelled = true; clearInterval(id); };
  }, []);

  const queueDepthMap: Record<string, number> = {};
  queuedCalls.forEach(c => {
    const q = c.queueId || (c as any).queue_id || 'unknown';
    queueDepthMap[q] = (queueDepthMap[q] || 0) + 1;
  });
  // Bars scale against a fixed capacity (not the current peak) so a lone
  // shallow queue doesn't read as "full". SLA threshold comes from config.
  const QUEUE_CAPACITY = 5;
  const slaThreshold = wallboard[0]?.sla_threshold_seconds ?? 60;
  const queueRows = Object.entries(queueDepthMap).map(([slug, depth]) => {
    const wb = wallboard.find(w => w.slug === slug || w.display_name?.toLowerCase() === slug.toLowerCase());
    return {
      name: wb?.display_name || (slug.charAt(0).toUpperCase() + slug.slice(1)),
      depth,
      max: QUEUE_CAPACITY,
      sla: wb && depth > 0 ? formatWaitShort(wb.longest_wait_seconds) : undefined,
    };
  });

  const aiActiveCalls = activeCalls.filter(c => (c as any).handledBy === 'ai' || (c as any).isAiHandled || c.status === 'ai_active');
  const humanActiveCalls = activeCalls.filter(c => !((c as any).handledBy === 'ai' || (c as any).isAiHandled || c.status === 'ai_active'));
  // In-flight handling only — AI vs Human (two-way). Queue depth is its own
  // tile, so folding "waiting" into a "who's handling" bar would muddy the
  // metric. Ink weight (not the AI hue) carries AI prominence; turquoise stays
  // reserved for the ✦ signal.
  const distributionData = [
    { name: 'AI',    value: aiActiveCalls.length,    cls: 'bg-ink' },
    { name: 'Human', value: humanActiveCalls.length, cls: 'bg-ink-muted' },
  ].filter(d => d.value > 0);
  const inFlight = aiActiveCalls.length + humanActiveCalls.length;
  const distTotal = inFlight || 1;

  // Surface the single call most in need of supervision for the attention bar
  // (most-negative sentiment, then longest-running). Color only marks deviation.
  const attnCall = [...activeCalls]
    .filter(c => (c.sentiment !== undefined && c.sentiment < -0.3) || (c.duration && c.duration > 600))
    .sort((a, b) => (a.sentiment ?? 0) - (b.sentiment ?? 0))[0];
  const attnIsAI = attnCall?.status === 'ai_active';

  // Floor metric tiles — compact Restraint grid. AI is signalled with the ✦
  // glyph; queue depth raises an amber attention dot when callers are waiting.
  const longestWait = wallboard.length ? Math.max(0, ...wallboard.map(w => w.longest_wait_seconds || 0)) : 0;
  const floorTiles: FloorStatTileProps[] = [
    { label: 'Active now', value: activeCalls.length },
    {
      label: 'AI handling',
      value: <span className="text-ai"><span aria-hidden>{AI_GLYPH}</span> {aiActiveCalls.length}</span>,
      subtitle: activeCalls.length ? `${Math.round((aiActiveCalls.length / activeCalls.length) * 100)}% of active` : undefined,
    },
    {
      label: 'Human agents',
      value: humanActiveCalls.length,
      subtitle: activeCalls.length ? `${Math.round((humanActiveCalls.length / activeCalls.length) * 100)}% of active` : undefined,
    },
    {
      label: 'In queue',
      value: queuedCalls.length,
      attention: queuedCalls.length > 0 ? 'warning' : undefined,
      subtitle: queuedCalls.length ? `longest ${formatWaitShort(longestWait)}` : undefined,
    },
  ];
  if (costSummary) {
    floorTiles.push({
      label: "Today's spend",
      value: `$${costSummary.total_estimated.toFixed(2)}`,
      subtitle: 'est. list rates',
    });
  }

  return (
    <div className="relative h-full overflow-y-auto bg-canvas">
      {/* Hero — full-bleed (rs-main 26px gutters); no seam, the floor reads as
          one continuous calm column (spec). */}
      <div className="px-7 pt-6 pb-3">
        <div className="flex items-center justify-between">
          <div>
            <h1 className="text-[20px] font-semibold text-ink tracking-tight leading-none">
              Floor status
            </h1>
            <div className="text-[11px] font-medium text-ink-dim mt-1.5">
              Every AI and human conversation, live.
            </div>
          </div>

          {/* Live pill — quiet, no ping */}
          <div className="flex items-center gap-2 px-3 py-1.5 rounded-full border border-rule-strong">
            <StatusDot status="success" />
            <span className="text-[11.5px] font-medium text-ink">Live</span>
          </div>
        </div>
      </div>

      {/* Floor metric tiles */}
      <div className="px-7 pt-4">
        <FloorStatGrid tiles={floorTiles} columns={floorTiles.length >= 5 ? 5 : 4} />
      </div>

      {/* Service-level wallboard (IMP-18) — SL% against each queue's own
          configured threshold, plus 24h abandon rate and live waits. */}
      {wallboard.length > 0 && (
        <div className="px-7 pt-7">
          <div className="flex items-baseline justify-between mb-3">
            <span className="text-[11px] font-medium text-ink-dim">Service level — last 24h</span>
            <span className="mono text-[11px] text-ink-dim">answered within each queue's SLA target</span>
          </div>
          <div className="grid grid-cols-2 lg:grid-cols-3 gap-2.5">
            {wallboard.map((row) => (
              <QueueSlaCard key={row.slug} row={row} />
            ))}
          </div>
        </div>
      )}

      {/* Cards row — Queue depth bars | Call distribution split (rs-cards 1.25fr 1fr) */}
      <div className="px-7 pt-3 grid grid-cols-[1.25fr_1fr] gap-2.5">
        {/* Queue depth — lightweight bars (rs-qrow), not a chart */}
        <div className="border border-rule rounded-lg bg-canvas-raised px-4 py-3.5">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[13.5px] font-semibold text-ink">Queue depth</h3>
            <span className="text-[11px] font-medium text-ink-dim">SLA threshold {slaThreshold}s</span>
          </div>
          {queueRows.length > 0 ? (
            <div className="mt-1.5">
              {queueRows.map((q) => (
                <QueueDepthRow key={q.name} name={q.name} depth={q.depth} max={q.max} sla={q.sla} />
              ))}
            </div>
          ) : (
            <div className="py-7 text-center text-[13px] text-ink-dim">Queue is clear</div>
          )}
        </div>

        {/* Call distribution — single split bar + legend (rs-distbar) */}
        <div className="border border-rule rounded-lg bg-canvas-raised px-4 py-3.5">
          <div className="flex items-baseline justify-between">
            <h3 className="text-[13.5px] font-semibold text-ink">Call distribution</h3>
            <span className="text-[11px] font-medium text-ink-dim">{inFlight} in flight</span>
          </div>
          {distributionData.length > 0 ? (
            <>
              <div className="flex h-1.5 rounded-full overflow-hidden gap-0.5 mt-4">
                {distributionData.map((d) => (
                  <div key={d.name} className={`${d.cls} rounded-full`} style={{ width: `${(d.value / distTotal) * 100}%` }} />
                ))}
              </div>
              {distributionData.map((d) => (
                <div key={d.name} className="flex items-center gap-2 text-[12.5px] text-ink-muted mt-2.5">
                  <span className={`w-1.5 h-1.5 rounded-full ${d.cls}`} />
                  {d.name}
                  <span className="ml-auto mono text-[11px] text-ink">
                    {d.value} · {Math.round((d.value / distTotal) * 100)}%
                  </span>
                </div>
              ))}
            </>
          ) : (
            <div className="py-7 text-center text-[13px] text-ink-dim">No calls in flight</div>
          )}
        </div>
      </div>

      {/* Attention bar — the single call most in need of supervision. Real
          actions only: Listen (ObserverControls) + a fuchsia "Open call" that
          drops the supervisor into the call detail (where whisper / take-over
          live). Whisper/Barge buttons are intentionally NOT faked here. */}
      {attnCall && (
        <div className="px-7 pt-2.5 pb-7">
          <AttentionBar
            status="warning"
            label="Needs attention"
            meta={
              attnCall.sentiment !== undefined
                ? `sentiment ${attnCall.sentiment > 0 ? '+' : ''}${attnCall.sentiment.toFixed(1)}`
                : 'long-running call'
            }
            phone={attnCall.from_number || attnCall.contact?.displayName || 'Unknown'}
            chips={[
              ...(attnCall.queueId || (attnCall as any).queue_id
                ? [{ label: String(attnCall.queueId || (attnCall as any).queue_id) }]
                : []),
              ...(attnIsAI && attnCall.ai_agent_name ? [{ label: attnCall.ai_agent_name, ai: true }] : []),
            ]}
            timestamp={`${formatWaitShort(attnCall.duration || 0)}${attnIsAI && attnCall.ai_agent_name ? ` · ${attnCall.ai_agent_name}` : ''}`}
            preview={attnCall.ai_summary || undefined}
            actions={
              <>
                {onSelectCall && (
                  <Button variant="primary" onClick={() => onSelectCall(attnCall)}>Open call</Button>
                )}
                {(!Array.isArray(attnCall.capabilities) || attnCall.capabilities.includes('monitor_listen')) && (
                  <ObserverControls callId={attnCall.id} callType={attnIsAI ? 'ai' : 'human'} compact />
                )}
              </>
            }
          />
        </div>
      )}
    </div>
  );
}

function QueueSlaCard({ row }: { row: WallboardRow }) {
  const sl = row.service_level;
  // SLA health drives a single status dot; the value stays neutral mono.
  const slStatus: RestraintStatus =
    sl == null ? 'neutral' :
    sl >= 90   ? 'success' :
    sl >= 70   ? 'warning' :
    'error';
  return (
    <div className="border border-rule rounded-lg bg-canvas-raised p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-[13px] font-medium text-ink">{row.display_name}</span>
        <span className="mono text-[10px] text-ink-dim uppercase">SL ≤ {row.sla_threshold_seconds}s</span>
      </div>
      <div className="flex items-center gap-2 mb-3">
        <StatusDot status={slStatus} />
        <span className="mono text-2xl font-semibold leading-none tabular-nums text-ink">
          {sl == null ? '—' : `${sl.toFixed(0)}%`}
        </span>
        <span className="mono text-[10px] text-ink-dim uppercase tracking-wider">
          {sl == null ? 'no data' : 'service level'}
        </span>
      </div>
      <div className="flex items-center gap-4 mono text-[11px] text-ink-muted">
        <span title="Waiting right now">{row.depth} waiting</span>
        <span title="Longest current wait">
          {row.depth > 0 ? formatWaitShort(row.longest_wait_seconds) : '0s'} longest
        </span>
        <span
          title="Abandon rate over the last 24h"
          className={row.abandon_rate != null && row.abandon_rate > 10 ? 'text-status-error' : ''}
        >
          {row.abandon_rate == null ? '—' : `${row.abandon_rate.toFixed(0)}%`} abandon
        </span>
        <span title="Answered / offered over the last 24h">
          {row.answered_24h}/{row.offered_24h}
        </span>
      </div>
    </div>
  );
}

export default DashboardCharts;
