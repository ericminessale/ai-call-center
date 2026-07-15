import { useEffect, useState } from 'react';
import { Clock, LogOut, Phone, PhoneCall, ShieldCheck, Sparkles } from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../stores/authStore';
import { demoApi } from '../../services/api';
import websocket from '../../services/websocket';

/**
 * Persistent strip across the top of the agent dashboard, only
 * rendered when the backend's runtime config reports DEMO_MODE=true.
 *
 * Surfaces:
 *   - the "DEMO" label so visitors know they're not in a real
 *     production call center
 *   - that this is the visitor's own private workspace, and how long it
 *     lives (expiry rides the 60s heartbeat, so the chip stays honest)
 *   - the demo phone number, dial-able with one click on mobile
 *   - phone verification: get a pairing code, TEXT it to the demo number,
 *     and calls between you and your workspace go live
 *
 * Renders nothing in production-shape clone-and-own deployments.
 */

/** Parse the backend's naive-UTC isoformat (no 'Z'/offset) as UTC, not
 *  local time — otherwise the countdown is skewed by the browser's offset
 *  (a Sydney visitor could see a live workspace as already expired). */
function parseUtcMs(iso: string | null | undefined): number {
  if (!iso) return NaN;
  const hasTz = /[zZ]|[+-]\d{2}:?\d{2}$/.test(iso);
  return new Date(hasTz ? iso : `${iso}Z`).getTime();
}

/** Human "time left" for the workspace chip: days above 24h, hours below. */
function timeLeft(expiresAt: string | null | undefined): { label: string; soon: boolean } | null {
  const expMs = parseUtcMs(expiresAt);
  if (!Number.isFinite(expMs)) return null;
  const ms = expMs - Date.now();
  if (ms <= 0) return null;
  const hours = ms / 3_600_000;
  if (hours < 24) return { label: `${Math.max(1, Math.floor(hours))}h left`, soon: true };
  return { label: `${Math.floor(hours / 24)}d left`, soon: false };
}

