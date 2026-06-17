import { useState } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { AlertCircle, Eye, EyeOff, Loader2 } from 'lucide-react';
import toast from 'react-hot-toast';
import { BrandMark, useBrand } from '../components/shared/Brand';

export default function Register() {
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [showPassword, setShowPassword] = useState(false);
  const [showConfirmPassword, setShowConfirmPassword] = useState(false);
  const { register, isLoading, error } = useAuthStore();
  const { productName, isWhiteLabeled } = useBrand();
  const navigate = useNavigate();

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (password !== confirmPassword) {
      toast.error('Passwords do not match');
      return;
    }
    if (password.length < 8) {
      toast.error('Password must be at least 8 characters long');
      return;
    }
    try {
      await register(email, password);
      toast.success('Account created');
      navigate('/');
    } catch {
      toast.error('Registration failed. Please try again.');
    }
  };

  return (
    <div className="relative h-full bg-canvas flex items-center justify-center p-6 overflow-hidden">
      <div className="absolute -top-40 -right-40 w-[520px] h-[520px] rounded-full opacity-[0.18] blur-[140px] pointer-events-none"
           style={{ background: 'radial-gradient(closest-side, #044EF4, transparent 70%)' }} />
      <div className="absolute -bottom-40 -left-40 w-[520px] h-[520px] rounded-full opacity-[0.10] blur-[160px] pointer-events-none"
           style={{ background: 'radial-gradient(closest-side, #F72A72, transparent 70%)' }} />

      <div className="absolute top-6 left-6 kicker text-ink-faint pointer-events-none">
        {isWhiteLabeled ? 'powered by signalwire' : 'signalwire / cf'}
      </div>
      <div className="absolute bottom-6 right-6 mono text-[9.5px] text-ink-faint uppercase tracking-[0.3em] pointer-events-none">secure channel</div>

      <div className="relative z-10 w-full max-w-md animate-fade-up">
        <div className="flex items-center gap-3 mb-8">
          <BrandMark size="lg" />
          <div className="leading-none">
            <div className="font-heading text-[26px] text-ink font-semibold tracking-heading">{productName}</div>
            <div className="kicker mt-1">Call Center</div>
          </div>
        </div>

        <div className="panel rounded-md shadow-panel p-8">
          <div className="mb-6">
            <div className="kicker mb-1">Register</div>
            <h2 className="font-heading text-[30px] text-ink font-semibold leading-[1.1] tracking-heading">
              Join the fabric.
            </h2>
            <p className="text-[13px] text-ink-muted mt-2">
              Create an account to take calls, watch queues, and configure AI agents.
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
              <label htmlFor="password" className="block kicker mb-1.5">Password</label>
              <div className="relative">
                <input
                  id="password"
                  name="password"
                  type={showPassword ? 'text' : 'password'}
                  autoComplete="new-password"
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
              <p className="mt-1 text-[11px] text-ink-dim">Minimum 8 characters.</p>
            </div>

            <div>
              <label htmlFor="confirmPassword" className="block kicker mb-1.5">Confirm</label>
              <div className="relative">
                <input
                  id="confirmPassword"
                  name="confirmPassword"
                  type={showConfirmPassword ? 'text' : 'password'}
                  autoComplete="new-password"
                  required
                  value={confirmPassword}
                  onChange={(e) => setConfirmPassword(e.target.value)}
                  className="input pr-10 mono"
                  placeholder="••••••••"
                />
                <button
                  type="button"
                  onClick={() => setShowConfirmPassword(!showConfirmPassword)}
                  className="absolute inset-y-0 right-0 pr-2.5 flex items-center text-ink-dim hover:text-ink transition-colors"
                  tabIndex={-1}
                >
                  {showConfirmPassword ? <EyeOff className="h-4 w-4" /> : <Eye className="h-4 w-4" />}
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
                  Creating account…
                </>
              ) : (
                'Create account'
              )}
            </button>
          </form>

          <div className="mt-6 pt-5 border-t border-rule text-center">
            <span className="text-[12.5px] text-ink-dim">
              Already have one?{' '}
              <Link to="/login" className="text-ink hover:text-sw-fuchsia font-medium transition-colors">
                Sign in
              </Link>
            </span>
          </div>
        </div>
      </div>
    </div>
  );
}
