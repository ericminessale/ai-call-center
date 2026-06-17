import { useNavigate } from 'react-router-dom';
import { Loader2, Phone, Sparkles } from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../stores/authStore';
import { BrandMark, useBrand } from '../components/shared/Brand';

/**
 * Hosted-demo landing card.
 *
 * Replaces the production Login page when ``DEMO_MODE=true`` on the
 * backend. One-click "Start Demo" issues a JWT for a demo persona via
 * ``POST /api/demo/start`` and lands the visitor in the agent
 * dashboard already-online — no email, no password.
 *
 * The featured phone numbers come from the backend's runtime config
 * (env-driven via ``DEMO_PHONE_NUMBERS``). When unconfigured we just
 * skip the phone-number list and let the visitor explore the dashboard.
 *
 * In production-shape clone-and-own deployments this component is
 * never rendered — Login.tsx checks ``runtimeConfig.demo_mode`` and
 * shows the normal login form instead.
 */
export default function DemoLanding() {
  const { startDemoSession, isLoading, runtimeConfig } = useAuthStore();
  const navigate = useNavigate();

  const handleStart = async () => {
    try {
      await startDemoSession();
      toast.success('Welcome to the demo');
      navigate('/');
    } catch {
      toast.error('Could not start demo session — please try again.');
    }
  };

  const phoneNumbers = runtimeConfig?.demo_phone_numbers ?? [];
  const { productName, isWhiteLabeled } = useBrand();

  return (
    <div className="relative h-full bg-canvas flex items-center justify-center p-6 overflow-hidden">
      {/* Atmospheric background — same blue/fuchsia glow as Login */}
      <div
        className="absolute -top-40 -right-40 w-[520px] h-[520px] rounded-full opacity-[0.18] blur-[140px] pointer-events-none"
        style={{ background: 'radial-gradient(closest-side, #044EF4, transparent 70%)' }}
      />
      <div
        className="absolute -bottom-40 -left-40 w-[520px] h-[520px] rounded-full opacity-[0.10] blur-[160px] pointer-events-none"
        style={{ background: 'radial-gradient(closest-side, #F72A72, transparent 70%)' }}
      />

      <div className="absolute top-6 left-6 kicker text-ink-faint pointer-events-none">
        {isWhiteLabeled ? 'powered by signalwire' : 'signalwire / cf'}
      </div>
      <div className="absolute top-6 right-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">
        <span className="inline-flex items-center gap-1.5">
          <span className="w-1 h-1 rounded-full bg-live animate-pulse" />
          live demo
        </span>
      </div>
      <div className="absolute bottom-6 left-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">
        v1.0
      </div>
      <div className="absolute bottom-6 right-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">
        no signup required
      </div>

      <div className="relative z-10 w-full max-w-lg animate-fade-up">
        <div className="flex items-center gap-3 mb-8">
          <BrandMark size="lg" />
          <div className="leading-none">
            <div className="font-heading text-[26px] text-ink font-semibold tracking-heading">
              {productName}
            </div>
            <div className="kicker mt-1">Call Center · Live Demo</div>
          </div>
        </div>

        <div className="panel rounded-md shadow-panel p-8">
          <div className="mb-6">
            <div className="kicker mb-1 inline-flex items-center gap-1.5">
              <Sparkles className="h-3 w-3" />
              Try it
            </div>
            <h2 className="font-heading text-[30px] text-ink font-semibold leading-[1.1] tracking-heading">
              See an AI call center in action.
            </h2>
            <p className="text-[13px] text-ink-muted mt-2 leading-relaxed">
              You'll be dropped into the agent dashboard as one of our demo
              agents. Dial the number below from your phone — the AI receptionist
              picks up, gathers context, and routes the call. You'll see it all
              live: live transcription, sentiment, AI tools invoked mid-call,
              the queue, the works.
            </p>
          </div>

          {phoneNumbers.length > 0 && (
            <div className="mb-6 space-y-2">
              <div className="kicker">Demo phone numbers</div>
              {phoneNumbers.map((p) => (
                <div
                  key={p.number}
                  className="flex items-center justify-between gap-3 rounded border border-rule bg-canvas-sunken px-3 py-2.5"
                >
                  <div className="flex items-center gap-2.5 min-w-0">
                    <Phone className="h-4 w-4 text-ai-soft flex-shrink-0" />
                    <span className="text-[13px] text-ink truncate">{p.label}</span>
                  </div>
                  <a
                    href={`tel:${p.number}`}
                    className="font-mono text-[13px] text-ai-soft hover:text-ai transition-colors whitespace-nowrap"
                  >
                    {p.number}
                  </a>
                </div>
              ))}
            </div>
          )}

          <button
            type="button"
            onClick={handleStart}
            disabled={isLoading}
            className="btn-primary w-full justify-center !py-3 mt-2"
          >
            {isLoading ? (
              <>
                <Loader2 className="h-4 w-4 animate-spin" />
                Starting demo…
              </>
            ) : (
              'Start demo'
            )}
          </button>

          <div className="mt-6 pt-5 border-t border-rule text-center">
            <p className="text-[11.5px] text-ink-dim leading-relaxed">
              This is a shared sandbox. Outbound dialing is disabled, and the
              database resets nightly. No account, no email — just the platform.
            </p>
          </div>
        </div>

        <p className="text-center mt-6 text-[11.5px] text-ink-dim">
          AI-first voice. Humans when it matters.
        </p>
      </div>
    </div>
  );
}
