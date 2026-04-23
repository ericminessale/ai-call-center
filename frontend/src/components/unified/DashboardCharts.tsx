import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';
import { Activity, Bot, Users, Phone } from 'lucide-react';
import { Call } from '../../types/callcenter';

interface DashboardChartsProps {
  activeCalls: Call[];
  queuedCalls: Call[];
}

const PALETTE = {
  signal: '#F72A72',
  live:   '#3FB77E',
  ai:     '#8A7BFF',
  wait:   '#E8A838',
  urgent: '#F0516E',
  info:   '#4DBCFF',
};

const DIST_COLORS = [PALETTE.live, PALETTE.ai, PALETTE.wait, PALETTE.urgent, PALETTE.info, PALETTE.signal];

export function DashboardCharts({ activeCalls, queuedCalls }: DashboardChartsProps) {
  const queueDepthMap: Record<string, number> = {};
  queuedCalls.forEach(c => {
    const q = c.queueId || (c as any).queue_id || 'unknown';
    queueDepthMap[q] = (queueDepthMap[q] || 0) + 1;
  });
  const queueDepthData = Object.entries(queueDepthMap).map(([name, count]) => ({
    name: name.charAt(0).toUpperCase() + name.slice(1),
    waiting: count,
  }));

  const aiActiveCalls = activeCalls.filter(c => (c as any).handledBy === 'ai' || (c as any).isAiHandled || c.status === 'ai_active');
  const humanActiveCalls = activeCalls.filter(c => !((c as any).handledBy === 'ai' || (c as any).isAiHandled || c.status === 'ai_active'));
  const distributionData = [
    { name: 'Human', value: humanActiveCalls.length, color: PALETTE.live },
    { name: 'AI',    value: aiActiveCalls.length,    color: PALETTE.ai },
    { name: 'Queue', value: queuedCalls.length,      color: PALETTE.wait },
  ].filter(d => d.value > 0);

  const totalCalls = activeCalls.length + queuedCalls.length;

  return (
    <div className="relative h-full overflow-y-auto bg-dotgrid">
      {/* Hero header */}
      <div className="px-10 pt-10 pb-8 border-b border-rule/60">
        <div className="flex items-start justify-between max-w-5xl mx-auto">
          <div>
            <div className="kicker mb-3">Control room</div>
            <h1 className="font-display text-[48px] leading-[1] text-ink tracking-tightest mb-2">
              Floor status
            </h1>
            <p className="text-[13.5px] text-ink-muted max-w-md">
              Real-time view of every AI and human conversation moving through the fabric.
            </p>
          </div>

          {/* Pulse indicator */}
          <div className="flex items-center gap-2 px-3 py-1.5 rounded border border-live/30 bg-live/5">
            <span className="relative flex h-2 w-2">
              <span className="absolute inline-flex h-full w-full rounded-full bg-live opacity-60 animate-ping" />
              <span className="relative inline-flex rounded-full h-2 w-2 bg-live" />
            </span>
            <span className="mono text-[11px] text-live-soft uppercase tracking-wider">Live</span>
          </div>
        </div>
      </div>

      {/* Hero stats row — inline, typography rhythm, no grid cells */}
      <div className="max-w-5xl mx-auto px-10 pt-8">
        <div className="flex items-baseline gap-12 flex-wrap">
          <BigStat kicker="Active now" value={activeCalls.length} icon={<Activity className="w-3.5 h-3.5" />} />
          <BigStat kicker="AI handling" value={aiActiveCalls.length} tone="ai" icon={<Bot className="w-3.5 h-3.5" />} />
          <BigStat kicker="Human agents" value={humanActiveCalls.length} tone="live" icon={<Users className="w-3.5 h-3.5" />} />
          <BigStat kicker="In queue" value={queuedCalls.length} tone={queuedCalls.length > 0 ? 'wait' : 'default'} icon={<Phone className="w-3.5 h-3.5" />} />
        </div>
      </div>

      {/* Charts row */}
      <div className="max-w-5xl mx-auto px-10 py-8 grid grid-cols-2 gap-5">
        {/* Queue depth */}
        <div className="panel rounded-md p-5">
          <div className="flex items-center justify-between mb-1">
            <span className="kicker">Queue depth</span>
            <span className="mono text-[11px] text-ink-dim">by queue</span>
          </div>
          <h3 className="font-display text-[22px] text-ink leading-none mb-4">Who is waiting.</h3>
          {queueDepthData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={queueDepthData} margin={{ top: 8, right: 8, left: -10, bottom: 0 }}>
                <CartesianGrid strokeDasharray="2 4" stroke="#24272E" vertical={false} />
                <XAxis
                  dataKey="name"
                  tick={{ fill: '#A3A099', fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }}
                  tickLine={false}
                  axisLine={{ stroke: '#24272E' }}
                />
                <YAxis
                  allowDecimals={false}
                  tick={{ fill: '#76736D', fontSize: 11, fontFamily: 'JetBrains Mono, monospace' }}
                  tickLine={false}
                  axisLine={false}
                  width={28}
                />
                <Tooltip
                  contentStyle={{
                    backgroundColor: '#14171C',
                    border: '1px solid #32363F',
                    borderRadius: '4px',
                    fontFamily: 'JetBrains Mono, monospace',
                    fontSize: '11px',
                    padding: '6px 10px',
                  }}
                  labelStyle={{ color: '#E8E5DE', fontWeight: 500 }}
                  cursor={{ fill: 'rgba(247, 42, 114, 0.08)' }}
                />
                <Bar dataKey="waiting" fill={PALETTE.signal} radius={[2, 2, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <EmptyChart message="Queue is clear" />
          )}
        </div>

        {/* Distribution */}
        <div className="panel rounded-md p-5">
          <div className="flex items-center justify-between mb-1">
            <span className="kicker">Call distribution</span>
            <span className="mono text-[11px] text-ink-dim">{totalCalls} total</span>
          </div>
          <h3 className="font-display text-[22px] text-ink leading-none mb-4">Human or AI?</h3>
          {totalCalls > 0 ? (
            <>
              <ResponsiveContainer width="100%" height={180}>
                <PieChart>
                  <Pie
                    data={distributionData}
                    cx="50%"
                    cy="50%"
                    innerRadius={52}
                    outerRadius={78}
                    paddingAngle={3}
                    dataKey="value"
                    stroke="#14171C"
                    strokeWidth={2}
                  >
                    {distributionData.map((d, i) => (
                      <Cell key={i} fill={d.color} />
                    ))}
                  </Pie>
                  <Tooltip
                    contentStyle={{
                      backgroundColor: '#14171C',
                      border: '1px solid #32363F',
                      borderRadius: '4px',
                      fontFamily: 'JetBrains Mono, monospace',
                      fontSize: '11px',
                      padding: '6px 10px',
                    }}
                    labelStyle={{ color: '#E8E5DE' }}
                  />
                </PieChart>
              </ResponsiveContainer>
              <div className="flex justify-center gap-4 mt-3">
                {distributionData.map((d) => (
                  <div key={d.name} className="flex items-center gap-1.5">
                    <span className="w-2 h-2 rounded-sm" style={{ background: d.color }} />
                    <span className="mono text-[11px] text-ink-muted uppercase tracking-wider">
                      {d.name} <span className="text-ink">{d.value}</span>
                    </span>
                  </div>
                ))}
              </div>
            </>
          ) : (
            <EmptyChart message="No calls in flight" />
          )}
        </div>
      </div>

      {/* Footer marker */}
      <div className="max-w-5xl mx-auto px-10 pb-8">
        <div className="rule-h mb-4" />
        <div className="flex items-center justify-between text-[11px] text-ink-dim mono uppercase tracking-wider">
          <span>signalwire / call fabric</span>
          <span>v1.0</span>
        </div>
      </div>
    </div>
  );
}

function BigStat({
  kicker,
  value,
  tone = 'default',
  icon,
}: {
  kicker: string;
  value: number;
  tone?: 'default' | 'live' | 'ai' | 'wait';
  icon?: React.ReactNode;
}) {
  const color =
    tone === 'live' ? 'text-live-soft' :
    tone === 'ai'   ? 'text-ai-soft'   :
    tone === 'wait' ? 'text-wait-soft' :
    'text-ink';
  return (
    <div className="flex flex-col gap-1.5 items-start">
      <div className="flex items-center gap-1.5 text-ink-dim">
        {icon}
        <span className="kicker">{kicker}</span>
      </div>
      <span className={`font-heading font-semibold text-[40px] leading-none tabular-nums ${color}`}>{value}</span>
    </div>
  );
}

function EmptyChart({ message }: { message: string }) {
  return (
    <div className="h-[180px] flex items-center justify-center">
      <p className="font-display text-[18px] text-ink-dim">{message}</p>
    </div>
  );
}

export default DashboardCharts;
