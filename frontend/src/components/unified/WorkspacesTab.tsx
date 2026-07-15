import { useCallback, useEffect, useRef, useState } from 'react';
import {
  Building2,
  Clock,
  Eye,
  EyeOff,
  Loader2,
  Phone,
  RefreshCw,
  Trash2,
  Users,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { adminApi, DemoStats, WorkspaceRosterRow } from '../../services/api';
import websocket from '../../services/websocket';

/**
 * Platform operator's workspace roster (Phase 5 operator view).
 *
 * Rendered only for platform-level admins (workspace_id null) in hosted
 * demo mode — the SettingsPanel gates the tab. Shows every visitor
 * workspace with lifecycle, verification, and usage numbers, plus:
 *
 *   - Watch: joins this operator's socket to the workspace's ``ws:{id}``
 *     room (server-verified platform-only), streaming its realtime events
 *     into the feed below — the cross-workspace drill-down that Phase 3
 *     deviation 24 deferred here. One workspace at a time.
 *   - Reap: runs the same GC path as the hourly job for one workspace,
 *     for retiring an abusive or stuck workspace before its TTL.
 *
 * Data is poll-refreshed; MAX_WORKSPACES (~200) keeps a single
 * unpaginated response cheap.
 */

// Realtime events worth showing in the watch feed, with a one-line
// summary each. Anything not listed is ignored (call_update alone can be
// chatty enough).
const FEED_EVENTS: Record<string, (d: any) => string> = {
  // Two shapes: the 5s wallboard broadcast (array of per-queue stats) and
  // per-call add/remove events ({call, queue_id, action}). The formatter
  // may return null to drop a line (see the wallboard filter below).
  queue_update: (d) => {
    if (Array.isArray(d)) {
      const waiting = d.reduce((acc: number, q: any) => acc + (q?.waiting || 0), 0);
      return `wallboard — ${d.length} queues, ${waiting} waiting`;
    }
    return `queue ${d?.queue_id ?? ''} — call ${d?.action ?? 'update'}`;
  },
  call_update: (d) => `call_update — ${d?.call?.status ?? d?.status ?? ''} ${d?.call?.call_sid ?? d?.call_sid ?? ''}`,
  call_ended: (d) => `call_ended — ${d?.call_sid ?? ''}`,
  queue_config_changed: () => 'queue_config_changed',
  demo_phone_verified: (d) => `phone verified ${d?.masked_number ?? ''}`,
  agent_online_status: (d) => `agent ${d?.user_id ?? ''} ${d?.status ?? d?.online_status ?? ''}`,
  sentiment_update: (d) => `sentiment ${d?.sentiment ?? ''} (${d?.call_sid ?? ''})`,
  callback_event: (d) => `callback ${d?.event ?? ''}`,
};

interface FeedLine {
  at: string;
  text: string;
}

function fmtWhen(iso: string | null): string {
  if (!iso) return '—';
  // Backend emits naive-UTC isoformat (no 'Z'/offset); parse as UTC so the
  // operator's local rendering isn't skewed by their browser offset.
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  const d = new Date(hasTz ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString(undefined, {
    month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
  });
}

function sumSeries(series?: { date: string; count: number }[]): number {
  return (series ?? []).reduce((acc, p) => acc + (p.count || 0), 0);
}

function StatCard({ label, value, hint }: { label: string; value: string | number; hint?: string }) {
  return (
    <div className="rounded border border-rule bg-canvas-sunken px-4 py-3" title={hint}>
      <div className="text-[10.5px] uppercase tracking-[0.14em] text-ink-faint">{label}</div>
      <div className="mt-1 font-mono text-[20px] text-ink leading-none">{value}</div>
    </div>
  );
}

export function WorkspacesTab() {
  const [rows, setRows] = useState<WorkspaceRosterRow[]>([]);
  const [stats, setStats] = useState<DemoStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [reaping, setReaping] = useState<string | null>(null);
  const [confirmReap, setConfirmReap] = useState<string | null>(null);
  const [watching, setWatching] = useState<string | null>(null);
  const [feed, setFeed] = useState<FeedLine[]>([]);
  const watchingRef = useRef<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ws, st] = await Promise.all([adminApi.listWorkspaces(), adminApi.demoStats()]);
      setRows(ws.data.workspaces);
      setStats(st.data);
    } catch {
      toast.error('Could not load the workspace roster.');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const interval = window.setInterval(() => void load(), 15_000);
    return () => window.clearInterval(interval);
  }, [load]);

  // Watch-feed listeners. Events arrive because the socket joined the
  // watched workspace's room server-side; they carry no workspace id, so
  // the UI allows watching ONE workspace at a time to keep attribution
  // unambiguous.
  useEffect(() => {
    const handlers = Object.entries(FEED_EVENTS).map(([event, fmt]) => {
      const handler = (data: any) => {
        if (!watchingRef.current) return;
        // The operator's socket also sits in their HOME workspace room, so
        // its wallboard stream arrives too. Wallboard payloads carry the
        // workspace public id per queue — drop ticks that aren't the
        // watched workspace (and empty arrays, which carry no info).
        if (event === 'queue_update' && Array.isArray(data)) {
          if (data.length === 0) return;
          const from = data[0]?.workspace;
          if (from && from !== watchingRef.current) return;
        }
        setFeed((prev) => {
          const line = { at: new Date().toLocaleTimeString(), text: fmt(data) };
          return [line, ...prev].slice(0, 50);
        });
      };
      websocket.on(event, handler);
      return { event, handler };
    });
    return () => {
      handlers.forEach(({ event, handler }) => websocket.off(event, handler));
    };
  }, []);

  // Socket.io room membership is per-sid and does NOT survive a reconnect
  // (network blip, backend redeploy). Re-emit watch on every (re)connect so
  // the feed keeps flowing while the UI still says "Watching".
  useEffect(() => {
    const onConnect = () => {
      if (watchingRef.current) {
        websocket.emit('watch_workspace', {
          workspace_id: watchingRef.current,
          token: localStorage.getItem('access_token'),
        });
      }
    };
    websocket.on('connect', onConnect);
    return () => websocket.off('connect', onConnect);
  }, []);

  // Leave the watched room when the tab unmounts.
  useEffect(() => {
    return () => {
      if (watchingRef.current) {
        websocket.emit('unwatch_workspace', {
          workspace_id: watchingRef.current,
          token: localStorage.getItem('access_token'),
        });
      }
    };
  }, []);

  const toggleWatch = (publicId: string) => {
    const token = localStorage.getItem('access_token');
    if (watching === publicId) {
      websocket.emit('unwatch_workspace', { workspace_id: publicId, token });
      watchingRef.current = null;
      setWatching(null);
      return;
    }
    if (watching) {
      websocket.emit('unwatch_workspace', { workspace_id: watching, token });
    }
    websocket.emit('watch_workspace', { workspace_id: publicId, token });
    watchingRef.current = publicId;
    setWatching(publicId);
    setFeed([]);
  };

  const doReap = async (publicId: string) => {
    setReaping(publicId);
    setConfirmReap(null);
    try {
      await adminApi.reapWorkspace(publicId);
      toast.success('Workspace reaped.');
      if (watchingRef.current === publicId) {
        watchingRef.current = null;
        setWatching(null);
      }
      await load();
    } catch (err: any) {
      toast.error(err?.response?.data?.error || 'Reap failed.');
    } finally {
      setReaping(null);
    }
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-ink-muted text-[13px]">
        <Loader2 className="h-4 w-4 animate-spin" /> Loading workspaces…
      </div>
    );
  }

  const wsStats = stats?.workspaces;

  return (
    <div className="max-w-5xl space-y-6">
      {/* Telemetry header */}
      <div>
        <h2 className="text-[15px] font-semibold text-ink flex items-center gap-2">
          <Building2 className="h-4 w-4 text-ai-soft" />
          Workspaces
        </h2>
        <p className="text-[12px] text-ink-muted mt-1">
          Every visitor workspace on this install — platform operators only.
        </p>
      </div>

      {stats && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-6 gap-3">
          <StatCard label="Active" value={wsStats?.active ?? 0} />
          <StatCard
            label="Verified"
            value={`${wsStats?.verified ?? 0} (${wsStats?.verified_pct ?? 0}%)`}
            hint="Active workspaces with a verified phone number"
          />
          <StatCard
            label="Created / 7d"
            value={sumSeries(wsStats?.created_by_day)}
            hint="New workspaces provisioned in the last 7 days"
          />
          <StatCard
            label="Reaped / 7d"
            value={sumSeries(wsStats?.reaped_by_day)}
            hint="Workspaces garbage-collected in the last 7 days"
          />
          <StatCard
            label="Seats leased"
            value={`${stats.active_leases}/${stats.pool_size}`}
            hint="WebRTC subscriber seats currently held / pool size"
          />
          <StatCard
            label="Rejected calls"
            value={stats.inbound_rejected?.total ?? 0}
            hint="Inbound calls rejected because the caller's number has no live workspace (all-time)"
          />
        </div>
      )}

      {/* Roster */}
      <div className="rounded border border-rule overflow-hidden">
        <table className="w-full text-[12px]">
          <thead>
            <tr className="bg-canvas-sunken text-left text-[10.5px] uppercase tracking-[0.12em] text-ink-faint">
              <th className="px-3 py-2 font-medium">Workspace</th>
              <th className="px-3 py-2 font-medium">Status</th>
              <th className="px-3 py-2 font-medium">Verified</th>
              <th className="px-3 py-2 font-medium text-right">Online</th>
              <th className="px-3 py-2 font-medium text-right">Users</th>
              <th className="px-3 py-2 font-medium text-right">Queues</th>
              <th className="px-3 py-2 font-medium text-right">Calls</th>
              <th className="px-3 py-2 font-medium">Last active</th>
              <th className="px-3 py-2 font-medium">Expires</th>
              <th className="px-3 py-2 font-medium text-right">Actions</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((ws) => (
              <tr key={ws.id} className="border-t border-rule">
                <td className="px-3 py-2">
                  <div className="text-ink font-medium truncate max-w-[180px]" title={ws.id}>
                    {ws.name}
                    {ws.is_template && (
                      <span className="ml-1.5 text-[10px] uppercase tracking-wide text-ink-faint">
                        template
                      </span>
                    )}
                  </div>
                  <div className="font-mono text-[10px] text-ink-faint truncate max-w-[180px]">
                    {ws.id}
                  </div>
                </td>
                <td className="px-3 py-2">
                  <span className="inline-flex items-center gap-1.5">
                    <span
                      className={`w-1.5 h-1.5 rounded-full ${
                        ws.status === 'active' ? 'bg-status-success' : 'bg-status-error'
                      }`}
                    />
                    <span className="text-ink-muted">{ws.status}</span>
                  </span>
                </td>
                <td className="px-3 py-2 font-mono text-ink-muted">
                  {ws.verified_number ? (
                    <span className="inline-flex items-center gap-1">
                      <Phone className="h-3 w-3 text-ai-soft" />
                      {ws.verified_number}
                    </span>
                  ) : (
                    <span className="text-ink-faint">—</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right font-mono text-ink-muted">
                  {ws.connected_clients > 0 ? (
                    <span className="inline-flex items-center gap-1">
                      <Users className="h-3 w-3 text-status-success" />
                      {ws.connected_clients}
                    </span>
                  ) : (
                    <span className="text-ink-faint">0</span>
                  )}
                </td>
                <td className="px-3 py-2 text-right font-mono text-ink-muted">{ws.users}</td>
                <td className="px-3 py-2 text-right font-mono text-ink-muted">{ws.queues}</td>
                <td className="px-3 py-2 text-right font-mono text-ink-muted">{ws.calls}</td>
                <td className="px-3 py-2 text-ink-muted whitespace-nowrap">
                  {fmtWhen(ws.last_active_at)}
                </td>
                <td className="px-3 py-2 text-ink-muted whitespace-nowrap">
                  <span className="inline-flex items-center gap-1">
                    {ws.expires_at && <Clock className="h-3 w-3 text-ink-faint" />}
                    {ws.expires_at ? fmtWhen(ws.expires_at) : 'never'}
                  </span>
                </td>
                <td className="px-3 py-2">
                  <div className="flex items-center justify-end gap-1.5">
                    {!ws.is_template && (
                      <>
                        <button
                          type="button"
                          onClick={() => toggleWatch(ws.id)}
                          className={`inline-flex items-center gap-1 rounded border px-2 py-1 transition-colors ${
                            watching === ws.id
                              ? 'border-ai text-ai'
                              : 'border-rule text-ink-muted hover:text-ink hover:border-ink-faint'
                          }`}
                          title={
                            watching === ws.id
                              ? 'Stop streaming this workspace\'s realtime events'
                              : 'Stream this workspace\'s realtime events into the feed below'
                          }
                        >
                          {watching === ws.id ? (
                            <EyeOff className="h-3 w-3" />
                          ) : (
                            <Eye className="h-3 w-3" />
                          )}
                          {watching === ws.id ? 'Watching' : 'Watch'}
                        </button>
                        {confirmReap === ws.id ? (
                          <span className="inline-flex items-center gap-1">
                            <button
                              type="button"
                              onClick={() => void doReap(ws.id)}
                              disabled={reaping === ws.id}
                              className="rounded border border-status-error/60 px-2 py-1 text-status-error hover:bg-status-error/10 transition-colors disabled:opacity-50"
                            >
                              {reaping === ws.id ? 'Reaping…' : 'Confirm'}
                            </button>
                            <button
                              type="button"
                              onClick={() => setConfirmReap(null)}
                              className="rounded border border-rule px-2 py-1 text-ink-muted hover:text-ink transition-colors"
                            >
                              Cancel
                            </button>
                          </span>
                        ) : (
                          <button
                            type="button"
                            onClick={() => setConfirmReap(ws.id)}
                            disabled={reaping !== null}
                            className="inline-flex items-center gap-1 rounded border border-rule px-2 py-1 text-ink-muted hover:text-status-error hover:border-status-error/60 transition-colors disabled:opacity-50"
                            title="Delete this workspace now — same cleanup the hourly GC runs (rows, Redis state, verify binding, seat)"
                          >
                            <Trash2 className="h-3 w-3" />
                            Reap
                          </button>
                        )}
                      </>
                    )}
                  </div>
                </td>
              </tr>
            ))}
            {rows.length === 0 && (
              <tr>
                <td colSpan={10} className="px-3 py-6 text-center text-ink-faint">
                  No workspaces yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      <div className="flex items-center gap-2 text-[11px] text-ink-faint">
        <RefreshCw className="h-3 w-3" />
        Refreshes every 15s.
      </div>

      {/* Watch feed */}
      {watching && (
        <div className="rounded border border-rule">
          <div className="flex items-center justify-between border-b border-rule bg-canvas-sunken px-3 py-2">
            <span className="text-[11px] uppercase tracking-[0.12em] text-ink-faint">
              Live events — {rows.find((r) => r.id === watching)?.name ?? watching}
            </span>
            <button
              type="button"
              onClick={() => toggleWatch(watching)}
              className="text-[11px] text-ink-muted hover:text-ink transition-colors"
            >
              Stop watching
            </button>
          </div>
          <div className="max-h-64 overflow-y-auto px-3 py-2 font-mono text-[11px] leading-relaxed">
            {feed.length === 0 ? (
              <div className="text-ink-faint">Waiting for events…</div>
            ) : (
              feed.map((line, i) => (
                <div key={`${line.at}-${i}`} className="text-ink-muted">
                  <span className="text-ink-faint">{line.at}</span> {line.text}
                </div>
              ))
            )}
          </div>
        </div>
      )}
    </div>
  );
}