export function DemoBanner() {
  const { runtimeConfig, user, workspace, logout } = useAuthStore();

  const [code, setCode] = useState<string | null>(null);
  const [verified, setVerified] = useState(false);
  const [maskedNumber, setMaskedNumber] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isDemo = !!runtimeConfig?.demo_mode;

  // Hydrate verification state on mount (covers reload while already verified
  // or with a live code), and listen for the "verified" push. Platform users
  // skip it — the verify endpoints are visitor-only (403 for them).
  useEffect(() => {
    if (!isDemo || (user != null && user.workspace_id == null)) return;
    let active = true;
    demoApi
      .verifyStatus()
      .then((r) => {
        if (!active) return;
        setVerified(r.data.verified);
        setMaskedNumber(r.data.masked_number);
        if (!r.data.verified) setCode(r.data.code);
      })
      .catch(() => {});

    const onVerified = (data: { masked_number?: string }) => {
      setVerified(true);
      setMaskedNumber(data?.masked_number ?? null);
      setCode(null);
      toast.success('Phone verified — call the demo number and your AI picks up');
    };
    websocket.on('demo_phone_verified', onVerified);
    return () => {
      active = false;
      websocket.off('demo_phone_verified', onVerified);
    };
  }, [isDemo, user]);

  if (!isDemo) return null;

  const personaName = user?.name ?? user?.email ?? 'demo agent';
  const phoneNumbers = runtimeConfig?.demo_phone_numbers ?? [];
  const remaining = timeLeft(workspace?.expires_at);
  // Platform operator (workspace null) on a hosted install: the visitor
  // affordances (verify, workspace expiry, leave-demo) don't apply — the
  // verify endpoints refuse platform users. Show a minimal strip instead.
  const isPlatformUser = user != null && user.workspace_id == null;

  if (isPlatformUser) {
    return (
      <div
        className="relative z-30 flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2 bg-ai/10 border-b border-ai/30 text-[12px] text-ink"
        role="status"
        aria-label="Demo mode banner"
      >
        <span className="inline-flex items-center gap-1.5 font-mono uppercase tracking-[0.2em] text-[10px] text-ai-soft">
          <Sparkles className="h-3 w-3" />
          demo
        </span>
        <span className="text-ink-muted">
          Hosted demo install — signed in as{' '}
          <span className="text-ink font-medium">{personaName}</span> (platform
          operator). Visitor workspaces are under Settings → Workspaces.
        </span>
      </div>
    );
  }

  const getCode = async () => {
    setBusy(true);
    try {
      const r = await demoApi.pairingCode();
      setCode(r.data.code);
    } catch {
      toast.error('Could not generate a code — try again.');
    } finally {
      setBusy(false);
    }
  };

  const callMe = async () => {
    setBusy(true);
    try {
      await demoApi.callMe();
      toast.success('The AI is calling your verified number now');
    } catch (err: any) {
      toast.error(
        err?.response?.data?.error || 'Could not place the demo call — try again.'
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="relative z-30 flex flex-wrap items-center gap-x-3 gap-y-1.5 px-4 py-2 bg-ai/10 border-b border-ai/30 text-[12px] text-ink"
      role="status"
      aria-label="Demo mode banner"
    >
      <span className="inline-flex items-center gap-1.5 font-mono uppercase tracking-[0.2em] text-[10px] text-ai-soft">
        <Sparkles className="h-3 w-3" />
        demo
      </span>

      <span className="text-ink-muted">
        Your private workspace — signed in as{' '}
        <span className="text-ink font-medium">{personaName}</span>.
      </span>

      {remaining && (
        <span
          className={`inline-flex items-center gap-1 font-mono text-[11px] ${
            remaining.soon ? 'text-status-warning' : 'text-ink-faint'
          }`}
          title={(() => {
            const d = runtimeConfig?.workspace_ttl_days ?? 7;
            return `Your workspace expires after ${d} ${d === 1 ? 'day' : 'days'} of inactivity — any visit from this browser extends it. Keep this browser to come back to it.`;
          })()}
        >
          <Clock className="h-3 w-3" />
          {remaining.label}
        </span>
      )}

      {/* Verification affordance */}
      {verified ? (
        <span className="inline-flex items-center gap-2">
          <span className="inline-flex items-center gap-1 text-ai-soft">
            <ShieldCheck className="h-3.5 w-3.5" />
            <span className="font-medium">Verified</span>
            {maskedNumber && (
              <span className="font-mono text-ink-muted">{maskedNumber}</span>
            )}
          </span>
          <button
            type="button"
            onClick={callMe}
            disabled={busy}
            className="inline-flex items-center gap-1.5 rounded border border-ai/40 px-2 py-0.5 text-ai-soft hover:text-ai hover:border-ai transition-colors disabled:opacity-50"
            title="Have the AI place an outbound call to your verified number"
          >
            <PhoneCall className="h-3 w-3" />
            Have the AI call me
          </button>
        </span>
      ) : code ? (
        <span className="inline-flex items-center gap-1.5">
          <span className="text-ink-muted">Text</span>
          <span className="font-mono text-[13px] tracking-[0.3em] text-ai font-semibold">
            {code}
          </span>
          <span className="text-ink-muted">
            to{' '}
            {phoneNumbers.length > 0 ? (
              <a
                href={`sms:${phoneNumbers[0].number}?&body=${code}`}
                className="font-mono text-ai-soft hover:text-ai transition-colors"
              >
                {phoneNumbers[0].number}
              </a>
            ) : (
              'the demo number'
            )}
          </span>
          <span className="text-ink-faint">to verify your phone</span>
        </span>
      ) : (
        <button
          type="button"
          onClick={getCode}
          disabled={busy}
          className="inline-flex items-center gap-1.5 rounded border border-ai/40 px-2 py-0.5 text-ai-soft hover:text-ai hover:border-ai transition-colors disabled:opacity-50"
          title="Verify your phone to unlock calling — your workspace accepts calls only from your verified number, and the AI can call you back"
        >
          <ShieldCheck className="h-3 w-3" />
          Verify your phone to start calling
        </button>
      )}

      {phoneNumbers.length > 0 && (
        <span className="hidden md:flex items-center gap-3 ml-auto">
          {phoneNumbers.map((p) => (
            <span key={p.number} className="inline-flex items-center gap-1.5">
              <Phone className="h-3 w-3 text-ai-soft" />
              <span className="text-ink-muted">{p.label}:</span>
              <a
                href={`tel:${p.number}`}
                className="font-mono text-ai-soft hover:text-ai transition-colors"
              >
                {p.number}
              </a>
            </span>
          ))}
        </span>
      )}

      <button
        type="button"
        onClick={logout}
        className={`inline-flex items-center gap-1.5 text-ink-muted hover:text-ink transition-colors ${
          phoneNumbers.length > 0 ? '' : 'ml-auto'
        }`}
        title="End your demo and release this workspace"
      >
        <LogOut className="h-3 w-3" />
        Leave demo
      </button>
    </div>
  );
}
