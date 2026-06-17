// Brand-aware identity components (IMP-02 white-label).
//
// useBrand() reads the runtime config the app already fetches at boot
// (authStore.fetchRuntimeConfig → GET /api/config/runtime), so branding
// works on pre-auth surfaces like the login page. When no branding is
// configured everything renders stock SignalWire.

import { useAuthStore } from '../../stores/authStore';
import type { Branding } from '../../lib/branding';
import Logo from './Logo';

export function useBrand() {
  const branding = useAuthStore((s) => s.runtimeConfig?.branding) as
    | Branding
    | null
    | undefined;
  const enabled = !!branding?.enabled;
  return {
    branding: branding ?? null,
    isWhiteLabeled: enabled,
    productName: (enabled && branding?.product_name) || 'SignalWire',
    logoUrl: (enabled && branding?.logo_url) || null,
  };
}

const markSizeMap = { xs: 18, sm: 22, md: 26, lg: 40 } as const;

/** Brand mark: the tenant logo when configured, SignalWire brackets otherwise. */
export function BrandMark({
  size = 'md',
  className = '',
}: {
  size?: keyof typeof markSizeMap;
  className?: string;
}) {
  const { logoUrl, productName } = useBrand();
  if (logoUrl) {
    return (
      <img
        src={logoUrl}
        alt={productName}
        style={{ height: markSizeMap[size], width: 'auto' }}
        className={className}
      />
    );
  }
  return <Logo size={size} className={className} />;
}

/** Attribution shown on white-labeled surfaces. Renders nothing on stock. */
export function PoweredBySignalWire({ className = '' }: { className?: string }) {
  const { isWhiteLabeled } = useBrand();
  if (!isWhiteLabeled) return null;
  return (
    <span
      className={`inline-flex items-center gap-1.5 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] ${className}`}
    >
      powered by signalwire
    </span>
  );
}
