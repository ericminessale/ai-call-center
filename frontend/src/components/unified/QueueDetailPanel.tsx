import { useState, useEffect } from 'react';
import { Phone, User } from 'lucide-react';
import { Call, QueueConfig, Interaction } from '../../types/callcenter';
import { contactsApi } from '../../services/api';
import { logger } from '../../lib/logger';
import { Button, Chip } from '../restraint';
import { AISummaryDisplay } from '../contacts/ContactDetailView';

interface QueueDetailPanelProps {
  call: Call;
  queueConfigs?: QueueConfig[];
  /** Take the call now (assign to me + join). */
  onAnswer: () => void;
  /** Open the full contact record for this caller. */
  onOpenContact: () => void;
}

const STRATEGY_LABEL: Record<string, string> = {
  fifo: 'FIFO',
  round_robin: 'Round Robin',
  priority: 'Priority-Based',
  skill_based: 'Skill-Based',
};

const fmtMMSS = (sec: number) => `${Math.floor(Math.max(0, sec) / 60)}:${String(Math.max(0, sec) % 60).padStart(2, '0')}`;

/**
 * Bespoke pre-answer triage surface for a WAITING queued caller (Restraint
 * RestraintQueue main). Distinct from ContactDetailView: it's call-centric and
 * surfaces queue telemetry — live time-in-queue, SLA, AI-fallback timer,
 * routing, and the caller's last interaction — so an agent can size up the call
 * before taking it. Wiring (take / open-contact) is delegated to the parent.
 */
