import { useNavigate } from 'react-router-dom';
import { Loader2, Phone, Sparkles } from 'lucide-react';
import toast from 'react-hot-toast';
import { useAuthStore } from '../stores/authStore';
import { BrandMark, useBrand } from '../components/shared/Brand';

/**
 * Hosted-demo landing card.
 *
 * Replaces the production Login page when ``DEMO_MODE=true`` on the
 * backend. One-click "Start demo" provisions (or resumes) the visitor's
 * own private WORKSPACE via ``POST /api/demo/start`` and lands them in
 * the agent dashboard as its admin — no email, no password.
 *
 * The pitch is verify-first (§6.1): Start → text your pairing code →
 * then dial. There is deliberately no "dial right now" framing — the
 * workspace accepts inbound only from the visitor's verified number.
 *
 * The featured phone numbers come from the backend's runtime config
 * (env-driven via ``DEMO_PHONE_NUMBERS``). When unconfigured we just
 * skip the phone/verify steps and let the visitor explore the dashboard.
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
  const ttlDays = runtimeConfig?.workspace_ttl_days ?? 7;
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
              Your own AI call center, in one click.
            </h2>
            <p className="text-[13px] text-ink-muted mt-2 leading-relaxed">
              Start a private workspace — your own queues, knowledge base, and
              AI receptionist, yours to reconfigure as the admin.
              {phoneNumbers.length > 0 ? (
                <>
                  {' '}Link your phone and call in: the AI answers for your
                  workspace, routes the call to you at the agent desk, and you
                  watch transcription, sentiment, and routing decisions live.
                </>
              ) : (
                <>
                  {' '}Explore the agent desktop, tune the queues and knowledge
                  base, and see how AI-first routing is put together.
                </>
              )}
            </p>
          </div>

          {/* Verify-first onboarding: calling requires pairing your phone
              BEFORE any call — the workspace accepts inbound only from, and
              dials outbound only to, the visitor's verified number. */}
          {phoneNumbers.length > 0 && (
            <div className="mb-6 space-y-2.5">
              <div className="kicker">How it works</div>
              {[
                {
                  n: 1,
                  text: 'Start your workspace — instant, no signup.',
                },
                {
                  n: 2,
                  text: 'Text the pairing code from the dashboard to the demo number. That links your phone to your workspace.',
                },
                {
                  n: 3,
                  text: 'Call the demo number — your AI receptionist answers, and the call is yours end to end.',
                },
              ].map((step) => (
                <div key={step.n} className="flex items-start gap-3">
                  <span className="mt-0.5 flex h-5 w-5 flex-shrink-0 items-center justify-center rounded-full border border-rule font-mono text-[10.5px] text-ai-soft">
                    {step.n}
                  </span>
                  <span className="text-[12.5px] text-ink-muted leading-relaxed">
                    {step.text}
                  </span>
                </div>
              ))}
            </div>
          )}

          {phoneNumbers.length > 0 && (
            <div className="mb-6 space-y-2">
              <div className="kicker">
                {phoneNumbers.length === 1 ? 'Demo number' : 'Demo numbers'}
              </div>
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
              <p className="text-[11px] text-ink-faint leading-relaxed">
                Calls are accepted only from your verified phone — nobody else
                can reach your workspace.
              </p>
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
              Your workspace is private and lives for {ttlDays}{' '}
              {ttlDays === 1 ? 'day' : 'days'} — each visit from this browser
              extends it. No account, no email. Keep this browser to pick up
              where you left off.
            </p>
          </div>
        </div>

        <p className="text-center mt-6 text-[11.5px] text-ink-dim">
          AI-first voice. Humans when it matters.
        </p>
        <p className="text-center mt-2 text-[10.5px]">
          <a
            href="/login?operator=1"
            className="text-ink-faint hover:text-ink-muted transition-colors"
          >
            Operator sign-in
          </a>
        </p>
      </div>
    </div>
  );
}
