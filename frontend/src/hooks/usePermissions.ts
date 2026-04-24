/**
 * Capability-flag gate for UI surfaces.
 *
 * Consumers should treat the return value as read-only truth. Backend still
 * enforces permissions on every endpoint — the hook only decides what to
 * render, not what to allow.
 *
 * Usage:
 *   const { can } = usePermissions();
 *   if (can('can_listen_ai_calls')) { ... }
 */
import { useAuthStore } from '../stores/authStore';
import type { PermissionFlag } from '../types';

export function usePermissions() {
  const user = useAuthStore((s) => s.user);
  const map = user?.effective_permissions ?? {};

  const can = (flag: PermissionFlag): boolean => Boolean(map[flag]);

  const canAny = (...flags: PermissionFlag[]): boolean =>
    flags.some((f) => Boolean(map[f]));

  const canAll = (...flags: PermissionFlag[]): boolean =>
    flags.every((f) => Boolean(map[f]));

  return { can, canAny, canAll, permissions: map };
}
