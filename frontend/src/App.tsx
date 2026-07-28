import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { useAuthStore } from './stores/authStore';
import { applyBranding } from './lib/branding';
import { ADMIN_SURFACE_ROLES, FULL_ADMIN_ROLES, SUPERVISORY_ROLES } from './lib/roles';
import { SocketProvider } from './contexts/SocketContext';
import { CallFabricProvider } from './contexts/CallFabricContext';
import { UnifiedAgentDesktop } from './pages/UnifiedAgentDesktop';
import Admin from './pages/Admin';
import Login from './pages/Login';
import Register from './pages/Register';
import CallDetails from './components/CallDetails';
import ProtectedRoute from './components/ProtectedRoute';

function App() {
  const { checkAuth, fetchRuntimeConfig } = useAuthStore();
  const runtimeConfig = useAuthStore((s) => s.runtimeConfig);

  // Apply white-label branding (IMP-02) whenever runtime config (re)loads —
  // including after an admin saves the Branding tab, which refetches it.
  useEffect(() => {
    applyBranding(runtimeConfig?.branding ?? null);
  }, [runtimeConfig]);

  // Viewport-relative scale. The UI is designed at a 1440px reference width;
  // on wider monitors we zoom the whole document to fill the viewport at the
  // SAME proportions (thicker top bar, larger type, rail uses real width)
  // instead of spraying tiny elements edge-to-edge. Clamped so it never
  // shrinks below the native design and never blows up past 1.6x on ultra-wides.
  useEffect(() => {
    const DESIGN_WIDTH = 1440;
    const root = document.getElementById('root');
    const applyScale = () => {
      const z = Math.max(1, Math.min(window.innerWidth / DESIGN_WIDTH, 1.6));
      // `zoom` reflows (unlike transform: scale) and is supported in the
      // Chromium engines this app targets.
      (document.documentElement.style as any).zoom = String(z);
      // Chromium quirk: under `zoom`, `100vh` (Tailwind h-screen) resolves to
      // the PHYSICAL viewport height and then gets scaled by the zoom — i.e.
      // it comes out z× too tall, pushing centered content below the fold.
      // Pin #root to the TRUE layout height (physical ÷ z) so the h-full
      // shells fill exactly one screen. Shells must use h-full, NOT h-screen.
      if (root) root.style.height = `${window.innerHeight / z}px`;
    };
    applyScale();
    window.addEventListener('resize', applyScale);
    return () => window.removeEventListener('resize', applyScale);
  }, []);

  useEffect(() => {
    // Fetch runtime config first so the Login route knows whether to
    // show the production form or the hosted-demo landing card. Both
    // calls run in parallel; neither blocks the other.
    fetchRuntimeConfig();
    checkAuth();
  }, [checkAuth, fetchRuntimeConfig]);

  return (
    <SocketProvider>
      <CallFabricProvider>
      <Router future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />

          {/* === UNIFIED AGENT DESKTOP (Primary Interface) === */}

          {/* Contacts View (Default) */}
          <Route
            path="/"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />
          <Route
            path="/contacts"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />
          <Route
            path="/contacts/:contactId"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Active Calls View */}
          <Route
            path="/calls"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />
          <Route
            path="/calls/:callId"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Queue View */}
          <Route
            path="/queue"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Callbacks View (Tier 2r) */}
          <Route
            path="/callbacks"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Supervisor View (integrated) — supervisory roles only. FE-01
              audit followup (2026-06-02): previously rendered for any logged-in
              user; backend RBAC still gates the supervisor APIs but the screen
              itself shouldn't show its buttons to agents. */}
          <Route
            path="/supervisor"
            element={
              <ProtectedRoute requireRole={[...SUPERVISORY_ROLES]}>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Settings View — the admin surface. Hosted 'visitor's belong here:
              queues, knowledge base and branding are their demo to configure.
              The admin-MANAGEMENT parts of the panel gate on isFullAdmin
              separately (HIGH-3), so a visitor sees no control they can't use. */}
          <Route
            path="/settings"
            element={
              <ProtectedRoute requireRole={[...ADMIN_SURFACE_ROLES]}>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Admin Settings (standalone fallback) — full admins only. Unlike
              /settings this legacy screen has no visitor-safe subset. */}
          <Route
            path="/admin"
            element={
              <ProtectedRoute requireRole={[...FULL_ADMIN_ROLES]}>
                <Admin />
              </ProtectedRoute>
            }
          />

          {/* Call Details */}
          <Route
            path="/call/:callSid"
            element={
              <ProtectedRoute>
                <CallDetails />
              </ProtectedRoute>
            }
          />

          {/* Redirect old /dashboard to unified interface */}
          <Route path="/dashboard" element={<Navigate to="/" replace />} />
          <Route path="/dashboard/:callId" element={<Navigate to="/calls" replace />} />

          {/* Catch-all redirect */}
          <Route path="*" element={<Navigate to="/" replace />} />
        </Routes>
      </Router>
      </CallFabricProvider>
      <Toaster
        position="top-right"
        toastOptions={{
          duration: 4000,
          style: {
            background: '#363636',
            color: '#fff',
          },
          success: {
            style: {
              background: '#22c55e',
            },
          },
          error: {
            style: {
              background: '#ef4444',
            },
          },
        }}
      />
    </SocketProvider>
  );
}

export default App;
