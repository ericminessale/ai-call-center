import { useState, useEffect, useCallback } from 'react';
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
} from 'lucide-react';
import { adminApi } from '../../services/api';
import toast from 'react-hot-toast';
import { ConfirmModal } from '../shared/ConfirmModal';

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
}

interface PhoneNumber {
  sid: string;
  phone_number: string;
  friendly_name: string;
  voice_url: string;
  status_callback: string;
  is_assigned: boolean;
}

interface QueueConfig {
  id: number;
  slug: string;
  display_name: string;
  description: string | null;
  is_active: boolean;
  routing_strategy: string;
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

type SettingsTabId = 'phone-numbers' | 'queues' | 'agents' | 'knowledge' | 'users';


// =============================================================================
// Main Settings Panel
// =============================================================================

export function SettingsPanel() {
  const [activeTab, setActiveTab] = useState<SettingsTabId>('phone-numbers');
  // Cross-tab navigation: AgentsTab can request opening a collection in KnowledgeBaseTab
  const [focusCollectionId, setFocusCollectionId] = useState<number | null>(null);

  const handleNavigateToCollection = (collectionId: number) => {
    setFocusCollectionId(collectionId);
    setActiveTab('knowledge');
  };

  const handleTabChange = (tab: SettingsTabId) => {
    if (tab !== 'knowledge') setFocusCollectionId(null);
    setActiveTab(tab);
  };

  return (
    <div className="h-full flex flex-col text-white">
      {/* Tab navigation */}
      <div className="bg-gray-800 border-b border-gray-700 px-6">
        <nav className="flex gap-1">
          <TabButton
            id="phone-numbers"
            icon={<Phone className="w-4 h-4" />}
            label="Phone Numbers"
            active={activeTab === 'phone-numbers'}
            onClick={() => handleTabChange('phone-numbers')}
          />
          <TabButton
            id="queues"
            icon={<LayoutList className="w-4 h-4" />}
            label="Queues"
            active={activeTab === 'queues'}
            onClick={() => handleTabChange('queues')}
          />
          <TabButton
            id="agents"
            icon={<Bot className="w-4 h-4" />}
            label="AI Agents"
            active={activeTab === 'agents'}
            onClick={() => handleTabChange('agents')}
          />
          <TabButton
            id="knowledge"
            icon={<BookOpen className="w-4 h-4" />}
            label="Knowledge Base"
            active={activeTab === 'knowledge'}
            onClick={() => handleTabChange('knowledge')}
          />
          <TabButton
            id="users"
            icon={<Users className="w-4 h-4" />}
            label="User Management"
            active={activeTab === 'users'}
            onClick={() => handleTabChange('users')}
          />
        </nav>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'phone-numbers' && <PhoneNumbersTab />}
        {activeTab === 'queues' && <QueuesTab />}
        {activeTab === 'agents' && <AgentsTab onNavigateToCollection={handleNavigateToCollection} />}
        {activeTab === 'knowledge' && <KnowledgeBaseTab focusCollectionId={focusCollectionId} />}
        {activeTab === 'users' && <UserManagementTab />}
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
      className={`flex items-center gap-2 px-4 py-3 text-sm font-medium border-b-2 transition-colors ${
        active
          ? 'border-blue-500 text-blue-400'
          : 'border-transparent text-gray-400 hover:text-gray-300 hover:border-gray-600'
      }`}
    >
      {icon}
      {label}
    </button>
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
  const [webhookUrl, setWebhookUrl] = useState('');
  const [isConfigured, setIsConfigured] = useState(true);
  const [loading, setLoading] = useState(true);
  const [updatingNumber, setUpdatingNumber] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const loadNumbers = useCallback(async () => {
    try {
      const resp = await adminApi.getPhoneNumbers();
      setNumbers(resp.data.phone_numbers);
      setWebhookUrl(resp.data.webhook_url);
      setIsConfigured(resp.data.is_configured);
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to load phone numbers');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadNumbers(); }, [loadNumbers]);

  const handleToggleAssign = async (number: PhoneNumber) => {
    setUpdatingNumber(number.sid);
    const action = number.is_assigned ? 'unassign' : 'assign';
    try {
      await adminApi.updatePhoneNumber(number.sid, action);
      toast.success(
        `${formatPhoneNumber(number.phone_number)} ${action === 'assign' ? 'assigned to' : 'unassigned from'} call center`
      );
      await loadNumbers();
    } catch (err: any) {
      toast.error(err.response?.data?.error || `Failed to ${action} phone number`);
    } finally {
      setUpdatingNumber(null);
    }
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

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-4xl">
      <div className="mb-6">
        <h2 className="text-xl font-semibold mb-1">Phone Number Management</h2>
        <p className="text-sm text-gray-400">
          Configure which phone numbers route inbound calls to the call center AI agent.
        </p>
      </div>

      {/* Warning if EXTERNAL_URL not configured */}
      {!isConfigured && (
        <div className="bg-red-900/20 border border-red-800/50 rounded-lg p-3 mb-6 flex items-start gap-2">
          <AlertCircle className="w-4 h-4 text-red-400 mt-0.5 flex-shrink-0" />
          <p className="text-sm text-red-300/80">
            EXTERNAL_URL environment variable is not set. Phone number assignment requires a publicly accessible URL.
            Set it in your .env file and restart the backend.
          </p>
        </div>
      )}

      {/* Webhook URL info box */}
      {webhookUrl && (
        <div className="bg-gray-800 rounded-lg p-4 mb-6 border border-gray-700">
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <ExternalLink className="w-4 h-4 text-gray-400 flex-shrink-0" />
              <span className="text-sm text-gray-400">Webhook URL:</span>
              <code className="text-sm text-blue-400 font-mono truncate">{webhookUrl}</code>
            </div>
            <button
              onClick={handleCopyWebhookUrl}
              className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm transition-colors flex-shrink-0 ml-3"
            >
              {copied ? (
                <>
                  <Check className="w-3.5 h-3.5 text-green-400" />
                  <span className="text-green-400">Copied</span>
                </>
              ) : (
                <>
                  <Copy className="w-3.5 h-3.5 text-gray-400" />
                  <span className="text-gray-300">Copy</span>
                </>
              )}
            </button>
          </div>
          <p className="text-xs text-gray-500 mt-2">
            Assigned numbers will have their inbound voice webhook set to this URL.
          </p>
        </div>
      )}

      {/* Phone numbers table */}
      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-700">
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Phone Number</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Name</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Status</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Current Webhook</th>
              <th className="text-right px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Action</th>
            </tr>
          </thead>
          <tbody>
            {numbers.map(number => {
              const isUpdating = updatingNumber === number.sid;
              const hasExternalWebhook = !number.is_assigned && number.voice_url && number.voice_url.length > 0;

              return (
                <tr key={number.sid} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                  <td className="px-4 py-3">
                    <div className="text-sm font-medium font-mono text-white">
                      {formatPhoneNumber(number.phone_number)}
                    </div>
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-sm text-white">
                      {number.friendly_name || '\u2014'}
                    </span>
                  </td>
                  <td className="px-4 py-3">
                    {number.is_assigned ? (
                      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded-full bg-green-900/30 text-green-400">
                        <span className="w-1.5 h-1.5 rounded-full bg-green-400" />
                        Assigned
                      </span>
                    ) : hasExternalWebhook ? (
                      <span
                        className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded-full bg-yellow-900/30 text-yellow-400 cursor-help"
                        title={`Current webhook: ${number.voice_url}`}
                      >
                        <span className="w-1.5 h-1.5 rounded-full bg-yellow-400" />
                        External
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1.5 px-2 py-0.5 text-xs rounded-full bg-gray-700 text-gray-400">
                        <span className="w-1.5 h-1.5 rounded-full bg-gray-500" />
                        Not Configured
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-3">
                    <span className="text-xs text-gray-400 font-mono truncate block max-w-[250px]" title={number.voice_url || 'None'}>
                      {number.voice_url || '\u2014'}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-right">
                    <button
                      onClick={() => handleToggleAssign(number)}
                      disabled={isUpdating || !isConfigured}
                      className={`inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-medium transition-colors disabled:opacity-50 ${
                        number.is_assigned
                          ? 'bg-gray-700 hover:bg-gray-600 text-gray-300 border border-gray-600'
                          : 'bg-blue-600 hover:bg-blue-700 text-white'
                      }`}
                    >
                      {isUpdating ? (
                        <Loader2 className="w-3.5 h-3.5 animate-spin" />
                      ) : null}
                      {number.is_assigned ? 'Unassign' : 'Assign'}
                    </button>
                  </td>
                </tr>
              );
            })}
            {numbers.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-gray-500 py-8 text-sm">
                  No phone numbers found in your SignalWire space.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {numbers.length > 0 && (
        <p className="text-xs text-gray-500 mt-3">
          {numbers.filter(n => n.is_assigned).length} of {numbers.length} numbers assigned to call center
        </p>
      )}
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
      ai_agent_route: q.ai_agent_route,
      default_priority: q.default_priority,
      sla_threshold_seconds: q.sla_threshold_seconds,
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
    <div className="flex gap-6 h-full">
      {/* Left: Queue list */}
      <div className="w-72 flex-shrink-0">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Queues</h2>
          <button
            onClick={() => setShowNewQueue(true)}
            className="p-1.5 hover:bg-gray-700 rounded-lg transition-colors text-blue-400"
            title="New Queue"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        {showNewQueue && (
          <div className="bg-gray-800 rounded-lg p-3 mb-3 border border-gray-700 space-y-2">
            <input
              value={newSlug}
              onChange={e => setNewSlug(e.target.value.toLowerCase().replace(/[^a-z0-9_-]/g, '-'))}
              placeholder="queue-slug"
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
            />
            <input
              value={newDisplayName}
              onChange={e => setNewDisplayName(e.target.value)}
              placeholder="Display Name"
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
            />
            <select
              value={newStrategy}
              onChange={e => setNewStrategy(e.target.value)}
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
            >
              {ROUTING_STRATEGIES.map(s => (
                <option key={s.value} value={s.value}>{s.label}</option>
              ))}
            </select>
            <div className="flex gap-2">
              <button onClick={handleCreateQueue} className="px-3 py-1 bg-blue-600 hover:bg-blue-700 rounded text-xs">Create</button>
              <button onClick={() => setShowNewQueue(false)} className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs">Cancel</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {queues.map(q => (
            <div
              key={q.id}
              onClick={() => handleSelectQueue(q)}
              className={`p-3 rounded-lg cursor-pointer border transition-colors ${
                selectedQueue?.id === q.id
                  ? 'bg-gray-700 border-blue-500'
                  : 'bg-gray-800 border-gray-700 hover:border-gray-600'
              }`}
            >
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <span className={`w-2 h-2 rounded-full ${q.is_active ? 'bg-green-400' : 'bg-gray-500'}`} />
                  <span className="text-sm font-medium">{q.display_name}</span>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); setPendingDelete(q); }}
                  className="p-1 hover:bg-gray-600 rounded text-gray-500 hover:text-red-400 transition-colors"
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              </div>
              <div className="flex items-center gap-2 mt-1.5">
                <span className="px-1.5 py-0.5 text-[10px] font-medium rounded bg-blue-600/30 text-blue-200">
                  {ROUTING_STRATEGIES.find(s => s.value === q.routing_strategy)?.label || q.routing_strategy}
                </span>
                {q.agent_count !== undefined && (
                  <span className="text-[10px] text-gray-300">{q.agent_count} agents</span>
                )}
              </div>
            </div>
          ))}
          {queues.length === 0 && (
            <p className="text-sm text-gray-400 text-center py-4">No queues configured.</p>
          )}
        </div>
      </div>

      {/* Right: Queue detail form */}
      <div className="flex-1 min-w-0">
        {selectedQueue ? (
          <div className="space-y-6">
            <div>
              <h2 className="text-lg font-semibold text-white">{selectedQueue.display_name}</h2>
            </div>

            {/* Settings form */}
            <div className="grid grid-cols-2 gap-4">
              <div>
                <label className="block text-xs text-gray-400 mb-1">Display Name</label>
                <input
                  value={editForm.display_name || ''}
                  onChange={e => setEditForm({ ...editForm, display_name: e.target.value })}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Routing Strategy</label>
                <select
                  value={editForm.routing_strategy || 'round_robin'}
                  onChange={e => setEditForm({ ...editForm, routing_strategy: e.target.value })}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white"
                >
                  {ROUTING_STRATEGIES.map(s => (
                    <option key={s.value} value={s.value}>{s.label}</option>
                  ))}
                </select>
                <p className="text-[10px] text-gray-400 mt-1">
                  {ROUTING_STRATEGIES.find(s => s.value === editForm.routing_strategy)?.desc}
                </p>
              </div>
              <div className="col-span-2">
                <label className="block text-xs text-gray-400 mb-1">Description</label>
                <textarea
                  value={editForm.description || ''}
                  onChange={e => setEditForm({ ...editForm, description: e.target.value })}
                  rows={2}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white resize-none"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">AI Agent Route</label>
                <select
                  value={editForm.ai_agent_route || ''}
                  onChange={e => setEditForm({ ...editForm, ai_agent_route: e.target.value || null })}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white"
                >
                  <option value="">None</option>
                  {agents.map(a => (
                    <option key={a.id} value={a.route}>{a.name} ({a.route})</option>
                  ))}
                </select>
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">Default Priority (1-10)</label>
                <input
                  type="number"
                  min={1}
                  max={10}
                  value={editForm.default_priority || 5}
                  onChange={e => setEditForm({ ...editForm, default_priority: parseInt(e.target.value) || 5 })}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white"
                />
              </div>
              <div>
                <label className="block text-xs text-gray-400 mb-1">SLA Threshold (seconds)</label>
                <input
                  type="number"
                  min={0}
                  value={editForm.sla_threshold_seconds || 60}
                  onChange={e => setEditForm({ ...editForm, sla_threshold_seconds: parseInt(e.target.value) || 60 })}
                  className="w-full bg-gray-700 border border-gray-600 rounded px-3 py-2 text-sm text-white"
                />
              </div>
              <div className="flex items-center gap-2">
                <input
                  type="checkbox"
                  checked={editForm.is_active ?? true}
                  onChange={e => setEditForm({ ...editForm, is_active: e.target.checked })}
                  className="rounded bg-gray-700 border-gray-600"
                />
                <label className="text-sm text-gray-300">Active</label>
              </div>
            </div>

            <button
              onClick={handleSaveQueue}
              disabled={saving}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Save Queue Settings
            </button>

          </div>
        ) : (
          <div className="flex items-center justify-center h-64 text-gray-500">
            <div className="text-center">
              <LayoutList className="w-12 h-12 mx-auto mb-3 text-gray-600" />
              <p className="text-sm">Select a queue to configure</p>
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
      <div className="w-72 flex-shrink-0">
        <div className="mb-4">
          <h2 className="text-lg font-semibold text-white">AI Agents</h2>
          <p className="text-xs text-gray-400 mt-1">Manage knowledge base assignments per agent</p>
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
                className={`w-full text-left p-3 rounded-lg cursor-pointer border transition-colors ${
                  selectedAgent?.id === agent.id
                    ? 'bg-gray-700 border-blue-500'
                    : 'bg-gray-800 border-gray-700 hover:border-gray-600'
                }`}
              >
                <div className="flex items-center gap-2">
                  <span className="text-sm font-medium text-white">{agent.name}</span>
                  {isOutbound && (
                    <span className="inline-flex items-center gap-1 px-1.5 py-0.5 text-[10px] font-medium rounded bg-sky-900/40 text-sky-300">
                      <PhoneOutgoing className="w-2.5 h-2.5" />
                      Outbound
                    </span>
                  )}
                </div>
                <div className="text-xs text-gray-400 font-mono mt-0.5">{agent.route}</div>

                {assignedColl ? (
                  <div className="flex items-center gap-1 mt-2">
                    <BookOpen className="w-3 h-3 text-blue-400" />
                    <span className="text-[10px] text-blue-300">{assignedColl.display_name}</span>
                  </div>
                ) : (
                  <div className="flex items-center gap-1 mt-2">
                    <BookOpen className="w-3 h-3 text-gray-600" />
                    <span className="text-[10px] text-gray-500">No knowledge base</span>
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
            {/* Agent header */}
            <div>
              <div className="flex items-center gap-3">
                <h2 className="text-lg font-semibold text-white">{selectedAgent.name}</h2>
                {selectedAgent.type === 'outbound' && (
                  <span className="inline-flex items-center gap-1 px-2 py-0.5 text-xs font-medium rounded bg-sky-900/40 text-sky-300">
                    <PhoneOutgoing className="w-3 h-3" />
                    Outbound
                  </span>
                )}
              </div>
              <p className="text-xs text-gray-400 font-mono mt-1">{selectedAgent.route}</p>
              <p className="text-sm text-gray-400 mt-2">{selectedAgent.description}</p>
            </div>

            {/* Section: Knowledge Base */}
            <div>
              <h3 className="text-sm font-medium text-gray-300 mb-2">Knowledge Base</h3>
              <p className="text-xs text-gray-400 mb-3">
                Assign a document collection for this agent to search during conversations.
              </p>
              <div className="flex items-center gap-2">
                <select
                  value={assignments[selectedAgent.id] || 0}
                  onChange={e => handleAssignmentChange(parseInt(e.target.value))}
                  className="flex-1 bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white focus:outline-none focus:border-blue-500"
                >
                  <option value={0}>None</option>
                  {collections.map(coll => (
                    <option key={coll.id} value={coll.id}>{coll.display_name}</option>
                  ))}
                </select>
                {assignments[selectedAgent.id] > 0 && (
                  <button
                    onClick={() => onNavigateToCollection(assignments[selectedAgent.id])}
                    className="p-2 hover:bg-gray-700 rounded-lg transition-colors text-gray-400 hover:text-blue-400"
                    title="Edit collection in Knowledge Base"
                  >
                    <Pencil className="w-4 h-4" />
                  </button>
                )}
              </div>
            </div>

            {/* Restart warning */}
            <div className="bg-yellow-900/20 border border-yellow-800/50 rounded-lg p-3 flex items-start gap-2">
              <Info className="w-4 h-4 text-yellow-500 mt-0.5 flex-shrink-0" />
              <p className="text-sm text-yellow-300/80">
                Knowledge base changes require an agent container restart to take effect.
                Call routing is configured per-queue in the Queues tab.
              </p>
            </div>

            {/* Save button */}
            <button
              onClick={handleSave}
              disabled={saving || !hasChanges}
              className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
            >
              {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
              Save Configuration
            </button>
          </div>
        ) : (
          <div className="flex items-center justify-center h-64 text-gray-500">
            <div className="text-center">
              <Bot className="w-12 h-12 mx-auto mb-3 text-gray-600" />
              <p className="text-sm">Select an agent to configure</p>
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
      <div className="w-72 flex-shrink-0">
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-semibold">Collections</h2>
          <button
            onClick={() => setShowNewCollection(true)}
            className="p-1.5 hover:bg-gray-700 rounded-lg transition-colors text-blue-400"
            title="New Collection"
          >
            <Plus className="w-4 h-4" />
          </button>
        </div>

        {showNewCollection && (
          <div className="bg-gray-800 rounded-lg p-3 mb-3 border border-gray-700 space-y-2">
            <input
              value={newCollName}
              onChange={e => setNewCollName(e.target.value.toLowerCase().replace(/[^a-z0-9_]/g, '_'))}
              placeholder="collection_name"
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
            />
            <input
              value={newCollDisplayName}
              onChange={e => setNewCollDisplayName(e.target.value)}
              placeholder="Display Name"
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white"
            />
            <textarea
              value={newCollDescription}
              onChange={e => setNewCollDescription(e.target.value)}
              placeholder="Description"
              rows={2}
              className="w-full bg-gray-700 border border-gray-600 rounded px-2 py-1.5 text-sm text-white resize-none"
            />
            <div className="flex gap-2">
              <button onClick={handleCreateCollection} className="px-3 py-1 bg-blue-600 hover:bg-blue-700 rounded text-xs">Create</button>
              <button onClick={() => setShowNewCollection(false)} className="px-3 py-1 bg-gray-700 hover:bg-gray-600 rounded text-xs">Cancel</button>
            </div>
          </div>
        )}

        <div className="space-y-2">
          {collections.map(coll => (
            <div
              key={coll.id}
              onClick={() => handleSelectCollection(coll)}
              className={`p-3 rounded-lg border cursor-pointer transition-colors ${
                selectedCollection?.id === coll.id
                  ? 'bg-gray-700 border-blue-500'
                  : 'bg-gray-800 border-gray-700 hover:border-gray-600'
              }`}
            >
              <div className="flex items-start justify-between">
                <div>
                  <div className="text-sm font-medium">{coll.display_name}</div>
                  <div className="text-xs text-gray-400 mt-0.5">{coll.name}</div>
                  <div className="text-xs text-gray-400 mt-1">{coll.document_count} documents</div>
                </div>
                <button
                  onClick={e => { e.stopPropagation(); handleDeleteCollection(coll); }}
                  className="p-1 hover:bg-gray-600 rounded text-gray-500 hover:text-red-400"
                >
                  <Trash2 className="w-3 h-3" />
                </button>
              </div>
              {coll.description && (
                <div className="text-xs text-gray-400 mt-1">{coll.description}</div>
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
                <h2 className="text-lg font-semibold">{selectedCollection.display_name}</h2>
                <p className="text-xs text-gray-400">{selectedCollection.description}</p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  onClick={() => { setNewDocMode(true); setEditingDoc(null); }}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm transition-colors"
                >
                  <Plus className="w-4 h-4" />
                  New Document
                </button>
                <button
                  onClick={handleReindex}
                  disabled={reindexing || documents.length === 0}
                  className="flex items-center gap-1.5 px-3 py-1.5 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
                >
                  {reindexing ? <Loader2 className="w-4 h-4 animate-spin" /> : <RefreshCw className="w-4 h-4" />}
                  Publish Changes
                </button>
              </div>
            </div>

            {reindexResult && (
              <div className={`mb-4 p-3 rounded-lg text-sm flex items-center gap-2 ${
                reindexResult.startsWith('Error')
                  ? 'bg-red-900/30 border border-red-800 text-red-300'
                  : 'bg-green-900/30 border border-green-800 text-green-300'
              }`}>
                {reindexResult.startsWith('Error')
                  ? <AlertCircle className="w-4 h-4 flex-shrink-0" />
                  : <Check className="w-4 h-4 flex-shrink-0" />
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
                  className={`flex items-center justify-between p-2.5 rounded-lg cursor-pointer transition-colors ${
                    editingDoc?.id === doc.id
                      ? 'bg-gray-700 border border-blue-500'
                      : 'bg-gray-800 border border-gray-700 hover:border-gray-600'
                  }`}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileText className="w-4 h-4 text-gray-500 flex-shrink-0" />
                    <span className="text-sm truncate">{doc.title}</span>
                    {doc.is_published && (
                      <span className="px-1.5 py-0.5 text-xs rounded bg-green-900/30 text-green-400">Published</span>
                    )}
                  </div>
                  <div className="flex items-center gap-2">
                    <span className="text-xs text-gray-500">
                      {new Date(doc.updated_at).toLocaleDateString()}
                    </span>
                    <button
                      onClick={e => { e.stopPropagation(); handleDeleteDocument(doc); }}
                      className="p-1 hover:bg-gray-600 rounded text-gray-500 hover:text-red-400"
                    >
                      <Trash2 className="w-3 h-3" />
                    </button>
                  </div>
                </div>
              ))}
              {documents.length === 0 && (
                <div className="text-center text-gray-500 py-8 text-sm">
                  No documents yet. Click "New Document" to add one.
                </div>
              )}
            </div>

            {/* New document form */}
            {newDocMode && (
              <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-medium">New Document</h3>
                  <label className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded-lg text-xs text-gray-300 cursor-pointer transition-colors">
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
                  placeholder="Document Title"
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white mb-3"
                />
                <textarea
                  value={newDocContent}
                  onChange={e => setNewDocContent(e.target.value)}
                  placeholder="Document content... (sales scripts, troubleshooting guides, product info, etc.)"
                  rows={12}
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white font-mono resize-y"
                />
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={handleCreateDocument}
                    disabled={!newDocTitle || !newDocContent}
                    className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium"
                  >
                    Create Document
                  </button>
                  <button
                    onClick={() => setNewDocMode(false)}
                    className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}

            {/* Edit document form */}
            {editingDoc && !newDocMode && (
              <div className="bg-gray-800 rounded-lg p-4 border border-gray-700">
                <div className="flex items-center justify-between mb-3">
                  <h3 className="text-sm font-medium">Edit Document</h3>
                  <label className="flex items-center gap-1.5 px-3 py-1.5 bg-gray-700 hover:bg-gray-600 rounded-lg text-xs text-gray-300 cursor-pointer transition-colors">
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
                        } catch (err) {
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
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white mb-3"
                />
                <textarea
                  value={editingDoc.content}
                  onChange={e => setEditingDoc({ ...editingDoc, content: e.target.value })}
                  rows={12}
                  className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-sm text-white font-mono resize-y"
                />
                <div className="flex gap-2 mt-3">
                  <button
                    onClick={handleSaveDocument}
                    className="px-4 py-2 bg-blue-600 hover:bg-blue-700 rounded-lg text-sm font-medium"
                  >
                    Save Changes
                  </button>
                  <button
                    onClick={() => setEditingDoc(null)}
                    className="px-4 py-2 bg-gray-700 hover:bg-gray-600 rounded-lg text-sm"
                  >
                    Cancel
                  </button>
                </div>
              </div>
            )}
          </>
        ) : (
          <div className="flex flex-col items-center justify-center h-64 text-gray-500">
            <BookOpen className="w-12 h-12 mb-3 opacity-50" />
            <p className="text-sm">Select a collection to manage its documents</p>
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

function UserManagementTab() {
  const [users, setUsers] = useState<AdminUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [pendingDelete, setPendingDelete] = useState<AdminUser | null>(null);

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

  const confirmDeleteUser = async (user: AdminUser) => {
    try {
      const resp = await adminApi.deleteUser(user.id);
      toast.success(`User "${user.email}" deleted`);
      if (resp.data.sw_warning) {
        toast(resp.data.sw_warning, { icon: '\u26a0\ufe0f' });
      }
      loadUsers();
    } catch (err: any) {
      toast.error(err.response?.data?.error || 'Failed to delete user');
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-4xl">
      <div className="mb-6">
        <h2 className="text-xl font-semibold mb-1">User Management</h2>
        <p className="text-sm text-gray-400">View and manage user accounts. Deleting a user removes their account, calls, and SignalWire subscriber.</p>
      </div>

      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-700">
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">User</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Role</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Subscriber</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Created</th>
              <th className="text-right px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map(user => (
              <tr key={user.id} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                <td className="px-4 py-3">
                  <div className="text-sm font-medium">{user.email}</div>
                  {user.name && <div className="text-xs text-gray-400">{user.name}</div>}
                </td>
                <td className="px-4 py-3">
                  <span className={`px-2 py-0.5 text-xs rounded ${
                    user.role === 'admin'
                      ? 'bg-purple-900/30 text-purple-400'
                      : 'bg-blue-900/30 text-blue-400'
                  }`}>
                    {user.role}
                  </span>
                </td>
                <td className="px-4 py-3">
                  {user.has_subscriber ? (
                    <span className="text-xs text-green-400">{user.signalwire_address || 'Yes'}</span>
                  ) : (
                    <span className="text-xs text-gray-500">None</span>
                  )}
                </td>
                <td className="px-4 py-3 text-sm text-gray-400">
                  {user.created_at ? new Date(user.created_at).toLocaleDateString() : '\u2014'}
                </td>
                <td className="px-4 py-3 text-right">
                  <button
                    onClick={() => setPendingDelete(user)}
                    className="p-1.5 hover:bg-gray-600 rounded text-gray-500 hover:text-red-400 transition-colors"
                    title="Delete user"
                  >
                    <Trash2 className="w-4 h-4" />
                  </button>
                </td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr>
                <td colSpan={5} className="text-center text-gray-500 py-8 text-sm">
                  No users found.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>

      {pendingDelete && (
        <ConfirmModal
          title="Delete User"
          message={`Permanently delete "${pendingDelete.email}"${pendingDelete.has_subscriber ? ' and their SignalWire subscriber' : ''}? This will also delete all their calls and cannot be undone.`}
          onConfirm={async () => {
            await confirmDeleteUser(pendingDelete);
            setPendingDelete(null);
          }}
          onCancel={() => setPendingDelete(null)}
        />
      )}
    </div>
  );
}


// =============================================================================
// Shared Components
// =============================================================================

function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <Loader2 className="w-8 h-8 animate-spin text-blue-400" />
    </div>
  );
}
