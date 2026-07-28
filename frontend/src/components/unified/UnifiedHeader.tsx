import { useState, useEffect, useCallback, useRef } from 'react';
import {
  Users,
  Phone,
  ListTodo,
  Eye,
  LogOut,
  ChevronDown,
  PhoneCall,
  Settings,
  AlertTriangle,
  Sparkles,
  X as XIcon,
  PhoneOutgoing,
  RotateCcw,
} from 'lucide-react';
import { ViewMode, AgentStatus } from '../../pages/UnifiedAgentDesktop';
import { BrandMark, useBrand } from '../shared/Brand';
import { QuickDialDropdown } from './QuickDialDropdown';
import { queueApi } from '../../services/api';
import toast from 'react-hot-toast';
import { DemoTip, useDemoTip } from '../shared/DemoTip';
import { useSocketContext } from '../../contexts/SocketContext';
import { useCallFabricContext } from '../../contexts/CallFabricContext';
import { Checkbox, Chip } from '../restraint';
import { isAdminSurface, isSupervisory } from '../../lib/roles';

interface UnifiedHeaderProps {
  user: { email: string; role?: string } | null;
  agentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  stats: {
    callsToday: number;
    avgHandleTime: number;
    queueDepth: number;
    longestWait: number;
  };
  viewMode: ViewMode;
  onViewModeChange: (mode: ViewMode) => void;
  callCounts: {
    active: number;
    queue: number;
    aiActive: number;
  };
  /** Pending callbacks count — drives the badge on the Callbacks tab (Tier 2r). */
  callbacksPending?: number;
  onLogout: () => void;
  callFabric: any;
  onOutboundCallStarted?: (phoneNumber: string) => void;
}

interface AvailableQueue {
  id: number;
  slug: string;
  display_name: string;
  routing_strategy: string;
  assignment_id: number | null;
  is_assigned: boolean;
  is_activated: boolean;
}

type StatusMeta = {
  label: string;
  dotClass: string;
  textClass: string;
};

const statusMeta: Record<AgentStatus, StatusMeta> = {
  available:    { label: 'Available',  dotClass: 'dot dot-live',   textClass: 'text-live-soft' },
  busy:         { label: 'Busy',       dotClass: 'dot dot-urgent', textClass: 'text-urgent-soft' },
  'after-call': { label: 'After Call', dotClass: 'dot dot-wait',   textClass: 'text-wait-soft' },
  break:        { label: 'Break',      dotClass: 'dot dot-wait',   textClass: 'text-wait-soft' },
  offline:      { label: 'Offline',    dotClass: 'dot dot-offline', textClass: 'text-ink-dim' },
};

const STRATEGY_SHORT: Record<string, string> = {
  fifo: 'FIFO',
  round_robin: 'RR',
  priority: 'PRI',
  skill_based: 'SKL',
};

