import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Settings,
  Database,
  BookOpen,
  Save,
  Plus,
  Trash2,
  RefreshCw,
  FileText,
  Loader2,
  Check,
  AlertCircle,
  Info,
  Users,
  Phone,
  Copy,
  ExternalLink,
  LayoutList,
  Bot,
  Pencil,
  PhoneOutgoing,
  Upload,
  Plug,
  Activity,
  RotateCcw,
  Palette,
  Building2,
} from 'lucide-react';
import { adminApi } from '../../services/api';
import { useAuthStore } from '../../stores/authStore';
import toast from 'react-hot-toast';
import { ConfirmModal } from '../shared/ConfirmModal';
import { Chip, Checkbox } from '../restraint';
import { ExternalToolsTab } from './ExternalToolsTab';
import { WebhookLogTab } from './WebhookLogTab';
import { WorkspacesTab } from './WorkspacesTab';

// =============================================================================
// Shared Types
// =============================================================================

interface Agent {
  id: string;
  name: string;
  route: string;
  type?: 'inbound' | 'outbound';
  description: string;
}

interface Collection {
  id: number;
  name: string;
  display_name: string;
  description: string;
  document_count: number;
}

interface Document {
  id: number;
  collection_id: number;
  title: string;
  content: string;
  is_published: boolean;
  updated_at: string;
}

interface AdminUser {
  id: number;
  email: string;
  name: string | null;
  role: string;
  is_active: boolean;
  created_at: string | null;
  has_subscriber: boolean;
  signalwire_address: string | null;
  languages: string[];
  // Resolved permission map (role defaults merged with per-user overrides).
  effective_permissions?: Partial<Record<PermissionKey, boolean>>;
  // Explicit overrides only — empty means "use role defaults".
  permission_overrides?: Partial<Record<PermissionKey, boolean>>;
  // Agent Assist — Knowledge Factbook mode. See AGENT_ASSIST.md.
  kb_factbook_mode?: 'off' | 'manual' | 'auto';
  // Agent Assist — AI Coach style preset. Per-call mode lives on the live
  // call surface as an in-call toggle (gated by `can_use_coach` permission),
  // not here.
  coach_intensity?: 'terse' | 'standard' | 'verbose';
}

type FactbookMode = 'off' | 'manual' | 'auto';
type CoachIntensity = 'terse' | 'standard' | 'verbose';

// Keep in lockstep with PERMISSION_FLAGS in backend/app/models/user.py.
// Order controls the order in the edit modal.
type PermissionKey =
  | 'can_listen_ai_calls'
  | 'can_listen_human_calls'
  | 'can_whisper'
  | 'can_barge'
  | 'can_control_recording'
  | 'can_use_coach';

const PERMISSION_LABELS: Record<PermissionKey, { label: string; hint: string }> = {
  can_listen_ai_calls: {
    label: 'Listen to AI calls',
    hint: 'Silently monitor calls handled by an AI agent.',
  },
  can_listen_human_calls: {
    label: 'Listen to human calls',
    hint: 'Silently join an active agent\u2019s call as an observer.',
  },
  can_whisper: {
    label: 'Whisper to agent',
    hint: 'Coach an agent mid-call; only the agent hears you.',
  },
  can_barge: {
    label: 'Barge into call',
    hint: 'Insert yourself into an active call with full audio.',
  },
  can_control_recording: {
    label: 'Control recording',
    hint: 'Start or stop recording on calls they participate in.',
  },
  can_use_coach: {
    label: 'Use AI coach',
    hint: 'Attach the AI Coach sidecar on their own calls (per-call toggle; bills per minute while attached).',
  },
};

const PERMISSION_ORDER: PermissionKey[] = [
  'can_listen_ai_calls',
  'can_listen_human_calls',
  'can_whisper',
  'can_barge',
  'can_control_recording',
  'can_use_coach',
];

// BCP-47 language menu shown to admins. Keep small — these are the
// languages SignalWire's live_translate supports out of the box.
const SUPPORTED_LANGUAGES: { code: string; label: string }[] = [
  { code: 'en-US', label: 'English (US)' },
  { code: 'es-ES', label: 'Spanish' },
  { code: 'fr-FR', label: 'French' },
  { code: 'de-DE', label: 'German' },
  { code: 'it-IT', label: 'Italian' },
  { code: 'pt-BR', label: 'Portuguese (Brazil)' },
  { code: 'ja-JP', label: 'Japanese' },
  { code: 'zh-CN', label: 'Chinese (Mandarin)' },
  { code: 'ko-KR', label: 'Korean' },
  { code: 'ar-SA', label: 'Arabic' },
];

interface PhoneNumber {
  sid: string;
  phone_number: string;
  friendly_name: string;
  voice_url: string;
  status_callback: string;
  is_assigned: boolean;
  target_mode: 'ai_triage' | 'ai_specialist' | 'human_direct' | null;
  target_queue_slug: string | null;
  // URL-drift fields — set when the SignalWire webhook URL points at one of
  // OUR routes but under a stale host (typical cause: ngrok URL rotated and
  // SignalWire still has the old one). Backend recovers the original mode
  // from the path so we can offer a one-click re-sync.
  is_drifted?: boolean;
  drifted_target_mode?: 'ai_triage' | 'ai_specialist' | 'human_direct' | null;
  drifted_target_queue_slug?: string | null;
}

type PhoneTargetMode = 'ai_triage' | 'ai_specialist' | 'human_direct';

interface QueueConfig {
  id: number;
  slug: string;
  display_name: string;
  description: string | null;
  is_active: boolean;
  routing_strategy: string;
  // Call transport: 'conference' (default) or 'bridge'. See CALL_TRANSPORT.md.
  // Bridge mode: SWML `connect` direct to assigned agent; per-leg REST verbs
  // (hold, DTMF) operate natively. Conference mode: caller in interaction
  // conference; multi-party (whisper/barge/listen) available.
  routing_transport?: 'conference' | 'bridge';
  ai_agent_route: string | null;
  default_priority: number;
  sla_threshold_seconds: number;
  max_wait_before_ai_fallback: number;
  agent_count?: number;
}

interface QueueAgentAssignment {
  user_id: number;
  user_name: string | null;
  user_email: string;
  skill_level: number;
}

type SettingsTabId = 'phone-numbers' | 'queues' | 'agents' | 'knowledge' | 'external-tools' | 'branding' | 'users' | 'webhooks' | 'workspaces';


// =============================================================================
// Main Settings Panel
// =============================================================================

export function SettingsPanel() {
  const [activeTab, setActiveTab] = useState<SettingsTabId>('phone-numbers');
  // Cross-tab navigation: AgentsTab can request opening a collection in KnowledgeBaseTab
  const [focusCollectionId, setFocusCollectionId] = useState<number | null>(null);
  // Platform operator's workspace roster (Phase 5): only meaningful on a
  // hosted multi-workspace install, and only for platform-level admins
  // (workspace_id null). Clone-and-own admins are platform-level too, but
  // demo_mode is off there, so the tab stays hidden.
  const { user, runtimeConfig } = useAuthStore();
  const showWorkspacesTab =
    !!runtimeConfig?.demo_mode && user != null && user.workspace_id == null;

  const handleNavigateToCollection = (collectionId: number) => {
    setFocusCollectionId(collectionId);
    setActiveTab('knowledge');
  };

  const handleTabChange = (tab: SettingsTabId) => {
    if (tab !== 'knowledge') setFocusCollectionId(null);
    setActiveTab(tab);
  };

  return (
    <div className="h-full flex flex-col text-ink">
      {/* Page header — rs-h1 + subtitle on page bg (no raised band) */}
      <div className="px-7 pt-6 pb-4">
        <h1 className="text-[20px] font-semibold text-ink leading-none tracking-tight">Settings</h1>
        <p className="text-[11px] font-medium text-ink-dim mt-1.5">
          Phone numbers, queues, AI agents, knowledge, branding, and team.
        </p>
      </div>

      {/* Tab navigation */}
      <div className="px-7 border-b border-rule">
        <nav className="flex gap-1">
          <TabButton
            id="phone-numbers"
            icon={<Phone className="w-3.5 h-3.5" />}
            label="Phone Numbers"
            active={activeTab === 'phone-numbers'}
            onClick={() => handleTabChange('phone-numbers')}
          />
          <TabButton
            id="queues"
            icon={<LayoutList className="w-3.5 h-3.5" />}
            label="Queues"
            active={activeTab === 'queues'}
            onClick={() => handleTabChange('queues')}
          />
          <TabButton
            id="agents"
            icon={<Bot className="w-3.5 h-3.5" />}
            label="AI Agents"
            active={activeTab === 'agents'}
            onClick={() => handleTabChange('agents')}
          />
          <TabButton
            id="knowledge"
            icon={<BookOpen className="w-3.5 h-3.5" />}
            label="Knowledge Base"
            active={activeTab === 'knowledge'}
            onClick={() => handleTabChange('knowledge')}
          />
          <TabButton
            id="external-tools"
            icon={<Plug className="w-3.5 h-3.5" />}
            label="External Tools"
            active={activeTab === 'external-tools'}
            onClick={() => handleTabChange('external-tools')}
          />
          <TabButton
            id="branding"
            icon={<Palette className="w-3.5 h-3.5" />}
            label="Branding"
            active={activeTab === 'branding'}
            onClick={() => handleTabChange('branding')}
          />
          <TabButton
            id="users"
            icon={<Users className="w-3.5 h-3.5" />}
            label="User Management"
            active={activeTab === 'users'}
            onClick={() => handleTabChange('users')}
          />
          <TabButton
            id="webhooks"
            icon={<Activity className="w-3.5 h-3.5" />}
            label="Webhook Log"
            active={activeTab === 'webhooks'}
            onClick={() => handleTabChange('webhooks')}
          />
          {showWorkspacesTab && (
            <TabButton
              id="workspaces"
              icon={<Building2 className="w-3.5 h-3.5" />}
              label="Workspaces"
              active={activeTab === 'workspaces'}
              onClick={() => handleTabChange('workspaces')}
            />
          )}
        </nav>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto p-8 bg-canvas">
        {activeTab === 'phone-numbers' && <PhoneNumbersTab />}
        {activeTab === 'queues' && <QueuesTab />}
        {activeTab === 'agents' && <AgentsTab onNavigateToCollection={handleNavigateToCollection} />}
        {activeTab === 'knowledge' && <KnowledgeBaseTab focusCollectionId={focusCollectionId} />}
        {activeTab === 'external-tools' && <ExternalToolsTab />}
        {activeTab === 'branding' && <BrandingTab />}
        {activeTab === 'users' && <UserManagementTab />}
        {activeTab === 'webhooks' && <WebhookLogTab />}
        {activeTab === 'workspaces' && showWorkspacesTab && <WorkspacesTab />}
      </div>
    </div>
  );
}


function TabButton({ icon, label, active, onClick }: {
  id: string;
  icon: React.ReactNode;
  label: string;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`relative flex items-center gap-2 px-3 py-3 text-[13px] font-medium transition-colors ${
        active ? 'text-ink' : 'text-ink-dim hover:text-ink-muted'
      }`}
    >
      {icon}
      {label}
      {active && (
        <span className="absolute -bottom-[1px] left-2 right-2 h-[2px] rounded-sm bg-sw-fuchsia" />
      )}
    </button>
  );
}


// =============================================================================
// Tab: Branding (IMP-02 white-label)
// =============================================================================

interface BrandingForm {
  product_name: string;
  logo_url: string;
  color_primary: string;
  color_accent: string;
  color_highlight: string;
}

const EMPTY_BRANDING: BrandingForm = {
  product_name: '',
  logo_url: '',
  color_primary: '',
  color_accent: '',
  color_highlight: '',
};

const BRANDING_COLORS: Array<{
  key: keyof BrandingForm;
  label: string;
  hint: string;
  stock: string;
}> = [
  { key: 'color_primary', label: 'Primary', hint: 'Buttons, structure, focus ring', stock: '#044EF4' },
  { key: 'color_accent', label: 'Accent', hint: 'Stats, table headers, link hover', stock: '#F72A72' },
  { key: 'color_highlight', label: 'Highlight', hint: 'Links, active tab', stock: '#40E0D0' },
];

