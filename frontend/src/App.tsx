import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { useAuthStore } from './stores/authStore';
import { SocketProvider } from './contexts/SocketContext';
import { CallFabricProvider } from './contexts/CallFabricContext';
import { UnifiedAgentDesktop } from './pages/UnifiedAgentDesktop';
import { AgentDashboard } from './pages/AgentDashboard';
import { SupervisorDashboard } from './pages/SupervisorDashboard';
import { CallCenterDashboard } from './pages/CallCenterDashboard';
import Dashboard from './pages/Dashboard';
import Login from './pages/Login';
import Register from './pages/Register';
import CallDetails from './components/CallDetails';
import ProtectedRoute from './components/ProtectedRoute';

function App() {
  const { checkAuth } = useAuthStore();

  useEffect(() => {
    checkAuth();
  }, [checkAuth]);

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

          {/* Supervisor View (integrated) */}
          <Route
            path="/supervisor"
            element={
              <ProtectedRoute>
                <UnifiedAgentDesktop />
              </ProtectedRoute>
            }
          />

          {/* === LEGACY ROUTES (for reference/transition) === */}

          {/* Legacy Agent Dashboard */}
          <Route
            path="/legacy-dashboard"
            element={
              <ProtectedRoute>
                <AgentDashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/legacy-dashboard/:callId"
            element={
              <ProtectedRoute>
                <AgentDashboard />
              </ProtectedRoute>
            }
          />

          {/* Legacy Supervisor Dashboard */}
          <Route
            path="/legacy-supervisor"
            element={
              <ProtectedRoute>
                <SupervisorDashboard />
              </ProtectedRoute>
            }
          />

          {/* Old 3-Zone Dashboard (Legacy) */}
          <Route
            path="/old-dashboard"
            element={
              <ProtectedRoute>
                <CallCenterDashboard />
              </ProtectedRoute>
            }
          />

          {/* Original Transcribe App Dashboard */}
          <Route
            path="/transcribe"
            element={
              <ProtectedRoute>
                <Dashboard />
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
