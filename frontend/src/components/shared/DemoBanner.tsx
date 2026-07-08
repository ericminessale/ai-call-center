import { useEffect, useState } from 'react';
import { LogOut, Phone, PhoneCall, ShieldCheck, Sparkles } from 'lucide-react';
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
 *   - the visitor's leased agent persona name
 *   - the demo phone numbers, dial-able with one click on mobile
 *   - phone verification: get a pairing code, TEXT it to the demo number,
 *     and once verified your calls become private + the AI can call you back
 *
 * Renders nothing in production-shape clone-and-own deployments.
 */
export function DemoBanner() {
  const { runtimeConfig, user, logout } = useAuthStore();

  const [code, setCode] = useState<string | null>(null);
  const [verified, setVerified] = useState(false);
  const [maskedNumber, setMaskedNumber] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const isDemo = !!runtimeConfig?.demo_mode;

  // Hydrate verification state on mount (covers reload while already verified
  // or with a live code), and listen for the mid-call "verified" push.
  useEffect(() => {
    if (!isDemo) return;
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
      toast.success('Phone verified — this call is now yours');
    };
    websocket.on('demo_phone_verified', onVerified);
    return () => {
      active = false;
      websocket.off('demo_phone_verified', onVerified);
    };
  }, [isDemo]);

  if (!isDemo) return null;

  const personaName = user?.name ?? user?.email ?? 'demo agent';
  const phoneNumbers = runtimeConfig?.demo_phone_numbers ?? [];

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
        You're signed in as{' '}
        <span className="text-ink font-medium">{personaName}</span> in a shared
        sandbox. Nightly reset.
      </span>

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
          title="Verify your phone so your calls are private and the AI can call you back"
        >
          <ShieldCheck className="h-3 w-3" />
          Verify your phone
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
        title="Release your demo agent back to the pool"
      >
        <LogOut className="h-3 w-3" />
        Leave demo
      </button>
    </div>
  );
}