function BrandingTab() {
  const fetchRuntimeConfig = useAuthStore((s) => s.fetchRuntimeConfig);
  const [form, setForm] = useState<BrandingForm>(EMPTY_BRANDING);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    adminApi.getBranding()
      .then((res) => {
        const b = res.data.branding || {};
        setForm({
          product_name: b.product_name || '',
          logo_url: b.logo_url || '',
          color_primary: b.color_primary || '',
          color_accent: b.color_accent || '',
          color_highlight: b.color_highlight || '',
        });
      })
      .catch(() => toast.error('Failed to load branding'))
      .finally(() => setLoading(false));
  }, []);

  const save = async (next: BrandingForm) => {
    setSaving(true);
    try {
      await adminApi.updateBranding(next);
      // Re-fetching runtime config re-applies the CSS variables app-wide
      // (App.tsx effect) — the rebrand lands live, no rebuild.
      await fetchRuntimeConfig();
      toast.success('Branding applied live');
    } catch (e: any) {
      toast.error(e.response?.data?.error || 'Failed to save branding');
    } finally {
      setSaving(false);
    }
  };

  const resetToStock = async () => {
    setForm(EMPTY_BRANDING);
    await save(EMPTY_BRANDING);
  };

  if (loading) {
    return (
      <div className="flex items-center gap-2 text-ink-muted text-[13px]">
        <Loader2 className="w-4 h-4 animate-spin" />
        Loading branding…
      </div>
    );
  }

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h2 className="font-display text-[22px] text-ink leading-none mb-2">White-label branding</h2>
        <p className="text-[13px] text-ink-muted leading-relaxed">
          Rebrand the entire console at runtime — product name, logo, and brand colors apply
          live, with no rebuild or restart. Leave a field empty to keep the stock SignalWire
          value. The agent desktop, login page, and demo landing all follow.
        </p>
      </div>

      <div className="panel rounded-md p-6 space-y-5">
        <div>
          <label className="block kicker mb-1.5">Product name</label>
          <input
            value={form.product_name}
            onChange={(e) => setForm({ ...form, product_name: e.target.value })}
            placeholder="SignalWire"
            maxLength={60}
            className="input w-full"
          />
        </div>

        <div>
          <label className="block kicker mb-1.5">Logo URL</label>
          <input
            value={form.logo_url}
            onChange={(e) => setForm({ ...form, logo_url: e.target.value })}
            placeholder="https://example.com/logo.svg (optional)"
            className="input w-full"
          />
          <p className="text-[11.5px] text-ink-dim mt-1">
            Replaces the SignalWire mark in headers and on the login page. The SignalWire
            logo itself is never recolored — supply your own asset instead.
          </p>
        </div>

        <div className="space-y-3">
          {BRANDING_COLORS.map(({ key, label, hint, stock }) => (
            <div key={key} className="flex items-center gap-3">
              <input
                type="color"
                value={form[key] || stock}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                className="w-9 h-9 rounded border border-rule bg-canvas-raised cursor-pointer"
                title={`${label} color`}
              />
              <div className="flex-1">
                <div className="text-[13px] font-medium text-ink">{label}</div>
                <div className="text-[11.5px] text-ink-dim">{hint}</div>
              </div>
              <input
                value={form[key]}
                onChange={(e) => setForm({ ...form, [key]: e.target.value })}
                placeholder={stock}
                className="input w-28 mono text-[12px]"
              />
            </div>
          ))}
        </div>

        <div className="flex items-center gap-2 pt-2 border-t border-rule">
          <button onClick={() => save(form)} disabled={saving} className="btn-primary">
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
            Apply live
          </button>
          <button onClick={resetToStock} disabled={saving} className="btn-secondary">
            <RotateCcw className="w-3.5 h-3.5" />
            Reset to SignalWire
          </button>
        </div>
      </div>

      <p className="text-[11.5px] text-ink-dim mt-4">
        White-labeled deployments show a small "powered by signalwire" attribution on auth
        surfaces. Per-DID tenant branding (two numbers, two brands, same code) builds on this
        same config — see IMP-05 in the improvement plan.
      </p>
    </div>
  );
}


// =============================================================================
// Tab: Phone Numbers
// =============================================================================

function formatPhoneNumber(phone: string): string {
  // Format E.164 to readable: +12065551234 -> +1 (206) 555-1234
  const match = phone.match(/^\+1(\d{3})(\d{3})(\d{4})$/);
  if (match) {
    return `+1 (${match[1]}) ${match[2]}-${match[3]}`;
  }
  return phone;
}

