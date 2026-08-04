import { useState } from 'react';
import { Clock, Github, LogOut, Phone, PhoneCall, ShieldCheck, Sparkles } from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../stores/authStore';
import { useVerifyStore } from '../../stores/verifyStore';
import { demoApi } from '../../services/api';

/**
 * Thin persistent chrome strip across the top of the agent dashboard, only
 * rendered when the backend's runtime config reports DEMO_MODE=true.
 *
 * Deliberately carries ONLY data a visitor can act on:
 *   - how long this workspace lives (the one thing they can lose)
 *   - the number to CALL to test it, and the number they VERIFIED
 *   - have-the-AI-call-me, clone-the-repo, leave-demo
 *
 * What used to be here and isn't: a "DEMO" sparkle chip and "Your private
 * workspace — signed in as Demo Admin". Both were decoration paying no rent
 * — the persona name is a generated placeholder, and a visitor who clicked
 * "try the demo" does not need to be told they're in a demo. The strip has
 * to earn its horizontal space; every remaining item is either a fact they
 * need or a button they press.
 *
 * The VERIFICATION FLOW itself lives in the prominent PhoneVerificationCard
 * below this strip (shown while unverified); once verified this strip shows a
 * compact "Verified" badge. Verification state comes from the shared
 * useVerifyStore (hydrated by useDemoVerification), not a local fetch.
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
  const verified = useVerifyStore((s) => s.verified);
  const maskedNumber = useVerifyStore((s) => s.maskedNumber);
  const [busy, setBusy] = useState(false);

  const isDemo = !!runtimeConfig?.demo_mode;
  if (!isDemo) return null;

  const personaName = user?.name ?? user?.email ?? 'demo agent';
  const phoneNumbers = runtimeConfig?.demo_phone_numbers ?? [];
  const repoUrl = runtimeConfig?.repo_url ?? null;
  const remaining = timeLeft(workspace?.expires_at);
  const isPlatformUser = user != null && user.workspace_id == null;

  // Platform operator (workspace null) on a hosted install: the visitor
  // affordances (verify, workspace expiry, leave-demo) don't apply — the
  // verify endpoints refuse platform users. Show a minimal strip instead.
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

      {/* Verified badge + call-me. The UNVERIFIED flow is the prominent
          PhoneVerificationCard below this strip, so nothing verify-related
          renders here until it succeeds. */}
      {verified && (
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
      )}

      {/* Right cluster: the number to call, then the two exits. `ml-auto` on
          whichever item comes first so the cluster pins right regardless of
          which pieces are configured. */}
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

      {repoUrl && (
        <a
          href={repoUrl}
          target="_blank"
          rel="noopener noreferrer"
          className={`inline-flex items-center gap-1.5 text-ai-soft hover:text-ai transition-colors ${
            phoneNumbers.length > 0 ? '' : 'ml-auto'
          }`}
          title="This whole call center is open source — clone it and run your own"
        >
          <Github className="h-3 w-3" />
          Clone this
        </a>
      )}

      <button
        type="button"
        onClick={logout}
        className={`inline-flex items-center gap-1.5 text-ink-muted hover:text-ink transition-colors ${
          phoneNumbers.length > 0 || repoUrl ? '' : 'ml-auto'
        }`}
        title="End your demo and release this workspace"
      >
        <LogOut className="h-3 w-3" />
        Leave demo
      </button>
    </div>
  );
}
