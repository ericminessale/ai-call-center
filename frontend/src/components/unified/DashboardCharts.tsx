import { BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, PieChart, Pie, Cell, ResponsiveContainer } from 'recharts';
import { Call } from '../../types/callcenter';

interface DashboardChartsProps {
  activeCalls: Call[];
  queuedCalls: Call[];
}

const COLORS = ['#3b82f6', '#10b981', '#f59e0b', '#ef4444', '#8b5cf6', '#06b6d4'];

export function DashboardCharts({ activeCalls, queuedCalls }: DashboardChartsProps) {
  // Queue depth by queue
  const queueDepthMap: Record<string, number> = {};
  queuedCalls.forEach(c => {
    const q = c.queueId || (c as any).queue_id || 'unknown';
    queueDepthMap[q] = (queueDepthMap[q] || 0) + 1;
  });
  const queueDepthData = Object.entries(queueDepthMap).map(([name, count]) => ({
    name: name.charAt(0).toUpperCase() + name.slice(1),
    waiting: count,
  }));

  // Call distribution donut
  const aiActiveCalls = activeCalls.filter(c => (c as any).handledBy === 'ai' || (c as any).isAiHandled);
  const humanActiveCalls = activeCalls.filter(c => !((c as any).handledBy === 'ai' || (c as any).isAiHandled));
  const distributionData = [
    { name: 'Human Active', value: humanActiveCalls.length },
    { name: 'AI Active', value: aiActiveCalls.length },
    { name: 'Waiting', value: queuedCalls.length },
  ].filter(d => d.value > 0);

  const totalCalls = activeCalls.length + queuedCalls.length;

  return (
    <div className="h-full flex flex-col items-center justify-center p-8">
      <h2 className="text-xl font-semibold text-white mb-8">Call Center Overview</h2>

      <div className="grid grid-cols-2 gap-8 w-full max-w-3xl">
        {/* Queue Depth Chart */}
        <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-4">Queue Depth</h3>
          {queueDepthData.length > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <BarChart data={queueDepthData}>
                <CartesianGrid strokeDasharray="3 3" stroke="#374151" />
                <XAxis dataKey="name" tick={{ fill: '#9ca3af', fontSize: 12 }} />
                <YAxis allowDecimals={false} tick={{ fill: '#9ca3af', fontSize: 12 }} />
                <Tooltip
                  contentStyle={{ backgroundColor: '#1f2937', border: '1px solid #374151', borderRadius: '8px' }}
                  labelStyle={{ color: '#f3f4f6' }}
                />
                <Bar dataKey="waiting" fill="#3b82f6" radius={[4, 4, 0, 0]} />
              </BarChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[200px] flex items-center justify-center text-gray-500 text-sm">
              No calls in queue
            </div>
          )}
        </div>

        {/* Call Distribution Donut */}
        <div className="bg-gray-800 rounded-lg border border-gray-700 p-4">
          <h3 className="text-sm font-medium text-gray-400 mb-4">Call Distribution</h3>
          {totalCalls > 0 ? (
            <ResponsiveContainer width="100%" height={200}>
              <PieChart>
                <Pie
                  data={distributionData}
                  cx="50%"
                  cy="50%"
                  innerRadius={50}
                  outerRadius={80}
                  paddingAngle={2}
                  dataKey="value"
                >
                  {distributionData.map((_, i) => (
                    <Cell key={i} fill={COLORS[i % COLORS.length]} />
                  ))}
                </Pie>
                <Tooltip
                  contentStyle={{ backgroundColor: '#1f2937', border: '1px solid #374151', borderRadius: '8px' }}
                />
              </PieChart>
            </ResponsiveContainer>
          ) : (
            <div className="h-[200px] flex items-center justify-center text-gray-500 text-sm">
              No active calls
            </div>
          )}
          {totalCalls > 0 && (
            <div className="flex justify-center gap-4 mt-2">
              {distributionData.map((d, i) => (
                <div key={d.name} className="flex items-center gap-1.5 text-xs text-gray-400">
                  <div className="w-2.5 h-2.5 rounded-full" style={{ backgroundColor: COLORS[i % COLORS.length] }} />
                  {d.name} ({d.value})
                </div>
              ))}
            </div>
          )}
        </div>
      </div>

      {/* Summary stats */}
      <div className="flex gap-6 mt-8">
        <div className="bg-gray-800 rounded-lg border border-gray-700 px-6 py-3 text-center">
          <div className="text-2xl font-bold text-white">{activeCalls.length}</div>
          <div className="text-xs text-gray-400">Active Calls</div>
        </div>
        <div className="bg-gray-800 rounded-lg border border-gray-700 px-6 py-3 text-center">
          <div className="text-2xl font-bold text-blue-400">{queuedCalls.length}</div>
          <div className="text-xs text-gray-400">In Queue</div>
        </div>
        <div className="bg-gray-800 rounded-lg border border-gray-700 px-6 py-3 text-center">
          <div className="text-2xl font-bold text-green-400">{totalCalls}</div>
          <div className="text-xs text-gray-400">Total</div>
        </div>
      </div>
    </div>
  );
}
