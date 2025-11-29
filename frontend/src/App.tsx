import { useEffect } from 'react';
import { BrowserRouter as Router, Routes, Route, Navigate } from 'react-router-dom';
import { Toaster } from 'react-hot-toast';
import { useAuthStore } from './stores/authStore';
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
    <>
      <Router future={{
        v7_startTransition: true,
        v7_relativeSplatPath: true,
      }}>
        <Routes>
          <Route path="/login" element={<Login />} />
          <Route path="/register" element={<Register />} />

          {/* Agent Dashboard (Master-Detail Layout) */}
          <Route
            path="/dashboard"
            element={
              <ProtectedRoute>
                <AgentDashboard />
              </ProtectedRoute>
            }
          />
          <Route
            path="/dashboard/:callId"
            element={
              <ProtectedRoute>
                <AgentDashboard />
              </ProtectedRoute>
            }
          />

          {/* Supervisor Dashboard */}
          <Route
            path="/supervisor"
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

          <Route
            path="/call/:callSid"
            element={
              <ProtectedRoute>
                <CallDetails />
              </ProtectedRoute>
            }
          />

          {/* Default to new dashboard */}
          <Route path="/" element={<Navigate to="/dashboard" replace />} />
          <Route path="*" element={<Navigate to="/dashboard" replace />} />
        </Routes>
      </Router>
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
    </>
  );
}

export default App;