export function QueueDetailPanel({ call, queueConfigs, onAnswer, onOpenContact }: QueueDetailPanelProps) {
  const slug = call.queue_id || (call as any).queueId || '';
  const qc: any = queueConfigs?.find((q) => q.slug === slug);
  const slaSec = Number(qc?.sla_threshold_seconds) || 60;
  // Hold cap (`max_wait_before_ai_fallback` — legacy field name). 0 means the
  // admin disabled it, so `??` not `||`: coercing an explicit 0 to 120 would
  // show a countdown to something the backend will never do. null/undefined =
  // config not loaded yet, so use the model default.
  const holdCapSec = qc?.max_wait_before_ai_fallback == null
    ? 120
    : Number(qc.max_wait_before_ai_fallback);
  const strategy = STRATEGY_LABEL[qc?.routing_strategy || ''] || qc?.routing_strategy || '—';

  const name = call.contact?.displayName || call.from_number || call.phoneNumber || 'Unknown caller';
  const phone = call.from_number || call.phoneNumber || (call as any).fromNumber || '';
  const isAIOrigin = call.handler_type === 'ai' || !!call.ai_agent_name;
  const ai: any = (call as any).aiContext || {};
  const intent = ai.reason || ai.intent || ai.issue || ai.issue_description || '—';

  // Live-ticking time in queue from created_at.
  const [waitSec, setWaitSec] = useState(0);
  useEffect(() => {
    if (!call.created_at) return;
    const upd = () => setWaitSec(Math.floor((Date.now() - new Date(call.created_at!).getTime()) / 1000));
    upd();
    const id = setInterval(upd, 1000);
    return () => clearInterval(id);
  }, [call.created_at]);
  const overSla = waitSec > slaSec;
  const callbackIn = Math.max(0, holdCapSec - waitSec);

  // The caller's most recent prior interaction (triage context). Skips cleanly
  // for unknown callers / first-time contacts.
  const [lastInteraction, setLastInteraction] = useState<Interaction | null>(null);
  const [loadedLast, setLoadedLast] = useState(false);
  useEffect(() => {
    let cancelled = false;
    if (!call.contact_id) { setLoadedLast(true); return; }
    setLoadedLast(false);
    contactsApi.getInteractions(call.contact_id, 1, 1)
      .then((r) => { if (!cancelled) { setLastInteraction(r.data.interactions?.[0] || null); setLoadedLast(true); } })
      .catch((e) => { logger.debug('QueueDetail: no prior interactions', e?.message); if (!cancelled) setLoadedLast(true); });
    return () => { cancelled = true; };
  }, [call.contact_id]);

  return (
    <div className="h-full overflow-y-auto bg-canvas px-6 pt-5 pb-6">
      {/* Call head — avatar · name/phone · actions */}
      <div className="flex items-center gap-3.5">
        <div className="w-[46px] h-[46px] rounded-xl bg-canvas-elevated border border-rule-strong flex items-center justify-center text-ink text-[17px] font-semibold flex-shrink-0">
          {name === 'Unknown caller'
            ? '?'
            : name.split(' ').map((w) => w[0]).filter(Boolean).slice(0, 2).join('').toUpperCase()}
        </div>
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <h2 className="text-[23px] font-semibold text-ink tracking-tight leading-tight truncate">{name}</h2>
            {isAIOrigin && <Chip ai>{call.ai_agent_name || 'Receptionist'}</Chip>}
          </div>
          {phone && <div className="mono text-[11.5px] text-ink-dim mt-[3px]">{phone}</div>}
        </div>
        <div className="ml-auto flex items-center gap-2">
          <Button variant="primary" onClick={onAnswer} icon={<Phone className="w-3.5 h-3.5" />}>Answer now</Button>
          <Button variant="secondary" onClick={onOpenContact} icon={<User className="w-3.5 h-3.5" />}>Open contact</Button>
        </div>
      </div>

      {/* Time in queue | Routing */}
      <div className="grid grid-cols-2 gap-2.5 mt-[18px]">
        <div className="border border-rule rounded-lg bg-canvas-raised px-4 py-3.5">
          <h3 className="text-[13.5px] font-semibold text-ink">Time in queue</h3>
          <div className="flex items-baseline gap-3 mt-3">
            <span className={`mono text-[44px] font-semibold leading-none tabular-nums ${overSla ? 'text-status-warning' : 'text-ink'}`}>
              {fmtMMSS(waitSec)}
            </span>
            {overSla && <Chip dot="warning">Over SLA {slaSec}s</Chip>}
          </div>
          <div className="h-1.5 rounded-full bg-rule overflow-hidden mt-3.5">
            <div
              className="h-full rounded-full bg-status-warning"
              style={{ width: `${Math.min(100, (waitSec / slaSec) * 100)}%` }}
            />
          </div>
          <div className="text-[11px] text-ink-dim mt-2">
            {holdCapSec > 0
              ? `Caller is offered a callback in ${fmtMMSS(callbackIn)} if no agent answers`
              : 'No hold limit set for this queue'}
          </div>
        </div>

        <div className="border border-rule rounded-lg bg-canvas-raised px-4 py-3.5">
          <h3 className="text-[13.5px] font-semibold text-ink">Routing</h3>
          <div className="flex flex-col gap-2 mt-2.5">
            <Row k="Queue" v={`${qc?.display_name || slug || '—'} · ${strategy}`} />
            <Row k="Priority" v={<span className="mono">{call.priority ?? qc?.default_priority ?? '—'}</span>} />
            <Row k="Triage intent" v={intent} />
            <Row k="Eligible agents" v={qc?.agent_count != null ? `${qc.agent_count} assigned` : '—'} />
          </div>
        </div>
      </div>

      {/* Last interaction */}
      {loadedLast && (
        <div className="border border-rule rounded-lg bg-canvas-raised px-4 py-3.5 mt-2.5">
          <h3 className="text-[13.5px] font-semibold text-ink">
            {lastInteraction
              ? `Last interaction · ${new Date(lastInteraction.createdAt).toLocaleDateString()}`
              : 'First contact'}
          </h3>
          <div className="text-[12.5px] text-ink-muted leading-relaxed mt-2">
            {lastInteraction?.summary
              ? <AISummaryDisplay summary={lastInteraction.summary} />
              : 'No prior interactions on record for this caller.'}
          </div>
        </div>
      )}
    </div>
  );
}

function Row({ k, v }: { k: string; v: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 text-[12px]">
      <span className="text-ink-dim flex-shrink-0">{k}</span>
      <span className="text-ink font-medium text-right truncate">{v}</span>
    </div>
  );
}

export default QueueDetailPanel;
