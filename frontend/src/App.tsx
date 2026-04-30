import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { useAuthStore } from './stores/authStore';
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

          {/* Supervisor View (integrated) */}
          <Route
            path="/supervisor"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Settings View */}
          <Route
            path="/settings"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* Admin Settings (standalone fallback) */}
          <Route
            path="/admin"
            element={
              <ProtectedRoute>
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
