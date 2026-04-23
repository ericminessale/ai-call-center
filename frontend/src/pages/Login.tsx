import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { AlertCircle, Eye, EyeOff, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import Logo from '../components/shared/Logo';

export default function Login() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const { login, isLoading, error } = useAuthStore();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await login(email, password);
      toast.success('Welcome back');
      navigate('/');
    } catch {
      toast.error('Login failed. Check your credentials.');
    }
  };

  return (
    <div className="relative min-h-screen bg-canvas flex items-center justify-center p-6 overflow-hidden">
      {/* Atmospheric background — subtle blue glow (brand structural accent) */}
      <div className="absolute -top-40 -right-40 w-[520px] h-[520px] rounded-full opacity-[0.18] blur-[140px] pointer-events-none"
           style={{ background: 'radial-gradient(closest-side, #044EF4, transparent 70%)' }} />
      <div className="absolute -bottom-40 -left-40 w-[520px] h-[520px] rounded-full opacity-[0.10] blur-[160px] pointer-events-none"
           style={{ background: 'radial-gradient(closest-side, #F72A72, transparent 70%)' }} />

      {/* Corner crosshairs */}
      <div className="absolute top-6 left-6 kicker text-ink-faint pointer-events-none">signalwire / cf</div>
      <div className="absolute top-6 right-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">
        <span className="inline-flex items-center gap-1.5">
          <span className="w-1 h-1 rounded-full bg-live animate-pulse" />
          ready
        </span>
      </div>
      <div className="absolute bottom-6 left-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">v1.0</div>
      <div className="absolute bottom-6 right-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">secure channel</div>

      {/* Card */}
      <div className="relative z-10 w-full max-w-md animate-fade-up">
        <div className="flex items-center gap-3 mb-8">
          <Logo size="lg" />
          <div className="leading-none">
            <div className="font-heading text-[26px] text-ink font-semibold tracking-heading">SignalWire</div>
            <div className="kicker mt-1">Call Center</div>
          </div>
        </div>

        <div className="panel rounded-md shadow-panel p-8">
          <div className="mb-6">
            <div className="kicker mb-1">Sign in</div>
            <h2 className="font-heading text-[30px] text-ink font-semibold leading-[1.1] tracking-heading">
              Welcome back.
            </h2>
            <p className="text-[13px] text-ink-muted mt-2">
              Sign in to take calls, monitor queues, and configure the fabric.
            </p>
          </div>

          {error && (
            <div className="mb-5 flex items-center gap-2 p-3 bg-urgent/10 border border-urgent/30 rounded">
              <AlertCircle className="h-4 w-4 text-urgent-soft flex-shrink-0" />
              <span className="text-[12.5px] text-urgent-soft">{error}</span>
            </div>
          )}

          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label htmlFor="email" className="block kicker mb-1.5">Email</label>
              <input
                id="email"
                name="email"
                type="email"
                autoComplete="email"
                required
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="input mono"
                placeholder="you@signalwire.com"
              />
            </div>

            <div>
              <div className="flex items-center justify-between mb-1.5">
                <label htmlFor="password" className="block kicker">Password</label>
                <Link
                  to="/forgot-password"
                  className="text-[11.5px] text-sw-turquoise hover:text-sw-fuchsia transition-colors"
                >
                  Forgot?
                </Link>
              </div>
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="current-password"
                  required
                  value={password}
                  onChange={(e) => setPassword(e.target.value)}
                  className="input pr-10 mono"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowPassword(!showPassword)}
                  className="absolute inset-y-0 right-0 pr-2.5 flex items-center text-ink-dim hover:text-ink transition-colors"
                  tabIndex={-1}
                >
                  {showPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
                </button>
              </div>
            </div>

            <button
              type="submit"
              disabled={isLoading}
              className="btn-primary w-full justify-center !py-2.5 mt-2"
            >
              {isLoading ? (
                <>
                  <Loader2 className="h-4 w-4 animate-spin" />
                  Signing in…
                </>
              ) : (
                'Sign in'
              )}
            </button>
          </form>

          <div className="mt-6 pt-5 border-t border-rule text-center">
            <span className="text-[12.5px] text-ink-dim">
              New here?{' '}
              <Link to="/register" className="text-sw-turquoise hover:text-sw-fuchsia font-medium transition-colors">
                Create an account
              </Link>
            </span>
          </div>
        </div>

        {/* Tagline */}
        <p className="text-center mt-6 text-[11.5px] text-ink-dim">
          AI-first voice. Humans when it matters.
        </p>
      </div>
    </div>
  );
}