export function UnifiedHeader({
  user,
  agentStatus,
  onStatusChange,
  stats,
  viewMode,
  onViewModeChange,
  callbacksPending = 0,
  callCounts,
  onLogout,
  callFabric,
  onOutboundCallStarted,
}: UnifiedHeaderProps) {
  const [showStatusDropdown, setShowStatusDropdown] = useState(false);
  const [showUserMenu, setShowUserMenu] = useState(false);
  const [showQuickDial, setShowQuickDial] = useState(false);
  const statusRef = useRef<HTMLDivElement>(null);
  const userRef = useRef<HTMLDivElement>(null);

  // Demo-only nudge inside the queue dropdown — tells visitors what
  // the checkboxes do. Dismissal is sticky via localStorage; the
  // hook returns shouldShow=false outside demo mode entirely.
  const queueTip = useDemoTip('demo-queue-checkboxes');

  const [availableQueues, setAvailableQueues] = useState<AvailableQueue[]>([]);

  const loadQueues = useCallback(async () => {
    try {
      const resp = await queueApi.getAvailableQueues();
      setAvailableQueues(resp.data.queues);
    } catch {
      setAvailableQueues([]);
    }
  }, []);

  useEffect(() => { loadQueues(); }, [loadQueues]);

  // Close dropdowns on outside click
  useEffect(() => {
    const onClick = (e: MouseEvent) => {
      if (statusRef.current && !statusRef.current.contains(e.target as Node)) setShowStatusDropdown(false);
      if (userRef.current && !userRef.current.contains(e.target as Node)) setShowUserMenu(false);
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, []);

  const handleQueueToggle = async (queue: AvailableQueue) => {
    try {
      if (!queue.is_assigned) {
        const resp = await queueApi.selfSubscribe(queue.id);
        setAvailableQueues(prev => prev.map(q =>
          q.id === queue.id
            ? { ...q, assignment_id: resp.data.assignment.id, is_assigned: true, is_activated: true }
            : q
        ));
      } else {
        const resp = await queueApi.toggleQueueActivation(queue.assignment_id!, !queue.is_activated);
        setAvailableQueues(prev => prev.map(q =>
          q.id === queue.id ? { ...q, is_activated: resp.data.assignment.is_activated } : q
        ));
      }
    } catch {
      toast.error('Failed to update queue');
    }
  };

  const current = statusMeta[agentStatus];
  // ACW countdown — shown inside the status pill while the auto-entered
  // after-call window ticks down (auto-returns to Available at zero).
  const { acwSecondsLeft } = useCallFabricContext();
  const activeQueueCount = availableQueues.filter(q => q.is_activated).length;
  const formatMMSS = (secs: number) =>
    `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;

  // FE-04 (2026-06-02 audit): surface ACTIONABLE socket state only. The
  // persistent "Offline" pill was removed — it duplicated the agent-status
  // pill and read as stuck (socket state ≠ agent presence). We still show the
  // transient "Reconnecting" spinner, which is a distinct, useful signal.
  const { connectionStatus } = useSocketContext();
  const socketBanner: { label: string; tone: 'wait' | 'urgent'; spin: boolean } | null = (() => {
    if (connectionStatus === 'reconnecting') {
      return { label: 'Reconnecting', tone: 'wait', spin: true };
    }
    return null;
  })();

  const { productName } = useBrand();

  return (
    <header className="relative bg-canvas-raised border-b border-rule">
      {/* Single 52px chrome bar (matches RsChrome): brand · inline tabs ·
          right group [stats · status · quick-dial · user]. */}
      <div className="h-[52px] flex items-center px-[18px]">
        {/* Brand — our logo + wordmark (locked; never swap for the mockup mark) */}
        <div className="flex items-center gap-2.5 shrink-0">
          <BrandMark size="md" />
          <div className="leading-none whitespace-nowrap">
            <span className="font-heading text-[13.5px] text-ink font-semibold tracking-heading">{productName}</span>
            <span className="text-[13.5px] text-ink-dim font-normal">&nbsp;Call&nbsp;Center</span>
          </div>
        </div>

        {/* Tabs — inline, full-height, fuchsia underline on active */}
        <nav className="flex items-stretch h-full ml-5">
          <ViewTab
            icon={<Users className="w-3.5 h-3.5" />}
            label="Contacts"
            active={viewMode === 'contacts'}
            onClick={() => onViewModeChange('contacts')}
          />
          <ViewTab
            icon={<Phone className="w-3.5 h-3.5" />}
            label="Active Calls"
            count={callCounts.active}
            active={viewMode === 'calls'}
            onClick={() => onViewModeChange('calls')}
          />
          <ViewTab
            icon={<ListTodo className="w-3.5 h-3.5" />}
            label="Queue"
            count={callCounts.queue}
            tone={callCounts.queue > 0 ? 'wait' : 'default'}
            active={viewMode === 'queue'}
            onClick={() => onViewModeChange('queue')}
          />
          <ViewTab
            icon={<PhoneOutgoing className="w-3.5 h-3.5" />}
            label="Callbacks"
            count={callbacksPending}
            tone={callbacksPending > 0 ? 'wait' : 'default'}
            active={viewMode === 'callbacks'}
            onClick={() => onViewModeChange('callbacks')}
          />
          {isSupervisory(user?.role) && (
            <ViewTab
              icon={<Eye className="w-3.5 h-3.5" />}
              label="Supervisor"
              active={viewMode === 'supervisor'}
              onClick={() => onViewModeChange('supervisor')}
            />
          )}
          {isAdminSurface(user?.role) && (
            <ViewTab
              icon={<Settings className="w-3.5 h-3.5" />}
              label="Settings"
              active={viewMode === 'settings'}
              onClick={() => onViewModeChange('settings')}
            />
          )}
        </nav>

        {/* Right group */}
        <div className="ml-auto flex items-center gap-3.5 shrink-0">
          {/* CallFabric / socket condition banners — rare, kept (mockup omitted them) */}
          {(callFabric.conferenceJoinError || callFabric.registrationError || callFabric.isChangingStatus || socketBanner) && (
            <div className="flex items-center gap-2">
              {callFabric.conferenceJoinError && (
                <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-urgent/10 border border-urgent/30" title={callFabric.conferenceJoinError}>
                  <AlertTriangle className="w-3.5 h-3.5 text-urgent-soft" />
                  <span className="text-[11px] text-urgent-soft mono uppercase tracking-wider">Conf&nbsp;Err</span>
                </div>
              )}
              {callFabric.registrationError && (
                <button
                  onClick={() => callFabric.resetCallFabricState()}
                  className="flex items-center gap-1.5 px-2 py-1 rounded bg-urgent/15 border border-urgent/40 hover:bg-urgent/25 transition-colors"
                  title={callFabric.registrationError}
                >
                  <RotateCcw className="w-3.5 h-3.5 text-urgent-soft" />
                  <span className="text-[11px] text-urgent-soft mono uppercase tracking-wider">Reset&nbsp;CF</span>
                </button>
              )}
              {callFabric.isChangingStatus && (
                <div className="flex items-center gap-1.5 px-2 py-1 rounded bg-info/10 border border-info/30">
                  <span className="w-2.5 h-2.5 border-2 border-info-soft border-t-transparent rounded-full animate-spin" />
                  <span className="text-[11px] text-info-soft mono uppercase tracking-wider">Connecting</span>
                </div>
              )}
              {socketBanner && (
                <div
                  className={`flex items-center gap-1.5 px-2 py-1 rounded border ${
                    socketBanner.tone === 'urgent'
                      ? 'bg-urgent/15 border-urgent/40'
                      : 'bg-wait/10 border-wait/30'
                  }`}
                  title={
                    socketBanner.tone === 'urgent'
                      ? 'Real-time socket disconnected. Call assignments and live updates are not flowing. Reconnecting…'
                      : 'Re-establishing the real-time socket. Updates may lag briefly.'
                  }
                >
                  {socketBanner.spin ? (
                    <span className={`w-2.5 h-2.5 border-2 ${
                      socketBanner.tone === 'urgent' ? 'border-urgent-soft' : 'border-wait-soft'
                    } border-t-transparent rounded-full animate-spin`} />
                  ) : (
                    <span className={`dot ${
                      socketBanner.tone === 'urgent' ? 'dot-urgent' : 'dot-wait'
                    }`} />
                  )}
                  <span className={`text-[11px] mono uppercase tracking-wider ${
                    socketBanner.tone === 'urgent' ? 'text-urgent-soft' : 'text-wait-soft'
                  }`}>
                    {socketBanner.label}
                  </span>
                </div>
              )}
            </div>
          )}

          {/* Stats — column label/value, separated by a hairline (RsChrome rs-stats) */}
          <div className="hidden xl:flex items-stretch gap-5 pr-3.5 border-r border-rule">
            <Stat kicker="Today"    value={String(stats.callsToday)} />
            <Stat kicker="Avg time" value={formatMMSS(stats.avgHandleTime)} />
            <Stat
              kicker="In queue"
              value={String(stats.queueDepth)}
              tone={stats.queueDepth > 0 ? 'wait' : 'default'}
            />
            <Stat
              kicker="Wait"
              value={stats.longestWait > 0 ? formatMMSS(stats.longestWait) : '—'}
              tone={stats.longestWait > 60 ? 'urgent' : stats.longestWait > 30 ? 'wait' : 'default'}
            />
          </div>

          {/* Status pill + presence/queues dropdown */}
          <div ref={statusRef} className="relative">
            <button
              onClick={() => setShowStatusDropdown(v => !v)}
              className="flex items-center gap-2 px-3 py-1.5 rounded-lg bg-canvas-raised border border-rule-strong hover:bg-canvas-hover transition-colors"
            >
              <span className={current.dotClass} />
              <span className={`text-[13px] font-medium ${current.textClass}`}>{current.label}</span>
              {agentStatus === 'after-call' && acwSecondsLeft != null && (
                <span
                  className="mono text-[10px] px-1.5 py-0.5 rounded-sm text-wait-soft border border-rule-strong leading-none tabular-nums"
                  title="After-call work — auto-returns to Available when the timer ends"
                >
                  {formatMMSS(acwSecondsLeft)}
                </span>
              )}
              {activeQueueCount > 0 && agentStatus !== 'offline' && (
                <span className="mono text-[10px] px-1.5 py-0.5 rounded-sm bg-transparent text-ink-muted border border-rule-strong leading-none">
                  {activeQueueCount}
                </span>
              )}
              <ChevronDown className="w-3.5 h-3.5 text-ink-dim" />
            </button>

            {/* Demo-only nudges (offline → go available; available+0 queues → pick one). */}
            <DemoTip
              id="demo-go-available"
              show={agentStatus === 'offline' && !showStatusDropdown}
              title="Set yourself available"
              body="Click here and switch to Available to enter the queue and start receiving calls."
              placement="bottom-end"
            />
            <DemoTip
              id="demo-pick-a-queue"
              show={
                agentStatus === 'available' &&
                activeQueueCount === 0 &&
                availableQueues.length > 0 &&
                !showStatusDropdown
              }
              title="Almost there — pick your queues"
              body="You're available but not signed up for any queues, so calls won't route to you. Click your status pill and check the queues you want calls from — a call only rings you if its queue is checked."
              placement="bottom-end"
            />

            {showStatusDropdown && (
              <div className="absolute top-full right-0 mt-2 w-60 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
                <div className="px-3 py-2 border-b border-rule">
                  <span className="kicker">Presence</span>
                </div>
                {(Object.keys(statusMeta) as AgentStatus[]).map((s) => {
                  const m = statusMeta[s];
                  const active = s === agentStatus;
                  return (
                    <button
                      key={s}
                      onClick={() => { onStatusChange(s); setShowStatusDropdown(false); }}
                      className={`w-full flex items-center gap-2.5 px-3 py-2 text-left transition-colors ${
                        active ? 'bg-canvas-hover' : 'hover:bg-canvas-hover'
                      }`}
                    >
                      <span className={m.dotClass} />
                      <span className={`text-[13px] ${m.textClass}`}>{m.label}</span>
                      {active && <span className="ml-auto kicker text-sw-fuchsia">Now</span>}
                    </button>
                  );
                })}

                {availableQueues.length > 0 && (
                  <>
                    <div className="px-3 py-2 border-t border-rule mt-1 flex items-center justify-between">
                      <span className="kicker">My queues</span>
                    </div>
                    {queueTip.shouldShow && (
                      <div className="mx-3 mb-1 px-2.5 py-2 rounded border border-ai/40 bg-ai/10">
                        <div className="flex items-start gap-1.5">
                          <Sparkles className="w-3 h-3 mt-0.5 text-ai-soft shrink-0" />
                          <div className="flex-1 min-w-0">
                            <p className="text-[11.5px] text-ink leading-snug">
                              <span className="text-ai-soft font-medium">Activate a queue</span>{' '}
                              to start receiving calls from it. You can be on
                              multiple queues at once.
                            </p>
                          </div>
                          <button
                            type="button"
                            onClick={(e) => { e.preventDefault(); queueTip.dismiss(); }}
                            aria-label="Dismiss tip"
                            className="-mt-0.5 -mr-0.5 p-0.5 rounded text-ink-dim hover:text-ink"
                          >
                            <XIcon className="w-3 h-3" />
                          </button>
                        </div>
                      </div>
                    )}
                    <div className="max-h-48 overflow-y-auto pb-1">
                      {availableQueues.map(queue => (
                        <div
                          key={queue.id}
                          className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-canvas-hover"
                        >
                          <Checkbox
                            checked={queue.is_activated}
                            onChange={() => handleQueueToggle(queue)}
                          />
                          <span className="text-[13px] text-ink flex-1 truncate">{queue.display_name}</span>
                          <Chip>
                            {STRATEGY_SHORT[queue.routing_strategy] || queue.routing_strategy}
                          </Chip>
                        </div>
                      ))}
                    </div>
                  </>
                )}
              </div>
            )}
          </div>

          {/* Quick dial */}
          <div className="relative">
            <button
              onClick={() => setShowQuickDial(v => !v)}
              className={`flex items-center justify-center w-[30px] h-[30px] rounded-md border border-rule-strong bg-canvas-raised hover:bg-canvas-hover transition-colors ${
                callFabric.isOnline ? 'text-live-soft' : 'text-ink-muted'
              }`}
              title={callFabric.isOnline ? 'Quick Dial (Online)' : 'Quick Dial (Offline)'}
            >
              <PhoneCall className="w-4 h-4" />
            </button>
            {showQuickDial && (
              <QuickDialDropdown
                callFabric={callFabric}
                onClose={() => setShowQuickDial(false)}
                onCallStarted={onOutboundCallStarted}
              />
            )}
          </div>

          {/* User */}
          <div ref={userRef} className="relative">
            <button
              onClick={() => setShowUserMenu(v => !v)}
              className="flex items-center gap-2 pl-1 pr-1.5 py-1 rounded-lg hover:bg-canvas-hover transition-colors"
            >
              <span className="w-7 h-7 rounded-full bg-canvas-elevated border border-rule-strong flex items-center justify-center text-[11px] font-semibold text-ink-muted">
                {user?.email?.charAt(0).toUpperCase() || '?'}
              </span>
              <ChevronDown className="w-3.5 h-3.5 text-ink-dim" />
            </button>
            {showUserMenu && (
              <div className="absolute top-full right-0 mt-2 w-56 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
                <div className="px-3 py-2 border-b border-rule">
                  <div className="kicker">Signed in as</div>
                  <div className="text-[13px] text-ink mono truncate">{user?.email}</div>
                  {user?.role && (
                    <div className="mt-1">
                      <Chip>{user.role}</Chip>
                    </div>
                  )}
                </div>
                <button
                  onClick={() => { setShowUserMenu(false); onLogout(); }}
                  className="w-full flex items-center gap-2.5 px-3 py-2 text-left text-urgent-soft hover:bg-canvas-hover transition-colors"
                >
                  <LogOut className="w-4 h-4" />
                  <span className="text-[13px]">Sign out</span>
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </header>
  );
}

function Stat({
  kicker,
  value,
  tone = 'default',
}: {
  kicker: string;
  value: string;
  tone?: 'default' | 'wait' | 'urgent';
}) {
  const color =
    tone === 'urgent' ? 'text-urgent-soft' :
    tone === 'wait'   ? 'text-wait-soft'   :
    'text-ink';
  return (
    <div className="flex flex-col justify-center leading-none gap-[3px] min-w-0">
      <span className="text-[10px] text-ink-dim font-medium whitespace-nowrap">{kicker}</span>
      <span className={`mono text-[13px] font-semibold ${color}`}>{value}</span>
    </div>
  );
}

function ViewTab({
  icon,
  label,
  count,
  tone = 'default',
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  count?: number;
  tone?: 'default' | 'wait';
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`relative flex items-center gap-1.5 px-3 h-full text-[13px] font-medium transition-colors ${
        active
          ? 'text-ink'
          : 'text-ink-muted hover:text-ink'
      }`}
    >
      {icon}
      <span className="whitespace-nowrap">{label}</span>
      {count !== undefined && count > 0 && (
        <span className={`mono text-[10px] px-1.5 py-0.5 rounded-sm border leading-none ${
          tone === 'wait'
            ? 'bg-transparent text-wait-soft border-rule-strong'
            : 'bg-transparent text-ink-muted border-rule-strong'
        }`}>
          {count}
        </span>
      )}
      {active && (
        <span className="absolute -bottom-px left-0 right-0 h-[2px] bg-sw-fuchsia" />
      )}
    </button>
  );
}

export default UnifiedHeader;
