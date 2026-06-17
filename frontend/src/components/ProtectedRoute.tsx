import { Navigate } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { Loader2 } from 'lucide-react';

interface ProtectedRouteProps {
  children: React.ReactNode;
  // When set, the authenticated user's role must equal (string) or be
  // included in (array) this value. A mismatch redirects to "/" instead of
  // rendering the gated screen. This is a UI guard only — the backend still
  // enforces authorization; it stops admin/supervisor surfaces from showing
  // to roles that shouldn't see them.
  requireRole?: string | string[];
}

export default function ProtectedRoute({ children, requireRole }: ProtectedRouteProps) {
  const { isAuthenticated, isCheckingAuth, user } = useAuthStore();

  // Show loading while checking auth
  if (isCheckingAuth) {
    return (
      <div className="h-full bg-canvas flex items-center justify-center">
        <div className="text-center">
          <Loader2 className="h-8 w-8 animate-spin text-sw-fuchsia mx-auto mb-4" />
          <p className="text-ink-muted text-sm">Checking authentication…</p>
        </div>
      </div>
    );
  }

  // Only redirect after auth check is complete
  if (!isAuthenticated) {
    return <Navigate to="/login" replace />;
  }

  // Role gate: when a route declares requireRole, bounce a user without a
  // matching role to the default view rather than rendering the screen.
  if (requireRole) {
    const allowed = Array.isArray(requireRole) ? requireRole : [requireRole];
    const role = user?.role;
    if (!role || !allowed.includes(role)) {
      return <Navigate to="/" replace />;
    }
  }

  return <>{children}</>;
}
