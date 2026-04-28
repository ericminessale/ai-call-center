import { Phone, Sparkles } from 'lucide-react';
import { useAuthStore } from '../../stores/authStore';

/**
 * Persistent strip across the top of the agent dashboard, only
 * rendered when the backend's runtime config reports DEMO_MODE=true.
 *
 * Surfaces:
 *   - the "DEMO" label so visitors know they're not in a real
 *     production call center
 *   - the visitor's leased agent persona name (so they understand
 *     which agent identity they're operating as)
 *   - the demo phone numbers, dial-able with one click on mobile
 *
 * Renders nothing in production-shape clone-and-own deployments.
 * Add to the top of any layout shell that hosts the dashboard.
 */
export function DemoBanner() {
  const { runtimeConfig, user } = useAuthStore();

  if (!runtimeConfig?.demo_mode) return null;

  const personaName = user?.name ?? user?.email ?? 'demo agent';
  const phoneNumbers = runtimeConfig.demo_phone_numbers ?? [];

  return (
    <div
      className="relative z-30 flex items-center gap-3 px-4 py-2 bg-ai/10 border-b border-ai/30 text-[12px] text-ink"
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
        sandbox. Outbound dial is disabled; nightly reset.
      </span>

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
    </div>
  );
}
