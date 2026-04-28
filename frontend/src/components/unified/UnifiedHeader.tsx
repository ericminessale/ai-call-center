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
} from 'lucide-react';
import { ViewMode, AgentStatus } from '../../pages/UnifiedAgentDesktop';
import Logo from '../shared/Logo';
import { QuickDialDropdown } from './QuickDialDropdown';
import { queueApi } from '../../services/api';
import toast from 'react-hot-toast';
import { DemoTip, useDemoTip } from '../shared/DemoTip';

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
  const activeQueueCount = availableQueues.filter(q => q.is_activated).length;
  const formatMMSS = (secs: number) =>
    `${Math.floor(secs / 60)}:${String(secs % 60).padStart(2, '0')}`;

  return (
    <header className="relative bg-canvas border-b border-rule">
      {/* Top row — Brand | Status · Stats | Quick-dial · User */}
      <div className="h-[52px] flex items-stretch">
        {/* Brand cell */}
        <div className="flex items-center gap-2.5 pl-5 pr-6 border-r border-rule min-w-[260px]">
          <Logo size="md" />
          <div className="flex flex-col leading-none">
            <span className="font-heading text-[16px] text-ink font-semibold tracking-heading">
              SignalWire
            </span>
            <span className="kicker mt-[3px] text-[9px]">Call&nbsp;Center</span>
          </div>
        </div>

        {/* Status */}
        <div ref={statusRef} className="relative flex items-center px-4 border-r border-rule">
          <button
            onClick={() => setShowStatusDropdown(v => !v)}
            className="flex items-center gap-2 px-3 py-1.5 rounded bg-canvas-raised border border-rule hover:border-rule-strong transition-colors"
          >
            <span className={current.dotClass} />
            <span className={`text-[13px] font-medium ${current.textClass}`}>{current.label}</span>
            {activeQueueCount > 0 && agentStatus !== 'offline' && (
              <span className="mono text-[10px] px-1.5 py-0.5 rounded bg-live/15 text-live-soft border border-live/25 leading-none">
                {activeQueueCount}
              </span>
            )}
            <ChevronDown className="w-3.5 h-3.5 text-ink-dim" />
          </button>

          {/* Demo-only nudges: a small state machine that walks a
              fresh visitor from "I just landed" → "I'm online and on
              a queue" without leaving them stuck.
                1. Offline               → "Set yourself available"
                2. Available + 0 queues  → "Pick a queue"
                3. Available + ≥1 queue  → silent
              Both fire in the same spot below the status pill (the
              two states are mutually exclusive). Each has its own
              dismissal id so individual dismissals don't bleed
              across states. Hidden whenever the dropdown is open
              (the inline note inside the dropdown takes over). */}
          <DemoTip
            id="demo-go-available"
            show={agentStatus === 'offline' && !showStatusDropdown}
            title="Set yourself available"
            body="Click here and switch to Available to enter the queue and start receiving calls."
            placement="bottom-start"
          />
          <DemoTip
            id="demo-pick-a-queue"
            show={
              agentStatus === 'available' &&
              activeQueueCount === 0 &&
              availableQueues.length > 0 &&
              !showStatusDropdown
            }
            title="Almost there — pick a queue"
            body="You're available but not signed up for any queues, so calls won't route to you. Click your status pill and check at least one."
            placement="bottom-start"
          />

          {callFabric.conferenceJoinError && (
            <div className="ml-2 flex items-center gap-1.5 px-2 py-1 rounded bg-urgent/10 border border-urgent/30" title={callFabric.conferenceJoinError}>
              <AlertTriangle className="w-3.5 h-3.5 text-urgent-soft" />
              <span className="text-[11px] text-urgent-soft mono uppercase tracking-wider">Conf&nbsp;Err</span>
            </div>
          )}
          {callFabric.isChangingStatus && (
            <div className="ml-2 flex items-center gap-1.5 px-2 py-1 rounded bg-info/10 border border-info/30">
              <span className="w-2.5 h-2.5 border-2 border-info-soft border-t-transparent rounded-full animate-spin" />
              <span className="text-[11px] text-info-soft mono uppercase tracking-wider">Connecting</span>
            </div>
          )}

          {showStatusDropdown && (
            <div className="absolute top-full left-4 mt-2 w-60 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
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
                    {active && <span className="ml-auto kicker" style={{ color: 'var(--sw-turquoise)' }}>Now</span>}
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
                      <label
                        key={queue.id}
                        className="flex items-center gap-2.5 px-3 py-1.5 hover:bg-canvas-hover cursor-pointer"
                      >
                        <input
                          type="checkbox"
                          checked={queue.is_activated}
                          onChange={() => handleQueueToggle(queue)}
                          className="w-3.5 h-3.5 rounded-sm bg-canvas-raised border-rule-strong accent-sw-blue"
                        />
                        <span className="text-[13px] text-ink flex-1 truncate">{queue.display_name}</span>
                        <span className="chip chip-muted">
                          {STRATEGY_SHORT[queue.routing_strategy] || queue.routing_strategy}
                        </span>
                      </label>
                    ))}
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        {/* Stats strip — the hero bit. Mono digits, kicker labels. */}
        <div className="flex items-center gap-6 px-6 flex-1 min-w-0 overflow-hidden">
          <Stat kicker="Today"    value={String(stats.callsToday)} />
          <Stat kicker="Avg Time" value={formatMMSS(stats.avgHandleTime)} />
          <Stat
            kicker="In Queue"
            value={String(stats.queueDepth)}
            tone={stats.queueDepth > 0 ? 'wait' : 'default'}
          />
          <Stat
            kicker="Wait"
            value={stats.longestWait > 0 ? formatMMSS(stats.longestWait) : '—'}
            tone={stats.longestWait > 60 ? 'urgent' : stats.longestWait > 30 ? 'wait' : 'default'}
          />
        </div>

        {/* Quick dial + user */}
        <div className="flex items-center gap-2 pr-4 border-l border-rule">
          <div className="relative">
            <button
              onClick={() => setShowQuickDial(v => !v)}
              className={`flex items-center justify-center w-9 h-9 rounded transition-colors ${
                callFabric.isOnline
                  ? 'text-live-soft hover:bg-canvas-hover'
                  : 'text-ink-dim hover:bg-canvas-hover'
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

          <div ref={userRef} className="relative">
            <button
              onClick={() => setShowUserMenu(v => !v)}
              className="flex items-center gap-2 px-3 py-1.5 rounded hover:bg-canvas-hover transition-colors"
            >
              <span className="w-6 h-6 rounded-full bg-canvas-raised border border-rule flex items-center justify-center text-[11px] font-semibold text-ink">
                {user?.email?.charAt(0).toUpperCase() || '?'}
              </span>
              <span className="text-[12px] text-ink-muted mono truncate max-w-[160px]">{user?.email}</span>
              <ChevronDown className="w-3.5 h-3.5 text-ink-dim" />
            </button>
            {showUserMenu && (
              <div className="absolute top-full right-0 mt-2 w-56 panel-raised rounded-md shadow-panel z-50 animate-fade-up overflow-hidden">
                <div className="px-3 py-2 border-b border-rule">
                  <div className="kicker">Signed in as</div>
                  <div className="text-[13px] text-ink mono truncate">{user?.email}</div>
                  {user?.role && (
                    <div className="mt-1">
                      <span className="chip chip-muted">{user.role}</span>
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

      {/* Tab strip — underline nav, serif-lite */}
      <div className="h-10 flex items-center px-5 border-t border-rule bg-canvas-sunken/40">
        <nav className="flex items-center gap-1">
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
          {(user?.role === 'admin' || user?.role === 'supervisor') && (
            <ViewTab
              icon={<Eye className="w-3.5 h-3.5" />}
              label="Supervisor"
              active={viewMode === 'supervisor'}
              onClick={() => onViewModeChange('supervisor')}
            />
          )}
          {user?.role === 'admin' && (
            <ViewTab
              icon={<Settings className="w-3.5 h-3.5" />}
              label="Settings"
              active={viewMode === 'settings'}
              onClick={() => onViewModeChange('settings')}
            />
          )}
        </nav>
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
    <div className="flex flex-col leading-none gap-1 min-w-0">
      <span className="kicker">{kicker}</span>
      <span className={`mono text-[15px] font-medium ${color}`}>{value}</span>
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
      className={`relative flex items-center gap-2 px-3 h-9 text-[13px] font-medium transition-colors ${
        active
          ? 'text-ink'
          : 'text-ink-dim hover:text-ink-muted'
      }`}
    >
      {icon}
      <span>{label}</span>
      {count !== undefined && count > 0 && (
        <span className={`mono text-[10px] px-1.5 py-0.5 rounded border leading-none ${
          tone === 'wait'
            ? 'bg-wait/10 text-wait-soft border-wait/25'
            : 'bg-canvas-raised text-ink-muted border-rule'
        }`}>
          {count}
        </span>
      )}
      {active && (
        <span className="absolute -bottom-[1px] left-2 right-2 h-[2px] rounded-sm" style={{ background: 'var(--sw-turquoise)' }} />
      )}
    </button>
  );
}

export default UnifiedHeader;