function PhoneNumbersTab() {
  const [numbers, setNumbers] = useState<PhoneNumber[]>([]);
  const [queues, setQueues] = useState<QueueConfig[]>([]);
  const [webhookUrl, setWebhookUrl] = useState('');
  const [isConfigured, setIsConfigured] = useState(true);
  const [loading, setLoading] = useState(true);
  const [updatingNumber, setUpdatingNumber] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [configuringNumber, setConfiguringNumber] = useState<PhoneNumber | null>(null);
  // Workspace-admin read-only view (hosted mode): entry numbers + own
  // verified status; the install's real DIDs stay platform-only.
  const [readOnly, setReadOnly] = useState(false);
  const [entryNumbers, setEntryNumbers] = useState<{ label: string; number: string }[]>([]);
  const [verifiedMasked, setVerifiedMasked] = useState<string | null>(null);

  const loadNumbers = useCallback(async () => {
    try {
      const [phonesResp, queuesResp] = await Promise.all([
        adminApi.getPhoneNumbers(),
        adminApi.getQueues(),
      ]);
      if (phonesResp.data.read_only) {
        setReadOnly(true);
        setEntryNumbers(phonesResp.data.entry_numbers || []);
        setVerifiedMasked(phonesResp.data.verified ? phonesResp.data.verified_masked : null);
      }
      setNumbers(phonesResp.data.phone_numbers);
      setWebhookUrl(phonesResp.data.webhook_url);
      setIsConfigured(phonesResp.data.is_configured);
      setQueues(queuesResp.data.queues || []);
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to load phone numbers');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadNumbers(); }, [loadNumbers]);

  const handleUnassign = async (number: PhoneNumber) => {
    setUpdatingNumber(number.sid);
    try {
      await adminApi.updatePhoneNumber(number.sid, 'unassign');
      toast.success(`${formatPhoneNumber(number.phone_number)} unassigned`);
      await loadNumbers();
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to unassign phone number');
    } finally {
      setUpdatingNumber(null);
    }
  };

  const handleConfigureSave = async (
    number: PhoneNumber,
    target_mode: PhoneTargetMode,
    target_queue_slug: string | null,
  ) => {
    setUpdatingNumber(number.sid);
    try {
      await adminApi.updatePhoneNumber(number.sid, 'assign', { target_mode, target_queue_slug });
      toast.success(`${formatPhoneNumber(number.phone_number)} routing updated`);
      setConfiguringNumber(null);
      await loadNumbers();
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to update routing');
    } finally {
      setUpdatingNumber(null);
    }
  };

  const renderStatusChip = (n: PhoneNumber, hasExternal: boolean) => {
    if (n.is_assigned && n.target_mode === 'ai_triage') {
      return (
        <span className="chip chip-live">
          <span className="dot dot-live !w-1.5 !h-1.5" />AI receptionist
        </span>
      );
    }
    if (n.is_assigned && n.target_mode === 'ai_specialist') {
      return (
        <span className="chip chip-live" title={`AI specialist for queue: ${n.target_queue_slug}`}>
          <span className="dot dot-live !w-1.5 !h-1.5" />
          AI · {n.target_queue_slug}
        </span>
      );
    }
    if (n.is_assigned && n.target_mode === 'human_direct') {
      return (
        <span className="chip chip-wait" title="Skips AI — caller goes straight into the human queue">
          <span className="dot dot-wait !w-1.5 !h-1.5" />
          Human · {n.target_queue_slug}
        </span>
      );
    }
    // URL drift — distinct from External. The number IS bound to one of our
    // routes, just under a stale host (common after ngrok rotation). Shows
    // urgent-coloured so the admin notices, and the Re-sync action repairs
    // it in one click using the recovered (mode, queue) from the path.
    if (n.is_drifted) {
      const modeLabel =
        n.drifted_target_mode === 'ai_triage'     ? 'AI receptionist' :
        n.drifted_target_mode === 'ai_specialist' ? `AI · ${n.drifted_target_queue_slug}` :
        n.drifted_target_mode === 'human_direct'  ? `Human · ${n.drifted_target_queue_slug}` :
        'this route';
      return (
        <span
          className="chip chip-urgent"
          title={`URL drifted — points at a stale host. Was: ${modeLabel}. Current webhook: ${n.voice_url}`}
        >
          <span className="dot dot-urgent !w-1.5 !h-1.5" />
          URL drifted
        </span>
      );
    }
    if (hasExternal) {
      return (
        <span className="chip chip-wait" title={`Current webhook: ${n.voice_url}`}>
          <span className="dot dot-wait !w-1.5 !h-1.5" />External
        </span>
      );
    }
    return (
      <span className="chip chip-muted">
        <span className="w-1.5 h-1.5 rounded-full bg-ink-faint inline-block" />Not configured
      </span>
    );
  };

  const handleCopyWebhookUrl = async () => {
    try {
      await navigator.clipboard.writeText(webhookUrl);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      toast.error('Failed to copy');
    }
  };

  const handleCopyNumber = async (phone: string) => {
    try {
      await navigator.clipboard.writeText(phone);
      toast.success(`${formatPhoneNumber(phone)} copied`);
    } catch {
      toast.error('Failed to copy');
    }
  };

  if (loading) return <LoadingSpinner />;

  if (readOnly) {
    return (
      <div className="max-w-6xl">
        <div className="mb-5">
          <div className="kicker mb-1">Inbound</div>
          <h2 className="font-display text-[24px] text-ink leading-none mb-2">Phone numbers</h2>
          <p className="text-[13px] text-ink-muted">
            Your workspace's entry numbers. Calls from your verified phone land in your
            workspace; number routing itself is managed by the platform.
          </p>
        </div>
        <div className="panel rounded-md p-4 mb-5">
          <div className="kicker mb-2">Verified number</div>
          {verifiedMasked ? (
            <p className="text-[13px] text-ink">
              <span className="mono text-sw-turquoise">{verifiedMasked}</span>
              <span className="text-ink-muted"> — inbound calls from this number reach your workspace, and it's the only number your workspace can dial.</span>
            </p>
          ) : (
            <p className="text-[13px] text-ink-muted">
              No verified number yet — text your pairing code from the banner to link your phone.
            </p>
          )}
        </div>
        {entryNumbers.length > 0 && (
          <div className="panel rounded-md overflow-hidden">
            <table className="w-full">
              <thead>
                <tr className="border-b border-rule bg-canvas-sunken">
                  <th className="text-left px-4 py-2.5 kicker">Entry number</th>
                  <th className="text-left px-4 py-2.5 kicker">Line</th>
                </tr>
              </thead>
              <tbody>
                {entryNumbers.map((n) => (
                  <tr key={n.number} className="border-b border-rule last:border-b-0">
                    <td className="px-4 py-3 mono text-[13px] text-ink">{formatPhoneNumber(n.number)}</td>
                    <td className="px-4 py-3 text-[13px] text-ink-muted">{n.label}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="max-w-6xl">
      <div className="mb-5">
        <div className="kicker mb-1">Inbound</div>
        <h2 className="font-display text-[24px] text-ink leading-none mb-2">Phone numbers</h2>
        <p className="text-[13px] text-ink-muted">
          Choose which SignalWire numbers route inbound calls into the call center fabric.
        </p>
      </div>

      {!isConfigured && (
        <div className="bg-urgent/10 border border-urgent/30 rounded p-3 mb-5 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 text-urgent-soft mt-0.5 flex-shrink-0" />
          <p className="text-[12.5px] text-urgent-soft">
            <span className="mono">EXTERNAL_URL</span> is not set. Phone-number assignment needs a publicly accessible URL — set it in <span className="mono">.env</span> and restart the backend.
          </p>
        </div>
      )}

      {webhookUrl && (
        <div className="panel rounded-md p-4 mb-5">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <ExternalLink className="w-3.5 h-3.5 text-ink-dim flex-shrink-0" />
              <span className="kicker">Webhook URL</span>
              <code className="mono text-[12px] text-sw-turquoise truncate">{webhookUrl}</code>
            </div>
            <button onClick={handleCopyWebhookUrl} className="btn-secondary !py-1.5 !px-2.5 !text-[12px] flex-shrink-0 ml-3">
              {copied ? (
                <>
                  <Check className="w-3 h-3 text-live-soft" />
                  <span className="text-live-soft">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="w-3 h-3" />
                  <span>Copy</span>
                </>
              )}
            </button>
          </div>
          <p className="text-[11.5px] text-ink-dim mt-2">
            Assigned numbers will have their inbound voice webhook set to this URL.
          </p>
        </div>
      )}

      <div className="panel rounded-md overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-rule bg-canvas-sunken">
              <th className="text-left px-4 py-2.5 kicker">Phone Number</th>
              <th className="text-left px-4 py-2.5 kicker">Name</th>
              <th className="text-left px-4 py-2.5 kicker">Status</th>
              <th className="text-left px-4 py-2.5 kicker">Current Webhook</th>
              <th className="text-right px-4 py-2.5 kicker">Action</th>
            </tr>
          </thead>
          <tbody>
            {numbers.map(number => {
              const isUpdating = updatingNumber === number.sid;
              const hasExternalWebhook = !number.is_assigned && number.voice_url && number.voice_url.length > 0;
              return (
                <tr key={number.sid} className="border-b border-rule/60 last:border-b-0 hover:bg-canvas-hover/30">
                  <td className="px-4 py-3 whitespace-nowrap">
                    <div className="inline-flex items-center gap-2">
                      <span className="mono text-[13px] text-ink">{formatPhoneNumber(number.phone_number)}</span>
                      <button
                        onClick={() => handleCopyNumber(number.phone_number)}
                        className="text-ink-faint hover:text-ink transition-colors"
                        title="Copy phone number"
                      >
                        <Copy className="w-3 h-3" />
                      </button>
                    </div>
                  </td>
                  <td className="px-4 py-3 whitespace-nowrap">
                    <span className="text-[13px] text-ink-muted">{number.friendly_name || '\u2014'}</span>
                  </td>
                  <td className="px-4 py-3">
                    {renderStatusChip(number, !!hasExternalWebhook)}
                  </td>
                  <td className="px-4 py-3">
                    <span className="mono text-[11px] text-ink-dim truncate block max-w-[280px]" title={number.voice_url || 'None'}>
                      {number.voice_url || '\u2014'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <div className="flex justify-end gap-1.5">
                      {/* Re-sync — only when drifted. One-click recovery
                          using the (mode, queue) recovered from the stale
                          URL's path. Avoids the two-step Unassign → Configure
                          → Assign dance after every ngrok rotation. */}
                      {number.is_drifted && number.drifted_target_mode && (
                        <button
                          onClick={() => handleConfigureSave(
                            number,
                            number.drifted_target_mode as PhoneTargetMode,
                            number.drifted_target_queue_slug || null,
                          )}
                          disabled={isUpdating || !isConfigured}
                          className="btn-primary !py-1 !px-2.5 !text-[12px]"
                          title="Re-bind to the same route under the current host"
                        >
                          {isUpdating ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                          Re-sync
                        </button>
                      )}
                      <button
                        onClick={() => setConfiguringNumber(number)}
                        disabled={isUpdating || !isConfigured}
                        className="btn-secondary !py-1 !px-2.5 !text-[12px]"
                      >
                        {isUpdating ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                        Configure
                      </button>
                      {(number.is_assigned || number.is_drifted) && (
                        <button
                          onClick={() => handleUnassign(number)}
                          disabled={isUpdating}
                          className="btn-secondary !py-1 !px-2.5 !text-[12px]"
                          title="Remove the SWML webhook from this phone number"
                        >
                          Unassign
                        </button>
                      )}
                    </div>
                  </td>
                </tr>
              );
            })}
            {numbers.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-ink-dim py-8 text-[13px]">
                  No phone numbers found in your SignalWire space.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {numbers.length > 0 && (
        <p className="text-[11.5px] text-ink-dim mt-3 mono">
          {numbers.filter(n => n.is_assigned).length} / {numbers.length} numbers assigned
        </p>
      )}

      {configuringNumber && (
        <PhoneNumberConfigureModal
          phoneNumber={configuringNumber}
          queues={queues}
          saving={updatingNumber === configuringNumber.sid}
          onSave={(mode, queueSlug) => handleConfigureSave(configuringNumber, mode, queueSlug)}
          onCancel={() => setConfiguringNumber(null)}
        />
      )}
    </div>
  );
}


function PhoneNumberConfigureModal({
  phoneNumber,
  queues,
  saving,
  onSave,
  onCancel,
}: {
  phoneNumber: PhoneNumber;
  queues: QueueConfig[];
  saving: boolean;
  onSave: (mode: PhoneTargetMode, queueSlug: string | null) => void;
  onCancel: () => void;
}) {
  const [mode, setMode] = useState<PhoneTargetMode>(
    (phoneNumber.target_mode as PhoneTargetMode) || 'ai_triage',
  );
  const [queueSlug, setQueueSlug] = useState<string | null>(phoneNumber.target_queue_slug);

  // Escape closes
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onCancel(); };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onCancel]);

  const activeQueues = queues.filter((q) => q.is_active);
  // ai_specialist requires the queue to have an AI agent route, otherwise the
  // SWML fallback kicks in and the agent triage runs anyway — surface the
  // restriction so admins can't pick an unusable combo.
  const availableQueues = mode === 'ai_specialist'
    ? activeQueues.filter((q) => q.ai_agent_route)
    : activeQueues;

  const handleModeChange = (newMode: PhoneTargetMode) => {
    setMode(newMode);
    if (newMode === 'ai_triage') {
      setQueueSlug(null);
      return;
    }
    // If the current queueSlug isn't valid in the new mode, default to the first available.
    const filtered = newMode === 'ai_specialist'
      ? activeQueues.filter((q) => q.ai_agent_route)
      : activeQueues;
    if (!queueSlug || !filtered.find((q) => q.slug === queueSlug)) {
      setQueueSlug(filtered[0]?.slug || null);
    }
  };

  const canSave = !saving && (mode === 'ai_triage' || !!queueSlug);

  // Routing options are selectable cards: a real surface (so the borders read
  // as distinct rows, not flat lines on the modal) + a fuchsia-marked selected
  // state. Fuchsia is the active-marker pop; turquoise stays AI-only.
  const optionClass = (selected: boolean) =>
    `flex items-start gap-3 p-3 rounded-md border cursor-pointer transition-colors ${
      selected
        ? 'border-sw-fuchsia bg-canvas'
        : 'border-rule bg-canvas hover:border-rule-strong hover:bg-canvas-hover'
    }`;

  return (
    <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
      <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onCancel} />

      <div className="relative panel-raised rounded-md shadow-panel w-full max-w-[480px] flex flex-col">
        <div className="px-6 pt-5 pb-4 border-b border-rule-strong">
          <div className="kicker mb-1">Phone number routing</div>
          <h3 className="font-display text-[22px] text-ink leading-none">
            {formatPhoneNumber(phoneNumber.phone_number)}
          </h3>
          {phoneNumber.friendly_name && (
            <p className="mt-1.5 text-[13px] text-ink-muted">{phoneNumber.friendly_name}</p>
          )}
        </div>

        <div className="px-6 py-5 space-y-5">
          <section>
            <div className="kicker mb-2">Route incoming calls to</div>
            <div className="space-y-2">
              <label className={optionClass(mode === 'ai_triage')}>
                <input
                  type="radio"
                  checked={mode === 'ai_triage'}
                  onChange={() => handleModeChange('ai_triage')}
                  className="mt-0.5 accent-sw-fuchsia"
                />
                <div>
                  <div className="text-[13px] text-ink">AI receptionist (default)</div>
                  <div className="text-[11.5px] text-ink-dim mt-0.5">
                    AI answers, classifies intent, routes to the matching queue.
                  </div>
                </div>
              </label>
              <label className={optionClass(mode === 'ai_specialist')}>
                <input
                  type="radio"
                  checked={mode === 'ai_specialist'}
                  onChange={() => handleModeChange('ai_specialist')}
                  className="mt-0.5 accent-sw-fuchsia"
                />
                <div>
                  <div className="text-[13px] text-ink">AI specialist for a queue</div>
                  <div className="text-[11.5px] text-ink-dim mt-0.5">
                    Skip triage, go straight to the queue's AI agent.
                  </div>
                </div>
              </label>
              <label className={optionClass(mode === 'human_direct')}>
                <input
                  type="radio"
                  checked={mode === 'human_direct'}
                  onChange={() => handleModeChange('human_direct')}
                  className="mt-0.5 accent-sw-fuchsia"
                />
                <div>
                  <div className="text-[13px] text-ink">Human direct to a queue</div>
                  <div className="text-[11.5px] text-ink-dim mt-0.5">
                    Skip AI entirely. Agent's pre-join whisper notes "direct line, no AI screening".
                  </div>
                </div>
              </label>
            </div>
          </section>

          {mode !== 'ai_triage' && (
            <section>
              <div className="kicker mb-2">Queue</div>
              {availableQueues.length === 0 ? (
                <div className="text-[12px] text-ink-dim p-3 bg-canvas-sunken rounded border border-rule">
                  No queues available for this mode.
                  {mode === 'ai_specialist' && (
                    <span> (Mode requires a queue with an AI agent route configured.)</span>
                  )}
                </div>
              ) : (
                <select
                  value={queueSlug || ''}
                  onChange={(e) => setQueueSlug(e.target.value || null)}
                  className="input w-full"
                >
                  <option value="">— pick a queue —</option>
                  {availableQueues.map((q) => (
                    <option key={q.slug} value={q.slug}>
                      {q.display_name} ({q.slug})
                    </option>
                  ))}
                </select>
              )}
            </section>
          )}
        </div>

        <div className="px-6 py-4 border-t border-rule flex justify-end gap-2">
          <button onClick={onCancel} disabled={saving} className="btn-secondary">
            Cancel
          </button>
          <button
            onClick={() => onSave(mode, mode === 'ai_triage' ? null : queueSlug)}
            disabled={!canSave}
            className="btn-primary"
          >
            {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : null}
            Save
          </button>
        </div>
      </div>
    </div>
  );
}


// =============================================================================
// Tab: Queues
// =============================================================================

const ROUTING_STRATEGIES = [
  { value: 'fifo', label: 'FIFO', desc: 'First In, First Out — longest-idle agent gets next call' },
  { value: 'round_robin', label: 'Round Robin', desc: 'Rotate evenly across available agents' },
  { value: 'priority', label: 'Priority-Based', desc: 'High-priority calls get highest-skill agents' },
  { value: 'skill_based', label: 'Skill-Based', desc: 'Always assign to highest-skill agent' },
];

function QueuesTab() {
  const [queues, setQueues] = useState<QueueConfig[]>([]);
  const [selectedQueue, setSelectedQueue] = useState<QueueConfig | null>(null);
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [showNewQueue, setShowNewQueue] = useState(false);
  const [newSlug, setNewSlug] = useState('');
  const [newDisplayName, setNewDisplayName] = useState('');
  const [newStrategy, setNewStrategy] = useState('round_robin');
  const [pendingDelete, setPendingDelete] = useState<QueueConfig | null>(null);
  const [editForm, setEditForm] = useState<Partial<QueueConfig>>({});

  const loadQueues = useCallback(async () => {
    try {
      const resp = await adminApi.getQueues();
      setQueues(resp.data.queues);
    } catch (err) {
      toast.error('Failed to load queues');
    } finally {
      setLoading(false);
    }
  }, []);

  const loadAgents = useCallback(async () => {
    try {
      const resp = await adminApi.getAgentConfig();
      setAgents(resp.data.available_agents);
    } catch (err) {
      // non-critical
    }
  }, []);

  useEffect(() => {
    loadQueues();
    loadAgents();
  }, [loadQueues, loadAgents]);

  const handleSelectQueue = (q: QueueConfig) => {
    setSelectedQueue(q);
    setEditForm({
      display_name: q.display_name,
      description: q.description,
      routing_strategy: q.routing_strategy,
      routing_transport: q.routing_transport || 'conference',
      ai_agent_route: q.ai_agent_route,
      default_priority: q.default_priority,
      sla_threshold_seconds: q.sla_threshold_seconds,
      max_wait_before_ai_fallback: q.max_wait_before_ai_fallback,
      is_active: q.is_active,
    });
  };

  const handleCreateQueue = async () => {
    if (!newSlug || !newDisplayName) return;
    try {
      await adminApi.createQueue({
        slug: newSlug,
        display_name: newDisplayName,
        routing_strategy: newStrategy,
      });
      setShowNewQueue(false);
      setNewSlug('');
      setNewDisplayName('');
      setNewStrategy('round_robin');
      toast.success('Queue created');
      loadQueues();
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to create queue');
    }
  };

  const handleSaveQueue = async () => {
    if (!selectedQueue) return;
    setSaving(true);
    try {
      await adminApi.updateQueue(selectedQueue.id, editForm);
      toast.success('Queue updated');
      loadQueues();
      // Update selected queue in place
      setSelectedQueue({ ...selectedQueue, ...editForm } as QueueConfig);
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to update queue');
    } finally {
      setSaving(false);
    }
  };

  const confirmDeleteQueue = async (q: QueueConfig) => {
    try {
      await adminApi.deleteQueue(q.id);
      if (selectedQueue?.id === q.id) {
        setSelectedQueue(null);
      }
      toast.success('Queue deleted');
      loadQueues();
    } catch (err) {
      toast.error('Failed to delete queue');
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="flex gap-3 h-full">
      {/* Left: Queue list */}
      <div className="w-80 flex-shrink-0">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="kicker mb-1">Routing</div>
            <h2 className="font-display text-[22px] text-ink leading-none">Queues</h2>
          </div>
          <button
            onClick={() => setShowNewQueue(true)}
            className="btn-secondary !p-1.5"
            title="New queue"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        {showNewQueue && (
          <div className="panel rounded-md p-3 mb-3 space-y-2">
            <input
              value={newSlug}
              onChange={e => setNewSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '-'))}
              placeholder="queue-slug"
              className="w-full input !py-1.5 !px-2"
            />
            <input
              value={newDisplayName}
              onChange={e => setNewDisplayName(e.target.value)}
              placeholder="Display Name"
              className="w-full input !py-1.5 !px-2"
            />
            <select
              value={newStrategy}
              onChange={e => setNewStrategy(e.target.value)}
              className="w-full input !py-1.5 !px-2"
            >
              {ROUTING_STRATEGIES.map(s => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <div className="flex gap-2">
              <button onClick={handleCreateQueue} className="btn-primary !py-1 !px-2.5 !text-[12px]">Create</button>
              <button onClick={() => setShowNewQueue(false)} className="btn-ghost !py-1 !px-2.5 !text-[12px]">Cancel</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {queues.map(q => (
            <div
              key={q.id}
              onClick={() => handleSelectQueue(q)}
              className={`relative p-3 rounded-md cursor-pointer border transition-colors ${
                selectedQueue?.id === q.id
                  ? 'bg-canvas-hover border-rule-strong'
                  : 'panel hover:border-rule-strong'
              }`}
            >
              {selectedQueue?.id === q.id && (
                <span className="absolute left-0 top-2.5 bottom-2.5 w-0.5 rounded-sm bg-sw-fuchsia" />
              )}
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2 min-w-0">
                  <span className={`dot ${q.is_active ? 'dot-live' : 'dot-offline'}`} />
                  <span className="text-[13px] font-medium text-ink truncate">{q.display_name}</span>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); setPendingDelete(q); }}
                  className="p-1 rounded hover:bg-urgent/10 text-ink-dim hover:text-urgent-soft transition-colors flex-shrink-0"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
              <div className="flex items-center gap-2 mt-1.5">
                <Chip>
                  {ROUTING_STRATEGIES.find(s => s.value === q.routing_strategy)?.label || q.routing_strategy}
                </Chip>
                {q.agent_count !== undefined && (
                  <span className="mono text-[10.5px] text-ink-dim">{q.agent_count} agents</span>
                )}
              </div>
            </div>
          ))}
          {queues.length === 0 && (
            <p className="text-[12.5px] text-ink-dim text-center py-4">No queues configured.</p>
          )}
        </div>
      </div>

      {/* Right: Queue detail form */}
      <div className="flex-1 min-w-0">
        {selectedQueue ? (
          <div className="space-y-6">
            <div>
              <div className="kicker mb-1">Queue</div>
              <h2 className="font-display text-[28px] text-ink leading-none tracking-tightest">{selectedQueue.display_name}</h2>
            </div>

            {/* Settings form */}
            <div className="grid grid-cols-2 gap-4 panel rounded-md p-5">
              {/* Row 1: Display Name + Routing Strategy */}
              <div>
                <label className="block kicker mb-1">Display Name</label>
                <input
                  value={editForm.display_name || ''}
                  onChange={e => setEditForm({ ...editForm, display_name: e.target.value })}
                  className="input"
                />
              </div>
              <div>
                <label className="block kicker mb-1">Routing Strategy</label>
                <select
                  value={editForm.routing_strategy || 'round_robin'}
                  onChange={e => setEditForm({ ...editForm, routing_strategy: e.target.value })}
                  className="input"
                >
                  {ROUTING_STRATEGIES.map(s => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
                <p className="text-[11px] text-ink-dim mt-1">
                  {ROUTING_STRATEGIES.find(s => s.value === editForm.routing_strategy)?.desc}
                </p>
              </div>

              {/* Description — full width */}
              <div className="col-span-2">
                <label className="block kicker mb-1">Description</label>
                <textarea
                  value={editForm.description || ''}
                  onChange={e => setEditForm({ ...editForm, description: e.target.value })}
                  rows={2}
                  className="input resize-none"
                />
              </div>

              {/* Row: AI Agent Route + Default Priority */}
              <div>
                <label className="block kicker mb-1">AI Agent Route</label>
                <select
                  value={editForm.ai_agent_route || ''}
                  onChange={e => setEditForm({ ...editForm, ai_agent_route: e.target.value || null })}
                  className="input"
                >
                  <option value="">None</option>
                  {agents.map(a => (
                    <option key={a.id} value={a.route}>{a.name} ({a.route})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block kicker mb-1">Default Priority (1–10)</label>
                <input
                  type="number" min={1} max={10}
                  value={editForm.default_priority || 5}
                  onChange={e => setEditForm({ ...editForm, default_priority: parseInt(e.target.value) || 5 })}
                  className="input mono"
                />
              </div>

              {/* Row: SLA Threshold + Max Wait Before AI Fallback */}
              <div>
                <label className="block kicker mb-1">SLA Threshold (seconds)</label>
                <input
                  type="number" min={0}
                  value={editForm.sla_threshold_seconds || 60}
                  onChange={e => setEditForm({ ...editForm, sla_threshold_seconds: parseInt(e.target.value) || 60 })}
                  className="input mono"
                />
              </div>
              <div>
                <label className="block kicker mb-1">Max Wait Before AI Fallback (seconds)</label>
                <input
                  type="number" min={0}
                  value={editForm.max_wait_before_ai_fallback ?? 120}
                  onChange={e => setEditForm({ ...editForm, max_wait_before_ai_fallback: parseInt(e.target.value) || 0 })}
                  className="input mono"
                />
                <p className="text-[11px] text-ink-dim mt-1">
                  Waiting callers are offered the <span className="text-ai">✦</span> AI specialist after this delay.
                </p>
              </div>

              {/* Call transport: see CALL_TRANSPORT.md. Conference (current default)
                  supports multi-party — supervisor monitor, whisper, barge. Bridge
                  is a direct two-leg dial with native per-leg REST verbs (hold,
                  DTMF) but no multi-party until promote-to-conference ships (M4).
                  Bridge falls back to conference automatically when no agent is
                  available, so the queue safety net is preserved. */}
              <div className="col-span-2">
                <label className="block kicker mb-1">Call Transport</label>
                <select
                  value={editForm.routing_transport || 'conference'}
                  onChange={e => setEditForm({
                    ...editForm,
                    routing_transport: e.target.value as 'conference' | 'bridge',
                  })}
                  className="input"
                >
                  <option value="conference">Conference — multi-party (supervisor monitor, whisper, barge)</option>
                  <option value="bridge">Bridge — direct dial (native hold + DTMF, no multi-party)</option>
                </select>
                <p className="text-[11px] text-ink-dim mt-1">
                  {editForm.routing_transport === 'bridge'
                    ? 'New calls dial the assigned agent directly. Falls back to conference parking when no agent is available.'
                    : 'New calls park in an interaction conference; agent joins via WebRTC. Default.'}
                </p>
              </div>
            </div>

            {/* Assigned Agents */}
            <div className="panel rounded-md p-5">
              <div className="flex items-center justify-between mb-4">
                <div className="flex items-center gap-2">
                  <Users className="w-4 h-4 text-ink-dim" />
                  <span className="kicker">Assigned Agents</span>
                </div>
                <Chip>{selectedQueue.agent_count ?? 0} assigned</Chip>
              </div>
              {selectedQueue.agent_count && selectedQueue.agent_count > 0 ? (
                <div className="space-y-2.5">
                  {Array.from({ length: selectedQueue.agent_count }).map((_, i) => (
                    <div
                      key={i}
                      className="flex items-center gap-3 rounded-md border border-rule bg-canvas-raised px-3 py-2.5"
                    >
                      <span className="w-6 h-6 rounded-md bg-canvas-elevated border border-rule flex items-center justify-center text-[10px] font-semibold text-ink-muted flex-shrink-0">
                        {String.fromCharCode(65 + (i % 26))}
                      </span>
                      <div className="flex-1 min-w-0">
                        <div className="text-[13px] font-medium text-ink truncate">Agent {i + 1}</div>
                        <div className="mono text-[11px] text-ink-dim truncate">Skill level set in agent console</div>
                      </div>
                      {/* Skill-level meter: 5 horizontal bar segments (12×4px) */}
                      <div className="flex items-center gap-1 flex-shrink-0" title="Skill level managed per agent">
                        {Array.from({ length: 5 }).map((_, d) => (
                          <span
                            key={d}
                            className={`w-3 h-1 rounded-sm ${d < 3 ? 'bg-ink-muted' : 'bg-rule'}`}
                          />
                        ))}
                      </div>
                    </div>
                  ))}
                  <p className="text-[11px] text-ink-dim pt-1">
                    Agents opt in to queues from their console (skill levels are set there). This list reflects current assignments.
                  </p>
                </div>
              ) : (
                <p className="text-[12.5px] text-ink-dim py-2">
                  No agents assigned yet. Agents join this queue from the queue-activation toggle in their console.
                </p>
              )}
            </div>

            {/* Queue active + save */}
            <div className="flex items-center justify-between gap-4">
              <Checkbox
                checked={editForm.is_active ?? true}
                onChange={checked => setEditForm({ ...editForm, is_active: checked })}
                label="Queue is active"
              />
              <button onClick={handleSaveQueue} disabled={saving} className="btn-primary">
                {saving ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <Save className="w-3.5 h-3.5" />}
                Save queue settings
              </button>
            </div>
          </div>
        ) : (
          <div className="flex items-center justify-center h-full bg-dotgrid rounded-md">
            <div className="text-center">
              <LayoutList className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
              <p className="font-display text-[22px] text-ink-muted">Pick a queue</p>
              <p className="text-[12px] text-ink-dim mt-1">Select a queue on the left to configure its routing.</p>
            </div>
          </div>
        )}
      </div>

      {pendingDelete && (
        <ConfirmModal
          title="Delete Queue"
          message={`Delete "${pendingDelete.display_name}" queue? This will remove all agent assignments and cannot be undone.`}
          onConfirm={async () => {
            await confirmDeleteQueue(pendingDelete);
            setPendingDelete(null);
          }}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}


// =============================================================================
// Tab: AI Agents (per-agent knowledge base assignment)
// =============================================================================

function AgentsTab({ onNavigateToCollection }: { onNavigateToCollection: (collectionId: number) => void }) {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [assignments, setAssignments] = useState<Record<string, number>>({});
  const [selectedAgent, setSelectedAgent] = useState<Agent | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [hasChanges, setHasChanges] = useState(false);
  const [originalAssignments, setOriginalAssignments] = useState<Record<string, number>>({});

  const loadData = useCallback(async () => {
    try {
      const [configResp, collectionsResp, assignmentsResp] = await Promise.all([
        adminApi.getAgentConfig(),
        adminApi.getCollections(),
        adminApi.getAgentAssignments(),
      ]);

      setAgents(configResp.data.available_agents);
      setCollections(collectionsResp.data.collections);

      const map: Record<string, number> = {};
      for (const a of assignmentsResp.data.assignments) {
        map[a.agent_id] = a.collection_id;
      }
      setAssignments(map);
      setOriginalAssignments({ ...map });
    } catch (err) {
      toast.error('Failed to load agent configuration');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  // Dirty detection
  useEffect(() => {
    setHasChanges(JSON.stringify(assignments) !== JSON.stringify(originalAssignments));
  }, [assignments, originalAssignments]);

  const handleAssignmentChange = (collectionId: number) => {
    if (!selectedAgent) return;
    setAssignments({ ...assignments, [selectedAgent.id]: collectionId });
  };

  const handleSave = async () => {
    setSaving(true);
    try {
      const assignmentList = Object.entries(assignments)
        .filter(([, collId]) => collId > 0)
        .map(([agentId, collId]) => ({ agent_id: agentId, collection_id: collId }));
      await adminApi.updateAgentAssignments({ assignments: assignmentList });

      setOriginalAssignments({ ...assignments });
      toast.success('Agent configuration saved');
    } catch (err) {
      toast.error('Failed to save configuration');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="flex gap-6 h-full">
      {/* LEFT COLUMN: Agent list */}
      <div className="w-80 flex-shrink-0">
        <div className="mb-4">
          <div className="kicker mb-1">AI fleet</div>
          <h2 className="font-display text-[22px] text-ink leading-none">Agents</h2>
          <p className="text-[12px] text-ink-muted mt-2">Knowledge base assignments per agent</p>
        </div>

        <div className="space-y-2">
          {[...agents].sort((a, b) => {
            if (a.type === 'outbound' && b.type !== 'outbound') return 1;
            if (a.type !== 'outbound' && b.type === 'outbound') return -1;
            return 0;
          }).map(agent => {
            const isOutbound = agent.type === 'outbound';
            const assignedCollId = assignments[agent.id];
            const assignedColl = collections.find(c => c.id === assignedCollId);

            return (
              <button
                key={agent.id}
                onClick={() => setSelectedAgent(agent)}
                className={`w-full text-left p-3 rounded-md cursor-pointer border transition-colors ${
                  selectedAgent?.id === agent.id
                    ? 'bg-canvas-elevated ring-1 ring-sw-blue/30'
                    : 'panel hover:border-rule-strong'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-[13.5px] font-medium text-ink">{agent.name}</span>
                  {isOutbound && (
                    <span className="chip chip-info"><PhoneOutgoing className="w-2.5 h-2.5" />Outbound</span>
                  )}
                </div>
                <div className="mono text-[11px] text-ink-dim mt-0.5">{agent.route}</div>

                {assignedColl ? (
                  <div className="flex items-center gap-1.5 mt-2">
                    <BookOpen className="w-3 h-3 text-sw-fuchsia" />
                    <span className="text-[11px] text-sw-fuchsia">{assignedColl.display_name}</span>
                  </div>
                ) : (
                  <div className="flex items-center gap-1.5 mt-2">
                    <BookOpen className="w-3 h-3 text-ink-faint" />
                    <span className="text-[11px] text-ink-dim">No knowledge base</span>
                  </div>
                )}
              </button>
            );
          })}
        </div>
      </div>

      {/* RIGHT COLUMN: Selected agent detail */}
      <div className="flex-1 min-w-0">
        {selectedAgent ? (
          <div className="space-y-6">
            <div>
              <div className="kicker mb-1">Agent</div>
              <div className="flex items-center gap-3">
                <h2 className="font-display text-[28px] text-ink leading-none tracking-tightest">{selectedAgent.name}</h2>
                {selectedAgent.type === 'outbound' && (
                  <span className="chip chip-info"><PhoneOutgoing className="w-2.5 h-2.5" />Outbound</span>
                )}
              </div>
              <p className="mono text-[12px] text-ink-dim mt-2">{selectedAgent.route}</p>
              <p className="text-[13px] text-ink-muted mt-2 max-w-xl">{selectedAgent.description}</p>
            </div>

            {/* Knowledge Base */}
            <div className="panel rounded-md p-5">
              <div className="kicker mb-1">Knowledge base</div>
              <h3 className="font-display text-[18px] text-ink leading-none mb-2">What this agent knows</h3>
              <p className="text-[12.5px] text-ink-muted mb-3 max-w-xl">
                Assign a document collection the agent can search during conversations via RAG.
              </p>
              <div className="flex items-center gap-2">
                <select
                  value={assignments[selectedAgent.id] || 0}
                  onChange={e => handleAssignmentChange(parseInt(e.target.value))}
                  className="input flex-1"
                >
                  <option value={0}>None</option>
                  {collections.map(coll => (
                    <option key={coll.id} value={coll.id}>{coll.display_name}</option>
                  ))}
                </select>
                {assignments[selectedAgent.id] > 0 && (
                  <button
                    onClick={() => onNavigateToCollection(assignments[selectedAgent.id])}
                    className="btn-ghost !p-2"
                    title="Edit collection in Knowledge Base"
                  >
                    <Pencil className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>

            <div className="bg-wait/10 border border-wait/25 rounded p-3 flex items-start gap-2">
              <Info className="w-3.5 h-3.5 text-wait-soft mt-0.5 flex-shrink-0" />
              <p className="text-[12px] text-wait-soft">
                KB changes apply live — new calls pick them up within about 30 seconds. Call routing is configured per-queue.
              </p>
            </div>

            <button onClick={handleSave} disabled={saving || !hasChanges} className="btn-primary">
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Save Configuration
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-center h-full bg-dotgrid rounded-md">
            <div className="text-center">
              <Bot className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
              <p className="font-display text-[22px] text-ink-muted">Pick an agent</p>
              <p className="text-[12px] text-ink-dim mt-1">Select an agent on the left to configure its knowledge.</p>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}


// =============================================================================
// Tab: Knowledge Base
// =============================================================================

/** Read a file and return its text content. Supports common text-based formats. */
async function parseFileToText(file: File): Promise<string> {
  return file.text();
}

const ACCEPTED_FILE_TYPES = '.txt,.md,.csv,.json,.html,.xml,.log';

function KnowledgeBaseTab({ focusCollectionId }: { focusCollectionId?: number | null }) {
  const [collections, setCollections] = useState<Collection[]>([]);
  const [selectedCollection, setSelectedCollection] = useState<Collection | null>(null);
  const [documents, setDocuments] = useState<Document[]>([]);
  const [editingDoc, setEditingDoc] = useState<Document | null>(null);
  const [newDocMode, setNewDocMode] = useState(false);
  const [newDocTitle, setNewDocTitle] = useState('');
  const [newDocContent, setNewDocContent] = useState('');
  const [loading, setLoading] = useState(true);
  const [reindexing, setReindexing] = useState(false);
  const [reindexResult, setReindexResult] = useState<string | null>(null);
  const [showNewCollection, setShowNewCollection] = useState(false);
  const [newCollName, setNewCollName] = useState('');
  const [newCollDisplayName, setNewCollDisplayName] = useState('');
  const [newCollDescription, setNewCollDescription] = useState('');
  const [pendingDelete, setPendingDelete] = useState<{ type: 'collection'; item: Collection } | { type: 'document'; item: Document } | null>(null);

  const loadCollections = useCallback(async () => {
    try {
      const resp = await adminApi.getCollections();
      setCollections(resp.data.collections);
    } catch (err) {
      toast.error('Failed to load collections');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadCollections(); }, [loadCollections]);

  // Auto-select collection when navigated from AgentsTab
  useEffect(() => {
    if (focusCollectionId && collections.length > 0) {
      const target = collections.find(c => c.id === focusCollectionId);
      if (target) handleSelectCollection(target);
    }
  }, [focusCollectionId, collections]);

  const loadDocuments = useCallback(async (collectionId: number) => {
    try {
      const resp = await adminApi.getDocuments(collectionId);
      setDocuments(resp.data.documents);
    } catch (err) {
      toast.error('Failed to load documents');
    }
  }, []);

  const handleSelectCollection = (coll: Collection) => {
    setSelectedCollection(coll);
    setEditingDoc(null);
    setNewDocMode(false);
    setReindexResult(null);
    loadDocuments(coll.id);
  };

  const handleCreateCollection = async () => {
    if (!newCollName || !newCollDisplayName) return;
    try {
      await adminApi.createCollection({
        name: newCollName,
        display_name: newCollDisplayName,
        description: newCollDescription,
      });
      setShowNewCollection(false);
      setNewCollName('');
      setNewCollDisplayName('');
      setNewCollDescription('');
      toast.success('Collection created');
      loadCollections();
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to create collection');
    }
  };

  const handleDeleteCollection = (coll: Collection) => {
    setPendingDelete({ type: 'collection', item: coll });
  };

  const confirmDeleteCollection = async (coll: Collection) => {
    try {
      await adminApi.deleteCollection(coll.id);
      if (selectedCollection?.id === coll.id) {
        setSelectedCollection(null);
        setDocuments([]);
      }
      toast.success('Collection deleted');
      loadCollections();
    } catch (err) {
      toast.error('Failed to delete collection');
    }
  };

  const handleCreateDocument = async () => {
    if (!selectedCollection || !newDocTitle || !newDocContent) return;
    try {
      await adminApi.createDocument(selectedCollection.id, {
        title: newDocTitle,
        content: newDocContent,
      });
      setNewDocMode(false);
      setNewDocTitle('');
      setNewDocContent('');
      toast.success('Document created');
      loadDocuments(selectedCollection.id);
      loadCollections();
    } catch (err) {
      toast.error('Failed to create document');
    }
  };

  const handleSaveDocument = async () => {
    if (!editingDoc) return;
    try {
      await adminApi.updateDocument(editingDoc.id, {
        title: editingDoc.title,
        content: editingDoc.content,
      });
      toast.success('Document saved');
      if (selectedCollection) loadDocuments(selectedCollection.id);
    } catch (err) {
      toast.error('Failed to save document');
    }
  };

  const handleDeleteDocument = (doc: Document) => {
    setPendingDelete({ type: 'document', item: doc });
  };

  const confirmDeleteDocument = async (doc: Document) => {
    try {
      await adminApi.deleteDocument(doc.id);
      if (editingDoc?.id === doc.id) setEditingDoc(null);
      toast.success('Document deleted');
      if (selectedCollection) {
        loadDocuments(selectedCollection.id);
        loadCollections();
      }
    } catch (err) {
      toast.error('Failed to delete document');
    }
  };

  const handleReindex = async () => {
    if (!selectedCollection) return;
    setReindexing(true);
    setReindexResult(null);
    try {
      const resp = await adminApi.reindexCollection(selectedCollection.id);
      const data = resp.data;
      setReindexResult(`Indexed ${data.documents_indexed} documents into ${data.chunks_indexed} chunks`);
      toast.success('Reindex complete');
      if (selectedCollection) loadDocuments(selectedCollection.id);
    } catch (err: any) {
      const msg = err.response?.data?.error || 'Reindex failed';
      setReindexResult(`Error: ${msg}`);
      toast.error(msg);
    } finally {
      setReindexing(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="flex gap-6 h-full">
      {/* Left: Collections */}
      <div className="w-80 flex-shrink-0">
        <div className="flex items-center justify-between mb-4">
          <div>
            <div className="kicker mb-1">RAG</div>
            <h2 className="font-display text-[22px] text-ink leading-none">Collections</h2>
          </div>
          <button
            onClick={() => setShowNewCollection(true)}
            className="btn-secondary !p-1.5"
            title="New collection"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        {showNewCollection && (
          <div className="panel rounded-md p-3 mb-3 space-y-2">
            <input
              value={newCollName}
              onChange={e => setNewCollName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))}
              placeholder="collection_name"
              className="w-full input !py-1.5 !px-2"
            />
            <input
              value={newCollDisplayName}
              onChange={e => setNewCollDisplayName(e.target.value)}
              placeholder="Display Name"
              className="w-full input !py-1.5 !px-2"
            />
            <textarea
              value={newCollDescription}
              onChange={e => setNewCollDescription(e.target.value)}
              placeholder="Description"
              rows={2}
              className="w-full input !py-1.5 !px-2 resize-none"
            />
            <div className="flex gap-2">
              <button onClick={handleCreateCollection} className="btn-primary !py-1 !px-2.5 !text-[12px]">Create</button>
              <button onClick={() => setShowNewCollection(false)} className="btn-ghost !py-1 !px-2.5 !text-[12px]">Cancel</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {collections.map(coll => (
            <div
              key={coll.id}
              onClick={() => handleSelectCollection(coll)}
              className={`p-3 rounded-md border cursor-pointer transition-colors ${
                selectedCollection?.id === coll.id
                  ? 'bg-canvas-elevated ring-1 ring-sw-blue/30'
                  : 'panel hover:border-rule-strong'
              }`}
            >
              <div className="flex items-start justify-between">
                <div>
                  <div className="text-[13.5px] font-medium text-ink">{coll.display_name}</div>
                  <div className="mono text-[11px] text-ink-dim mt-0.5">{coll.name}</div>
                  <div className="text-[11.5px] text-ink-dim mt-1">{coll.document_count} documents</div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); handleDeleteCollection(coll); }}
                  className="p-1 rounded hover:bg-urgent/10 text-ink-dim hover:text-urgent-soft transition-colors"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
              {coll.description && (
                <div className="text-[11.5px] text-ink-muted mt-1.5 leading-snug">{coll.description}</div>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Right: Documents */}
      <div className="flex-1 min-w-0">
        {selectedCollection ? (
          <>
            <div className="flex items-center justify-between mb-4">
              <div>
                <div className="kicker mb-1">Collection</div>
                <h2 className="font-display text-[24px] text-ink leading-none tracking-tightest">{selectedCollection.display_name}</h2>
                <p className="text-[12.5px] text-ink-muted mt-1.5">{selectedCollection.description}</p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => { setNewDocMode(true); setEditingDoc(null); }}
                  className="btn-secondary"
                >
                  <Plus className="w-3.5 h-3.5" />
                  New document
                </button>
                <button
                  onClick={handleReindex}
                  disabled={reindexing || documents.length === 0}
                  className="btn-primary"
                >
                  {reindexing ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
                  Publish changes
                </button>
              </div>
            </div>

            {reindexResult && (
              <div className={`mb-4 p-3 rounded border text-[12.5px] flex items-center gap-2 ${
                reindexResult.startsWith('Error')
                  ? 'bg-urgent/10 border-urgent/30 text-urgent-soft'
                  : 'bg-live/10 border-live/30 text-live-soft'
              }`}>
                {reindexResult.startsWith('Error')
                  ? <AlertCircle className="w-3.5 h-3.5 flex-shrink-0" />
                  : <Check className="w-3.5 h-3.5 flex-shrink-0" />
                }
                {reindexResult}
              </div>
            )}

            {/* Document list */}
            <div className="space-y-1 mb-4">
              {documents.map(doc => (
                <div
                  key={doc.id}
                  onClick={() => { setEditingDoc({ ...doc }); setNewDocMode(false); }}
                  className={`flex items-center justify-between px-3 py-2.5 rounded-md cursor-pointer transition-colors ${
                    editingDoc?.id === doc.id
                      ? 'bg-canvas-elevated border border-sw-blue/30'
                      : 'panel hover:border-rule-strong'
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileText className="w-3.5 h-3.5 text-ink-dim flex-shrink-0" />
                    <span className="text-[13px] text-ink truncate">{doc.title}</span>
                    {doc.is_published && (
                      <span className="chip chip-live">Published</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="mono text-[11px] text-ink-dim">
                      {new Date(doc.updated_at).toLocaleDateString()}
                    </span>
                    <button
                      onClick={e => { e.stopPropagation(); handleDeleteDocument(doc); }}
                      className="p-1 rounded hover:bg-urgent/10 text-ink-dim hover:text-urgent-soft"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </div>
              ))}
              {documents.length === 0 && (
                <div className="text-center py-10">
                  <p className="font-display text-[20px] text-ink-muted">No documents yet</p>
                  <p className="text-[12px] text-ink-dim mt-1">Click "New document" to add content the AI can search.</p>
                </div>
              )}
            </div>

            {/* New document form */}
            {newDocMode && (
              <div className="panel rounded-md p-4">
                <div className="flex items-center justify-between mb-3">
                  <div className="flex items-center gap-2">
                    <div className="kicker">New document</div>
                  </div>
                  <label className="btn-ghost cursor-pointer !py-1.5 !px-2.5">
                    <Upload className="w-3.5 h-3.5" />
                    Upload file
                    <input
                      type="file"
                      accept={ACCEPTED_FILE_TYPES}
                      className="hidden"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (!file) return;
                        try {
                          const text = await parseFileToText(file);
                          if (!newDocTitle) setNewDocTitle(file.name.replace(/\.[^.]+$/, ''));
                          setNewDocContent(text);
                          toast.success(`Loaded ${file.name}`);
                        } catch (err) {
                          toast.error('Failed to parse file');
                        }
                        e.target.value = '';
                      }}
                    />
                  </label>
                </div>
                <input
                  value={newDocTitle}
                  onChange={e => setNewDocTitle(e.target.value)}
                  placeholder="Document title"
                  className="input mb-3"
                />
                <textarea
                  value={newDocContent}
                  onChange={e => setNewDocContent(e.target.value)}
                  placeholder="Document content... (sales scripts, troubleshooting guides, product info, etc.)"
                  rows={12}
                  className="input mono resize-y"
                />
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={handleCreateDocument}
                    disabled={!newDocTitle || !newDocContent}
                    className="btn-primary"
                  >
                    Create document
                  </button>
                  <button
                    onClick={() => setNewDocMode(false)}
                    className="btn-ghost"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* Edit document form */}
            {editingDoc && !newDocMode && (
              <div className="panel rounded-md p-4">
                <div className="flex items-center justify-between mb-3">
                  <div className="kicker">Edit document</div>
                  <label className="btn-ghost cursor-pointer !py-1.5 !px-2.5">
                    <Upload className="w-3.5 h-3.5" />
                    Replace from file
                    <input
                      type="file"
                      accept={ACCEPTED_FILE_TYPES}
                      className="hidden"
                      onChange={async (e) => {
                        const file = e.target.files?.[0];
                        if (!file) return;
                        try {
                          const text = await parseFileToText(file);
                          setEditingDoc({ ...editingDoc, content: text });
                          toast.success(`Loaded ${file.name}`);
                        } catch {
                          toast.error('Failed to parse file');
                        }
                        e.target.value = '';
                      }}
                    />
                  </label>
                </div>
                <input
                  value={editingDoc.title}
                  onChange={e => setEditingDoc({ ...editingDoc, title: e.target.value })}
                  className="input mb-3"
                />
                <textarea
                  value={editingDoc.content}
                  onChange={e => setEditingDoc({ ...editingDoc, content: e.target.value })}
                  rows={12}
                  className="input mono resize-y"
                />
                <div className="flex gap-2 mt-3">
                  <button onClick={handleSaveDocument} className="btn-primary">Save changes</button>
                  <button onClick={() => setEditingDoc(null)} className="btn-ghost">Cancel</button>
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="flex items-center justify-center h-full bg-dotgrid rounded-md">
            <div className="text-center">
              <BookOpen className="w-5 h-5 mx-auto mb-3 text-ink-faint" />
              <p className="font-display text-[22px] text-ink-muted">Pick a collection</p>
              <p className="text-[12px] text-ink-dim mt-1">Manage documents inside the selected collection.</p>
            </div>
          </div>
        )}
      </div>

      {/* Confirm Delete Modal */}
      {pendingDelete && (
        <ConfirmModal
          title={pendingDelete.type === 'collection' ? 'Delete Collection' : 'Delete Document'}
          message={
            pendingDelete.type === 'collection'
              ? `Delete "${(pendingDelete.item as Collection).display_name}" and all its documents?`
              : `Delete "${(pendingDelete.item as Document).title}"?`
          }
          onConfirm={async () => {
            if (pendingDelete.type === 'collection') {
              await confirmDeleteCollection(pendingDelete.item as Collection);
            } else {
              await confirmDeleteDocument(pendingDelete.item as Document);
            }
            setPendingDelete(null);
          }}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}




// =============================================================================
// Tab: User Management
// =============================================================================

type UserRole = 'admin' | 'supervisor' | 'agent';

const ROLE_OPTIONS: UserRole[] = ['admin', 'supervisor', 'agent'];

function UserManagementTab() {
  const currentUser = useAuthStore((s) => s.user);
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [editingUser, setEditingUser] = useState<AdminUser | null>(null);
  const [savingRoleFor, setSavingRoleFor] = useState<number | null>(null);

  const loadUsers = useCallback(async () => {
    try {
      const resp = await adminApi.listUsers();
      setUsers(resp.data.users);
    } catch (err) {
      toast.error('Failed to load users');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadUsers(); }, [loadUsers]);

  const changeUserRole = async (user: AdminUser, nextRole: UserRole) => {
    if (user.role === nextRole) return;
    setSavingRoleFor(user.id);
    // Optimistic update — revert on error.
    const previous = user.role;
    setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, role: nextRole } : u)));
    try {
      await adminApi.updateUserRole(user.id, nextRole);
      toast.success(`${user.email} is now a ${nextRole}`);
    } catch (err: unknown) {
      setUsers((prev) => prev.map((u) => (u.id === user.id ? { ...u, role: previous } : u)));
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        'Failed to update role';
      toast.error(message);
    } finally {
      setSavingRoleFor(null);
    }
  };

  // Delete now lives inside UserEditModal (with its own confirmation modal).
  // The row only opens the edit surface; destructive actions are one level deeper.

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-5xl">
      <div className="mb-5">
        <div className="kicker mb-1">Team</div>
        <h2 className="font-display text-[24px] text-ink leading-none mb-2">User management</h2>
        <p className="text-[13px] text-ink-muted">
          Deleting a user removes their account, their calls, and their SignalWire subscriber.
        </p>
      </div>

      <div className="panel rounded-md overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-rule bg-canvas-sunken">
              <th className="text-left px-4 py-2.5 kicker">User</th>
              <th className="text-left px-4 py-2.5 kicker">Role</th>
              <th className="text-left px-4 py-2.5 kicker">Languages</th>
              <th className="text-left px-4 py-2.5 kicker">Subscriber</th>
              <th className="text-left px-4 py-2.5 kicker">Created</th>
              <th className="text-right px-4 py-2.5 kicker">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map(user => (
              <tr key={user.id} className="border-b border-rule/60 last:border-b-0 hover:bg-canvas-hover/30">
                <td className="px-4 py-3">
                  <div className="text-[13px] text-ink">{user.email}</div>
                  {user.name && <div className="text-[11.5px] text-ink-dim mt-0.5">{user.name}</div>}
                </td>
                <td className="px-4 py-3">
                  <RoleSelect
                    value={user.role as UserRole}
                    onChange={(next) => changeUserRole(user, next)}
                    disabled={
                      savingRoleFor === user.id ||
                      (currentUser?.id !== undefined && String(currentUser.id) === String(user.id))
                    }
                    title={
                      currentUser?.id !== undefined && String(currentUser.id) === String(user.id)
                        ? "You can't change your own role"
                        : undefined
                    }
                  />
                </td>
                <td className="px-4 py-3">
                  <LanguagesPreview codes={user.languages || ['en-US']} />
                </td>
                <td className="px-4 py-3">
                  {user.has_subscriber ? (
                    <span className="mono text-[11.5px] text-live-soft">{user.signalwire_address || 'Yes'}</span>
                  ) : (
                    <span className="text-[12px] text-ink-dim">None</span>
                  )}
                </td>
                <td className="px-4 py-3 mono text-[11.5px] text-ink-dim">
                  {user.created_at ? new Date(user.created_at).toLocaleDateString() : '\u2014'}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => setEditingUser(user)}
                    className="inline-flex items-center gap-1.5 px-2 py-1 rounded border border-rule hover:border-rule-strong text-[11.5px] text-ink-muted hover:text-ink transition-colors"
                    title="Edit user"
                  >
                    <Pencil className="w-3 h-3" />
                    Edit
                  </button>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={6} className="text-center text-ink-dim py-8 text-[13px]">
                  No users found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {editingUser && (
        <UserEditModal
          user={editingUser}
          isSelf={currentUser?.id !== undefined && String(currentUser.id) === String(editingUser.id)}
          onClose={() => setEditingUser(null)}
          onUpdated={(next) => {
            setUsers((prev) => prev.map((u) => (u.id === next.id ? next : u)));
            setEditingUser(next);
          }}
          onDeleted={() => {
            setEditingUser(null);
            loadUsers();
          }}
        />
      )}
    </div>
  );
}

// =============================================================================
// User edit modal
// =============================================================================
// Centered + blurred backdrop. Matches ConfirmModal's pattern (same backdrop,
// same button classes, same escape-to-cancel) so the stack of two modals for
// "edit → confirm delete" reads as one surface.
//
// Scope for this iteration: role, languages, permission overrides, subscriber
// summary, delete action. Role + languages are also editable inline on the
// row as quick-edits; the modal is the canonical deep-edit surface.

function UserEditModal({
  user,
  isSelf,
  onClose,
  onUpdated,
  onDeleted,
}: {
  user: AdminUser;
  isSelf: boolean;
  onClose: () => void;
  onUpdated: (next: AdminUser) => void;
  onDeleted: () => void;
}) {
  // Draft state for batched save. Initialized from the passed-in user.
  const [draftRole, setDraftRole] = useState<UserRole>(user.role as UserRole);
  const [draftLanguages, setDraftLanguages] = useState<string[]>(user.languages || ['en-US']);
  const [draftOverrides, setDraftOverrides] = useState<Partial<Record<PermissionKey, boolean>>>(
    user.permission_overrides || {}
  );
  const [draftFactbookMode, setDraftFactbookMode] = useState<FactbookMode>(
    (user.kb_factbook_mode as FactbookMode) || 'manual'
  );
  const [draftCoachIntensity, setDraftCoachIntensity] = useState<CoachIntensity>(
    (user.coach_intensity as CoachIntensity) || 'standard'
  );
  const [saving, setSaving] = useState(false);
  const [showDeleteConfirm, setShowDeleteConfirm] = useState(false);
  // Reset-subscriber state: separate from delete-user. Resetting just nukes
  // the SignalWire subscriber binding to break a stuck WebRTC registration;
  // the User row, calls, contacts all stay. See `resetSubscriber()` below.
  const [showResetSubscriberConfirm, setShowResetSubscriberConfirm] = useState(false);
  const [resettingSubscriber, setResettingSubscriber] = useState(false);

  // Escape closes the modal. Don't rebind on every render.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape' && !showDeleteConfirm) onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose, showDeleteConfirm]);

  // Role defaults for display so the user knows what they're overriding.
  const roleDefaults: Record<PermissionKey, boolean> = useMemo(() => {
    // Mirrors ROLE_PERMISSION_DEFAULTS in backend/app/models/user.py.
    // Kept inline to avoid a second fetch; keep in sync if backend changes.
    if (draftRole === 'admin') {
      return {
        can_listen_ai_calls: true,
        can_listen_human_calls: true,
        can_whisper: true,
        can_barge: true,
        can_control_recording: true,
        can_use_coach: true,
      };
    }
    if (draftRole === 'supervisor') {
      return {
        can_listen_ai_calls: true,
        can_listen_human_calls: true,
        can_whisper: true,
        can_barge: true,
        can_control_recording: true,
        can_use_coach: true,
      };
    }
    // agent
    return {
      can_listen_ai_calls: false,
      can_listen_human_calls: false,
      can_whisper: false,
      can_barge: false,
      can_control_recording: true,
      can_use_coach: true,
    };
  }, [draftRole]);

  // Resolved value = override if present, else role default.
  const resolvedFor = (flag: PermissionKey): boolean =>
    draftOverrides[flag] !== undefined ? Boolean(draftOverrides[flag]) : Boolean(roleDefaults[flag]);

  const isOverridden = (flag: PermissionKey): boolean => draftOverrides[flag] !== undefined;

  const toggleFlag = (flag: PermissionKey) => {
    const current = resolvedFor(flag);
    const next = !current;
    setDraftOverrides((prev) => {
      const copy = { ...prev };
      // If toggling back to the role default, drop the override rather than
      // storing a redundant value. Keeps the "overridden" state meaningful.
      if (next === Boolean(roleDefaults[flag])) {
        delete copy[flag];
      } else {
        copy[flag] = next;
      }
      return copy;
    });
  };

  const resetFlag = (flag: PermissionKey) => {
    setDraftOverrides((prev) => {
      if (!(flag in prev)) return prev;
      const copy = { ...prev };
      delete copy[flag];
      return copy;
    });
  };

  const resetAllOverrides = () => setDraftOverrides({});

  // Dirty check — only hit endpoints that actually changed.
  const dirty = {
    role: draftRole !== user.role,
    languages:
      JSON.stringify((draftLanguages || []).slice().sort()) !==
      JSON.stringify((user.languages || []).slice().sort()),
    permissions:
      JSON.stringify(draftOverrides) !== JSON.stringify(user.permission_overrides || {}),
    factbookMode: draftFactbookMode !== ((user.kb_factbook_mode as FactbookMode) || 'manual'),
    coachIntensity:
      draftCoachIntensity !== ((user.coach_intensity as CoachIntensity) || 'standard'),
  };
  const hasChanges =
    dirty.role ||
    dirty.languages ||
    dirty.permissions ||
    dirty.factbookMode ||
    dirty.coachIntensity;

  const save = async () => {
    if (!hasChanges || saving) return;
    setSaving(true);
    try {
      // Sequential so we surface the first error clearly. Role must land before
      // permissions in case the role change affects role-default resolution.
      let next: AdminUser = user;
      if (dirty.role) {
        if (isSelf && draftRole !== 'admin') {
          throw new Error('You cannot change your own role away from admin');
        }
        const resp = await adminApi.updateUserRole(user.id, draftRole);
        next = resp.data.user;
      }
      if (dirty.languages) {
        if (draftLanguages.length === 0) {
          throw new Error('Pick at least one language');
        }
        const resp = await adminApi.updateUserLanguages(user.id, draftLanguages);
        next = resp.data.user;
      }
      if (dirty.permissions) {
        // Send bool values only; the backend rejects anything else.
        const clean: Record<string, boolean> = {};
        for (const [k, v] of Object.entries(draftOverrides)) {
          if (typeof v === 'boolean') clean[k] = v;
        }
        const resp = await adminApi.updateUserPermissions(user.id, clean);
        next = resp.data.user;
      }
      if (dirty.factbookMode) {
        const resp = await adminApi.updateUserKbFactbookMode(user.id, draftFactbookMode);
        next = resp.data.user;
      }
      if (dirty.coachIntensity) {
        const resp = await adminApi.updateUserCoachIntensity(user.id, draftCoachIntensity);
        next = resp.data.user;
      }
      toast.success('User updated');
      onUpdated(next);
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        (err as Error)?.message ||
        'Failed to save changes';
      toast.error(message);
    } finally {
      setSaving(false);
    }
  };

  const doDelete = async () => {
    try {
      const resp = await adminApi.deleteUser(user.id);
      toast.success(`User "${user.email}" deleted`);
      if (resp.data?.sw_warning) {
        toast(resp.data.sw_warning, { icon: '\u26a0\ufe0f' });
      }
      onDeleted();
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        'Failed to delete user';
      toast.error(message);
    }
  };

  // Recovery action: force-recreate the SignalWire subscriber. Hammer for
  // the Hagrid-side "WebRTC endpoint registration failed" (-32603) class
  // of bugs \u2014 when the SDK can't go online because of a stale device
  // binding server-side, deleting + recreating the subscriber drops it.
  //
  // When admin resets their OWN subscriber, we also clear the SDK's
  // localStorage cache and hard-reload \u2014 they're getting a clean
  // browser-side state to match the fresh server-side identity.
  // When admin resets ANOTHER user's, that user has to reload manually;
  // we just toast a reminder.
  const doResetSubscriber = async () => {
    setResettingSubscriber(true);
    try {
      const resp = await adminApi.resetUserSubscriber(user.id);
      const data = resp.data || {} as any;
      // Surface soft warnings (delete-failed / recreate-failed). The
      // operation as a whole is still success if either surfaced \u2014 local
      // state is clean, recreate just needs a sign-in to retry.
      if (data.sw_warning) {
        toast(data.sw_warning, { icon: '\u26a0\ufe0f' });
      }
      if (data.recreate_error) {
        toast(
          'Subscriber deleted but recreate failed. Sign back in to retry.',
          { icon: '\u26a0\ufe0f' },
        );
      }
      // Atomic refresh of the parent's view of this user. Backend returns
      // the full to_dict() of the user AFTER the delete + recreate ran,
      // so we don't have to synthesize anything \u2014 it has the new
      // signalwire_address, has_subscriber, etc.
      const freshUser = data.user as AdminUser | undefined;
      if (freshUser) {
        onUpdated(freshUser);
      }
      if (isSelf) {
        toast.success('Subscriber reset. Reloading\u2026');
        // Match the Reset CF button behavior: clear sw:* SDK state then
        // hard-reload so initializeClient picks up the fresh credentials
        // against the new subscriber on next mount.
        try {
          for (const k of Object.keys(localStorage)) {
            if (k.startsWith('sw:')) localStorage.removeItem(k);
          }
        } catch {
          // localStorage unavailable in some private-browsing modes
        }
        // Small delay so the success toast renders before reload.
        setTimeout(() => window.location.reload(), 600);
      } else {
        toast.success(data.message || 'Subscriber reset');
      }
      setShowResetSubscriberConfirm(false);
    } catch (err: unknown) {
      const message =
        (err as { response?: { data?: { error?: string } } })?.response?.data?.error ||
        'Failed to reset subscriber';
      toast.error(message);
    } finally {
      setResettingSubscriber(false);
    }
  };

  const toggleLanguage = (code: string) => {
    setDraftLanguages((prev) =>
      prev.includes(code) ? prev.filter((c) => c !== code) : [...prev, code]
    );
  };

  return (
    <>
      <div className="fixed inset-0 z-40 flex items-center justify-center p-4">
        <div className="absolute inset-0 bg-black/60 backdrop-blur-sm" onClick={onClose} />

        <div className="relative panel-raised rounded-md shadow-panel w-full max-w-[560px] max-h-[90vh] flex flex-col">
          {/* Header */}
          <div className="px-6 pt-5 pb-4 border-b border-rule">
            <div className="kicker mb-1">Team member</div>
            <h3 className="font-display text-[22px] text-ink leading-none tracking-tightest">
              {user.email}
            </h3>
            {user.name && (
              <p className="mt-1.5 text-[13px] text-ink-muted">{user.name}</p>
            )}
          </div>

          {/* Body */}
          <div className="flex-1 overflow-y-auto px-6 py-5 space-y-6">
            {/* Role */}
            <section>
              <div className="kicker mb-2">Role</div>
              {isSelf ? (
                <div className="flex items-center gap-2 text-[13px] text-ink-muted">
                  <RoleSelect value={draftRole} onChange={() => {}} disabled />
                  <span className="text-[11.5px]">You can\u2019t change your own role.</span>
                </div>
              ) : (
                <RoleSelect value={draftRole} onChange={setDraftRole} />
              )}
            </section>

            {/* Languages */}
            <section>
              <div className="kicker mb-2">Languages spoken</div>
              <div className="grid grid-cols-2 gap-1.5">
                {SUPPORTED_LANGUAGES.map((lang) => (
                  <label
                    key={lang.code}
                    className="flex items-center gap-2 px-2 py-1.5 rounded hover:bg-canvas-hover/40 cursor-pointer text-[12.5px]"
                  >
                    <input
                      type="checkbox"
                      checked={draftLanguages.includes(lang.code)}
                      onChange={() => toggleLanguage(lang.code)}
                      className="accent-sw-turquoise"
                    />
                    <span className="mono text-[10.5px] text-ink-dim uppercase">{lang.code}</span>
                    <span className="text-ink-muted truncate">{lang.label}</span>
                  </label>
                ))}
              </div>
            </section>

            {/* Knowledge Factbook (Agent Assist) */}
            <section>
              <div className="kicker mb-2">Knowledge factbook</div>
              <select
                value={draftFactbookMode}
                onChange={(e) => setDraftFactbookMode(e.target.value as FactbookMode)}
                className="input"
              >
                <option value="off">Off — hide the panel</option>
                <option value="manual">Manual — typed query + transcript button</option>
                <option value="auto">Auto — stream facts as the caller talks</option>
              </select>
              <p className="mt-1.5 text-[11.5px] text-ink-dim">
                Controls how this user gets KB facts surfaced during a live call.
              </p>
            </section>

            {/* AI Coach style preset.
                Whether the agent CAN use the coach at all is the
                `can_use_coach` permission flag below. Whether they're
                actively using it on a given call is an in-call toggle in the
                Live Call panel — not an admin setting. This dropdown just
                sets the prompt-tone preset the sidecar uses when attached. */}
            <section>
              <div className="kicker mb-2">AI coach style</div>
              <select
                value={draftCoachIntensity}
                onChange={(e) => setDraftCoachIntensity(e.target.value as CoachIntensity)}
                className="input"
              >
                <option value="terse">Terse — one short tip</option>
                <option value="standard">Standard — balanced suggestion</option>
                <option value="verbose">Verbose — full reasoning + script</option>
              </select>
              <p className="mt-1.5 text-[11.5px] text-ink-dim">
                Prompt-tone preset fed to the sidecar when this agent
                enables the coach on a live call.
              </p>
            </section>

            {/* Permissions */}
            <section>
              <div className="flex items-baseline justify-between mb-2">
                <div className="kicker">Permissions</div>
                {Object.keys(draftOverrides).length > 0 && (
                  <button
                    onClick={resetAllOverrides}
                    className="text-[11px] text-ink-dim hover:text-ink-muted underline-offset-2 hover:underline"
                  >
                    Reset all to {draftRole} defaults
                  </button>
                )}
              </div>
              <div className="rounded-md border border-rule divide-y divide-rule/60">
                {/* Listen — layered: enabling the category reveals its scopes.
                    The parent toggle is purely a UI device; the two scope
                    flags are the real stored permissions. */}
                <PermissionGroup
                  label="Listen to calls"
                  hint="Silently monitor calls already in progress."
                  childFlags={['can_listen_ai_calls', 'can_listen_human_calls']}
                  resolvedFor={resolvedFor}
                  isOverridden={isOverridden}
                  toggleFlag={toggleFlag}
                  resetFlag={resetFlag}
                  roleDefaults={roleDefaults}
                  draftRole={draftRole}
                  setDraftOverrides={setDraftOverrides}
                />

                {/* Standalone actions. No parent grouping because each is a
                    single discrete capability, not a scope of something else. */}
                <PermissionRow
                  flag="can_whisper"
                  resolved={resolvedFor('can_whisper')}
                  overridden={isOverridden('can_whisper')}
                  toggle={() => toggleFlag('can_whisper')}
                  reset={() => resetFlag('can_whisper')}
                  roleDefault={roleDefaults.can_whisper}
                  draftRole={draftRole}
                />
                <PermissionRow
                  flag="can_barge"
                  resolved={resolvedFor('can_barge')}
                  overridden={isOverridden('can_barge')}
                  toggle={() => toggleFlag('can_barge')}
                  reset={() => resetFlag('can_barge')}
                  roleDefault={roleDefaults.can_barge}
                  draftRole={draftRole}
                />
                <PermissionRow
                  flag="can_control_recording"
                  resolved={resolvedFor('can_control_recording')}
                  overridden={isOverridden('can_control_recording')}
                  toggle={() => toggleFlag('can_control_recording')}
                  reset={() => resetFlag('can_control_recording')}
                  roleDefault={roleDefaults.can_control_recording}
                  draftRole={draftRole}
                />
                <PermissionRow
                  flag="can_use_coach"
                  resolved={resolvedFor('can_use_coach')}
                  overridden={isOverridden('can_use_coach')}
                  toggle={() => toggleFlag('can_use_coach')}
                  reset={() => resetFlag('can_use_coach')}
                  roleDefault={roleDefaults.can_use_coach}
                  draftRole={draftRole}
                />
              </div>
            </section>

            {/* Subscriber.
                Two distinct states share one backend endpoint but present
                different intents:
                  - has_subscriber: "Reset" (destructive recovery, drops
                    active session, urgent palette)
                  - no subscriber:  "Create" (constructive setup, info
                    palette, no session to drop) */}
            <section>
              <div className="kicker mb-2">SignalWire subscriber</div>
              {user.has_subscriber ? (
                <>
                  <div className="flex items-center justify-between gap-3">
                    <span className="mono text-[12px] text-live-soft truncate">
                      {user.signalwire_address || 'Linked'}
                    </span>
                    <button
                      onClick={() => setShowResetSubscriberConfirm(true)}
                      disabled={resettingSubscriber || saving}
                      className="btn-secondary !border-urgent/40 !text-urgent-soft hover:!bg-urgent/10 disabled:opacity-50 !py-1 !px-2.5 !text-[12px] whitespace-nowrap"
                      title={
                        "Force-recreate this user's Call Fabric subscriber. " +
                        "Recovery for stuck WebRTC registrations " +
                        "(Hagrid mWebRTCEndpoints binding stuck server-side). " +
                        (isSelf
                          ? "Will clear your local Call Fabric state and reload after."
                          : "The user must reload their browser after.")
                      }
                    >
                      <RotateCcw className="w-3.5 h-3.5" />
                      {resettingSubscriber ? 'Resetting…' : 'Reset subscriber'}
                    </button>
                  </div>
                  <p className="mt-1.5 text-[11px] text-ink-dim">
                    Drops the existing subscriber and mints a fresh one.
                    Recovery for stuck "WebRTC endpoint registration failed"
                    errors. {isSelf ? 'Your browser will reload after.' : 'The user must reload their browser to pick it up.'}
                  </p>
                </>
              ) : (
                <>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[12px] text-ink-dim">
                      No subscriber linked yet.
                    </span>
                    <button
                      onClick={() => setShowResetSubscriberConfirm(true)}
                      disabled={resettingSubscriber || saving}
                      className="btn-secondary !border-info/40 !text-info-soft hover:!bg-info/10 disabled:opacity-50 !py-1 !px-2.5 !text-[12px] whitespace-nowrap"
                      title={
                        "Mint a Call Fabric subscriber for this user now. " +
                        "Normally one is created on first sign-in; use this " +
                        "to provision ahead of time or to recover from an " +
                        "incomplete prior reset."
                      }
                    >
                      <Plus className="w-3.5 h-3.5" />
                      {resettingSubscriber ? 'Creating…' : 'Create subscriber'}
                    </button>
                  </div>
                  <p className="mt-1.5 text-[11px] text-ink-dim">
                    Auto-created on first sign-in, or click to provision now.
                  </p>
                </>
              )}
            </section>
          </div>

          {/* Footer */}
          <div className="px-6 py-4 border-t border-rule flex items-center gap-2">
            <button
              onClick={() => setShowDeleteConfirm(true)}
              disabled={isSelf || saving}
              className="btn-danger"
              title={isSelf ? 'You cannot delete yourself' : 'Permanently delete this user'}
            >
              <Trash2 className="w-3.5 h-3.5" />
              Delete user
            </button>
            <div className="flex-1" />
            <button onClick={onClose} className="btn-ghost" disabled={saving}>
              Cancel
            </button>
            <button
              onClick={save}
              disabled={!hasChanges || saving}
              className="btn-primary"
            >
              {saving ? 'Saving\u2026' : 'Save changes'}
            </button>
          </div>
        </div>
      </div>

      {showDeleteConfirm && (
        <ConfirmModal
          title="Delete user"
          message={`Permanently delete "${user.email}"${user.has_subscriber ? ' and their SignalWire subscriber' : ''}? This will also delete all their calls and cannot be undone.`}
          onConfirm={async () => {
            await doDelete();
            setShowDeleteConfirm(false);
          }}
          onCancel={() => setShowDeleteConfirm(false)}
        />
      )}
      {showResetSubscriberConfirm && (
        <ConfirmModal
          title={user.has_subscriber ? 'Reset SignalWire subscriber' : 'Create SignalWire subscriber'}
          message={
            user.has_subscriber
              ? (isSelf
                  ? `Reset YOUR OWN SignalWire subscriber? This will drop your active Call Fabric session immediately, clear cached SDK state, and reload your browser. Use this when "Go Online" fails with a WebRTC registration error.`
                  : `Reset ${user.email}'s SignalWire subscriber? Their current Call Fabric session will drop immediately and they must reload their browser to get a fresh subscriber. The user account, calls, and contacts stay intact.`)
              : (isSelf
                  ? `Mint a Call Fabric subscriber for yourself now? Your browser will reload to pick up the new identity.`
                  : `Mint a Call Fabric subscriber for ${user.email}? They'll need to reload their browser (or sign in) to pick it up.`)
          }
          onConfirm={doResetSubscriber}
          onCancel={() => setShowResetSubscriberConfirm(false)}
        />
      )}
    </>
  );
}

// ── Permission row primitives ────────────────────────────────────────────────
// Shared toggle + label + overridden-badge atom used by both flat flags and
// group children. Keeps the visual language identical across both surfaces.

function PermissionToggle({
  on,
  onClick,
  label,
}: {
  on: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      className={`mt-0.5 w-8 h-5 rounded-full flex-shrink-0 relative transition-colors ${
        on ? 'bg-sw-fuchsia' : 'bg-canvas-sunken border border-rule'
      }`}
      aria-label={`${label} ${on ? 'on' : 'off'}`}
    >
      <span
        className={`absolute top-0.5 w-3.5 h-3.5 rounded-full bg-ink transition-all ${
          on ? 'left-4' : 'left-0.5'
        }`}
      />
    </button>
  );
}

function PermissionRow({
  flag,
  resolved,
  overridden,
  toggle,
  reset,
  roleDefault,
  draftRole,
  indent = false,
}: {
  flag: PermissionKey;
  resolved: boolean;
  overridden: boolean;
  toggle: () => void;
  reset: () => void;
  roleDefault: boolean;
  draftRole: UserRole;
  indent?: boolean;
}) {
  const meta = PERMISSION_LABELS[flag];
  return (
    <div className={`flex items-start gap-3 px-3 py-2.5 ${indent ? 'pl-10' : ''}`}>
      <PermissionToggle on={resolved} onClick={toggle} label={meta.label} />
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="text-[13px] text-ink">{meta.label}</span>
          {overridden && (
            <span className="mono text-[9.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-signal/15 text-signal border border-signal/30">
              overridden
            </span>
          )}
        </div>
        <div className="text-[11.5px] text-ink-dim mt-0.5 leading-relaxed">
          {meta.hint}
        </div>
      </div>
      {overridden && (
        <button
          onClick={reset}
          className="text-[11px] text-ink-dim hover:text-ink-muted underline-offset-2 hover:underline flex-shrink-0"
          title={`Reset to ${draftRole} default (${roleDefault ? 'on' : 'off'})`}
        >
          reset
        </button>
      )}
    </div>
  );
}

// Parent toggle + child scopes. The parent is purely a UI device — stored state
// remains at the child level. Toggling the parent OFF clears all children;
// toggling ON (when all children are currently off) enables every child so
// the user gets the broadest scope by default and narrows from there.
function PermissionGroup({
  label,
  hint,
  childFlags,
  resolvedFor,
  isOverridden,
  toggleFlag,
  resetFlag,
  roleDefaults,
  draftRole,
  setDraftOverrides,
}: {
  label: string;
  hint: string;
  childFlags: PermissionKey[];
  resolvedFor: (flag: PermissionKey) => boolean;
  isOverridden: (flag: PermissionKey) => boolean;
  toggleFlag: (flag: PermissionKey) => void;
  resetFlag: (flag: PermissionKey) => void;
  roleDefaults: Record<PermissionKey, boolean>;
  draftRole: UserRole;
  setDraftOverrides: React.Dispatch<
    React.SetStateAction<Partial<Record<PermissionKey, boolean>>>
  >;
}) {
  const parentOn = childFlags.some((f) => resolvedFor(f));
  const anyChildOverridden = childFlags.some((f) => isOverridden(f));

  const toggleParent = () => {
    if (parentOn) {
      // Turn category off: force every child to false. Store an override
      // only when that differs from the role default (keep `permissions`
      // minimal and "overridden" badges accurate).
      setDraftOverrides((prev) => {
        const copy = { ...prev };
        for (const f of childFlags) {
          if (roleDefaults[f] === false) delete copy[f];
          else copy[f] = false;
        }
        return copy;
      });
    } else {
      // Turn category on: broadest scope by default (every child true).
      setDraftOverrides((prev) => {
        const copy = { ...prev };
        for (const f of childFlags) {
          if (roleDefaults[f] === true) delete copy[f];
          else copy[f] = true;
        }
        return copy;
      });
    }
  };

  return (
    <div className="px-3 py-2.5">
      {/* Parent row */}
      <div className="flex items-start gap-3">
        <PermissionToggle on={parentOn} onClick={toggleParent} label={label} />
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-[13px] text-ink font-medium">{label}</span>
            {anyChildOverridden && (
              <span className="mono text-[9.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-signal/15 text-signal border border-signal/30">
                overridden
              </span>
            )}
          </div>
          <div className="text-[11.5px] text-ink-dim mt-0.5 leading-relaxed">
            {hint}
          </div>
        </div>
      </div>

      {/* Scope children — only when the category is enabled. */}
      {parentOn && (
        <div className="mt-2 ml-10 pl-3 border-l border-rule/60">
          <div className="kicker mb-1 text-[9.5px]">Scope</div>
          <div className="-ml-3">
            {childFlags.map((flag) => (
              <PermissionRow
                key={flag}
                flag={flag}
                resolved={resolvedFor(flag)}
                overridden={isOverridden(flag)}
                toggle={() => toggleFlag(flag)}
                reset={() => resetFlag(flag)}
                roleDefault={roleDefaults[flag]}
                draftRole={draftRole}
                indent
              />
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// Read-only language preview for the user row. Editing happens in the user
// edit modal — the row is just scan-and-recognize. First two codes as pills,
// then a "+N" chip for overflow (hover reveals the full list).
function LanguagesPreview({ codes }: { codes: string[] }) {
  const shown = codes.slice(0, 2);
  const extra = codes.slice(2);
  return (
    <div className="inline-flex items-center gap-1 max-w-[200px]">
      {shown.map((code) => (
        <span
          key={code}
          className="mono text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-canvas-elevated border border-rule text-ink"
        >
          {code}
        </span>
      ))}
      {extra.length > 0 && (
        <span
          className="mono text-[10.5px] uppercase tracking-wider px-1.5 py-0.5 rounded bg-ai/15 border border-ai/30 text-ai-soft"
          title={extra.join(', ')}
        >
          +{extra.length}
        </span>
      )}
    </div>
  );
}


function RoleSelect({
  value,
  onChange,
  disabled,
  title,
}: {
  value: UserRole;
  onChange: (next: UserRole) => void;
  disabled?: boolean;
  title?: string;
}) {
  const style =
    value === 'admin'
      ? 'bg-canvas-elevated text-ink border-rule-strong'
      : value === 'supervisor'
        ? 'bg-ai/10 text-ai-soft border-ai/30'
        : 'bg-info/10 text-info-soft border-info/30';
  return (
    <select
      value={value}
      disabled={disabled}
      title={title}
      onChange={(e) => onChange(e.target.value as UserRole)}
      className={`px-2 py-1 text-[11.5px] font-medium rounded border capitalize cursor-pointer disabled:cursor-not-allowed disabled:opacity-60 mono uppercase tracking-wider ${style}`}
    >
      {ROLE_OPTIONS.map((r) => (
        <option key={r} value={r} className="bg-canvas text-ink">
          {r}
        </option>
      ))}
    </select>
  );
}


// =============================================================================
// Shared Components
// =============================================================================

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <Loader2 className="w-5 h-5 animate-spin text-sw-blue" />
    </div>
  );
}
