import { useState } from 'react';
import {
  Loader2, Lock, MessageSquare, PhoneIncoming, PhoneCall, ShieldCheck,
} from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../../stores/authStore';
import { useVerifyStore } from '../../stores/verifyStore';

/**
 * Prominent hosted-demo phone-verification card. Renders below the demo
 * banner while the visitor is UNVERIFIED (the banner shows a compact
 * "Verified" badge once done, so this card unmounts on success).
 *
 * Two steps:
 *   1. Explain what's locked and offer a pairing code.
 *   2. Show the code big, deep-link an SMS to the demo number, and wait
 *      live for the inbound-SMS webhook to flip verification (the store's
 *      markVerified, driven by the demo_phone_verified socket event).
 *
 * Demo-only; nothing here ships to a single-tenant deployment (the parent
 * gates on runtimeConfig.demo_mode).
 */
export function PhoneVerificationCard() {
  const runtimeConfig = useAuthStore((s) => s.runtimeConfig);
  const code = useVerifyStore((s) => s.code);
  const requesting = useVerifyStore((s) => s.requesting);
  const requestCode = useVerifyStore((s) => s.requestCode);
  const [regenerating, setRegenerating] = useState(false);

  const demoNumber = runtimeConfig?.demo_phone_numbers?.[0]?.number ?? null;

  const getCode = async () => {
    try {
      await requestCode();
    } catch {
      toast.error('Could not generate a code — try again.');
    }
  };

  const regenCode = async () => {
    setRegenerating(true);
    try {
      await requestCode();
    } catch {
      toast.error('Could not generate a code — try again.');
    } finally {
      setRegenerating(false);
    }
  };

  return (
    <div className="border-b border-rule bg-canvas-sunken px-4 py-4 sm:px-6">
      <div className="mx-auto flex max-w-3xl flex-col gap-4 sm:flex-row sm:items-start sm:gap-6">
        {/* Left: what & why */}
        <div className="flex-1 min-w-0">
          <div className="kicker mb-1 inline-flex items-center gap-1.5 text-ai-soft">
            <ShieldCheck className="h-3 w-3" />
            {code ? 'Verify · step 2 of 2' : 'One step to go live'}
          </div>
          <h2 className="font-heading text-[17px] font-semibold text-ink leading-snug tracking-heading">
            {code ? 'Text this code to link your phone' : 'Verify your phone to unlock calling'}
          </h2>
          <p className="mt-1 text-[12.5px] text-ink-muted leading-relaxed">
            Your workspace is live, but calls stay locked until you link the
            phone you'll use — it's how the demo keeps your calls private to you.
          </p>

          {/* Locked-until-verified feature list */}
          <ul className="mt-3 space-y-1.5">
            <li className="flex items-center gap-2 text-[12px] text-ink-muted">
              <Lock className="h-3 w-3 flex-shrink-0 text-ink-faint" />
              <PhoneIncoming className="h-3.5 w-3.5 flex-shrink-0 text-ai-soft" />
              Receive calls — the AI answers the demo number for your workspace
              and routes the call to you.
            </li>
            <li className="flex items-center gap-2 text-[12px] text-ink-muted">
              <Lock className="h-3 w-3 flex-shrink-0 text-ink-faint" />
              <PhoneCall className="h-3.5 w-3.5 flex-shrink-0 text-ai-soft" />
              Have the AI call you back on your verified number.
            </li>
          </ul>
        </div>

        {/* Right: the action */}
        <div className="w-full sm:w-[320px] sm:flex-shrink-0">
          {!code ? (
            <button
              type="button"
              onClick={getCode}
              disabled={requesting}
              className="btn-primary w-full justify-center !py-2.5"
            >
              {requesting ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Generating…
                </>
              ) : (
                'Get my pairing code'
              )}
            </button>
          ) : (
            <div className="rounded-md border border-rule bg-canvas p-3.5">
              {/* Big per-digit code */}
              <div className="flex justify-center gap-1.5" aria-label={`Pairing code ${code}`}>
                {code.split('').map((d, i) => (
                  <span
                    key={i}
                    className="flex h-11 w-8 items-center justify-center rounded border border-rule bg-canvas-sunken font-mono text-[22px] font-semibold text-ai"
                  >
                    {d}
                  </span>
                ))}
              </div>

              {/* Deep-link SMS to the demo number */}
              <div className="mt-3">
                {demoNumber ? (
                  <a
                    href={`sms:${demoNumber}?&body=${code}`}
                    className="btn-primary flex w-full items-center justify-center gap-1.5 !py-2.5"
                  >
                    <MessageSquare className="h-4 w-4" />
                    Text the code
                  </a>
                ) : (
                  <p className="text-center text-[12px] text-ink-muted">
                    Text <span className="font-mono text-ai">{code}</span> to the demo number.
                  </p>
                )}
                {demoNumber && (
                  <p className="mt-1.5 text-center text-[11px] text-ink-faint">
                    to <span className="font-mono text-ink-muted">{demoNumber}</span>
                  </p>
                )}
              </div>

              {/* Live waiting indicator + regenerate */}
              <div className="mt-3 flex items-center justify-between border-t border-rule pt-2.5">
                <span className="inline-flex items-center gap-1.5 text-[11.5px] text-ink-muted">
                  <span className="relative flex h-2 w-2">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-live opacity-60" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-live" />
                  </span>
                  Waiting for your text…
                </span>
                <button
                  type="button"
                  onClick={regenCode}
                  disabled={regenerating}
                  className="text-[11.5px] text-ink-faint hover:text-ink-muted transition-colors disabled:opacity-50"
                >
                  {regenerating ? 'Refreshing…' : 'Get a new code'}
                </button>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
