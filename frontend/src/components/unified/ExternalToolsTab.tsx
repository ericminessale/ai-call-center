import { useCallback, useEffect, useMemo, useState } from 'react';
import {
  Plug,
  Plus,
  Pencil,
  Trash2,
  Loader2,
  PlugZap,
  CheckCircle2,
  XCircle,
  Info,
} from 'lucide-react';
import toast from 'react-hot-toast';
import {
  adminApi,
  McpGateway,
  McpGatewayAuthType,
  McpGatewayInput,
  McpGatewayService,
} from '../../services/api';
import { ConfirmModal } from '../shared/ConfirmModal';
import { logger } from '../../lib/logger';

// =============================================================================
// External Tools Tab — admin UI for MCP Gateway integrations.
//
// Each row is one configured connection to an MCP Gateway service. The
// gateway in turn fronts one or more MCP servers, and the agents that
// are bound to a gateway pick up its tools as SWAIG functions at boot.
// "Test" probes the gateway's /services endpoint and lists what it
// exposes so the admin can confirm the connection is live before
// committing to the binding.
// =============================================================================

interface AgentDescriptor {
  id: string;
  name: string;
  route: string;
}

const EMPTY_FORM: McpGatewayInput = {
  name: '',
  description: '',
  gateway_url: '',
  auth_type: 'basic',
  auth_user: '',
  auth_password: '',
  auth_token: '',
  bound_agent_ids: [],
  enabled: true,
};

export function ExternalToolsTab() {
  const [gateways, setGateways] = useState<McpGateway[]>([]);
  const [agents, setAgents] = useState<AgentDescriptor[]>([]);
  const [loading, setLoading] = useState(true);

  const [editing, setEditing] = useState<McpGateway | null>(null);
  const [creating, setCreating] = useState(false);
  const [deleteCandidate, setDeleteCandidate] = useState<McpGateway | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [gwResp, agentsResp] = await Promise.all([
        adminApi.listMcpGateways(),
        adminApi.getAgentConfig(),
      ]);
      setGateways(gwResp.data.gateways || []);
      setAgents(agentsResp.data.available_agents || []);
    } catch (err) {
      logger.error('Failed to load MCP gateways', err);
      toast.error('Could not load external tool integrations');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleDelete = async () => {
    if (!deleteCandidate) return;
    try {
      await adminApi.deleteMcpGateway(deleteCandidate.id);
      toast.success(`Deleted ${deleteCandidate.name}`);
      setDeleteCandidate(null);
      load();
    } catch (err) {
      logger.error('delete MCP gateway failed', err);
      toast.error('Delete failed');
    }
  };

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div>
          <h2 className="font-display text-xl text-ink leading-tight">External Tools</h2>
          <p className="text-[13px] text-ink-muted mt-1 max-w-2xl">
            Bridge customer-owned MCP (Model Context Protocol) servers into agents through an MCP
            Gateway. Each gateway you configure here gets attached to the agents you select; the
            tools it exposes show up as SWAIG functions the AI can call mid-conversation. No code
            changes, no rebuild — paste a gateway URL, pick which agents should see it, done.
          </p>
        </div>
        <button
          type="button"
          onClick={() => setCreating(true)}
          className="inline-flex items-center gap-1.5 px-3 py-2 rounded-md bg-ai text-canvas text-[13px] font-medium hover:bg-ai-hover transition-colors"
        >
          <Plus className="w-4 h-4" />
          Add gateway
        </button>
      </header>

      {loading ? (
        <div className="flex items-center justify-center py-16 text-ink-muted">
          <Loader2 className="w-5 h-5 animate-spin mr-2" />
          Loading gateways…
        </div>
      ) : gateways.length === 0 ? (
        <EmptyState onAdd={() => setCreating(true)} />
      ) : (
        <ul className="space-y-3">
          {gateways.map((gw) => (
            <GatewayCard
              key={gw.id}
              gateway={gw}
              agents={agents}
              onEdit={() => setEditing(gw)}
              onDelete={() => setDeleteCandidate(gw)}
            />
          ))}
        </ul>
      )}

      {(creating || editing) && (
        <GatewayEditorModal
          initial={editing ?? null}
          agents={agents}
          onClose={() => {
            setCreating(false);
            setEditing(null);
          }}
          onSaved={() => {
            setCreating(false);
            setEditing(null);
            load();
          }}
        />
      )}

      {deleteCandidate && (
        <ConfirmModal
          onCancel={() => setDeleteCandidate(null)}
          onConfirm={handleDelete}
          title="Delete MCP gateway?"
          message={`"${deleteCandidate.name}" will be unbound from all agents at the next agent restart.`}
          confirmLabel="Delete"
          variant="danger"
        />
      )}
    </div>
  );
}

// =============================================================================
// Empty state
// =============================================================================

function EmptyState({ onAdd }: { onAdd: () => void }) {
  return (
    <div className="rounded-lg border border-dashed border-rule bg-canvas-raised p-10 text-center">
      <Plug className="w-8 h-8 mx-auto text-ink-muted mb-3" />
      <h3 className="text-ink text-[15px] font-medium">No external tool integrations yet</h3>
      <p className="text-[13px] text-ink-muted mt-2 max-w-md mx-auto">
        Connect agents to your own MCP servers (Salesforce, Zendesk, internal systems, anything
        you've built) via an MCP Gateway. Paste the gateway URL, pick which agents should load the
        tools, and you're live on the next agent restart.
      </p>
      <button
        type="button"
        onClick={onAdd}
        className="inline-flex items-center gap-1.5 mt-4 px-3 py-2 rounded-md bg-ai text-canvas text-[13px] font-medium hover:bg-ai-hover transition-colors"
      >
        <Plus className="w-4 h-4" />
        Add your first gateway
      </button>
    </div>
  );
}

// =============================================================================
// Gateway card (list row)
// =============================================================================

function GatewayCard({
  gateway,
  agents,
  onEdit,
  onDelete,
}: {
  gateway: McpGateway;
  agents: AgentDescriptor[];
  onEdit: () => void;
  onDelete: () => void;
}) {
  const [testing, setTesting] = useState(false);
  const [testResult, setTestResult] = useState<
    { ok: true; services: McpGatewayService[] } | { ok: false; error: string } | null
  >(null);

  const boundLabels = useMemo(() => {
    const map = new Map(agents.map((a) => [a.id, a.name]));
    return (gateway.bound_agent_ids || []).map((id) => map.get(id) || id);
  }, [agents, gateway.bound_agent_ids]);

  const runTest = async () => {
    setTesting(true);
    setTestResult(null);
    try {
      const resp = await adminApi.testMcpGateway(gateway.id);
      const data = resp.data;
      if (data.ok) {
        setTestResult({ ok: true, services: data.services || [] });
      } else {
        setTestResult({ ok: false, error: data.error || 'Test failed' });
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : 'Test failed';
      setTestResult({ ok: false, error: msg });
    } finally {
      setTesting(false);
    }
  };

  return (
    <li className="rounded-lg border border-rule bg-canvas-raised p-4">
      <div className="flex items-start justify-between gap-4">
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2">
            <h3 className="font-medium text-ink text-[15px] truncate">{gateway.name}</h3>
            {!gateway.enabled && (
              <span className="text-[11px] uppercase tracking-wide bg-canvas-sunken border border-rule rounded px-1.5 py-0.5 text-ink-muted">
                Disabled
              </span>
            )}
          </div>
          {gateway.description && (
            <p className="text-[13px] text-ink-muted mt-0.5">{gateway.description}</p>
          )}
          <div className="text-[12px] text-ink-muted mt-1.5 truncate font-mono">
            {gateway.gateway_url}
          </div>
          <div className="flex flex-wrap items-center gap-1.5 mt-2.5">
            <span className="text-[11px] uppercase tracking-wide text-ink-muted">Agents:</span>
            {boundLabels.length === 0 ? (
              <span className="text-[12px] text-ink-muted italic">none — not loaded by any agent</span>
            ) : (
              boundLabels.map((label) => (
                <span
                  key={label}
                  className="text-[11px] bg-ai/15 text-ai-soft border border-ai/30 rounded px-1.5 py-0.5"
                >
                  {label}
                </span>
              ))
            )}
          </div>
          <div className="text-[11px] uppercase tracking-wide text-ink-muted mt-2">
            Auth: {gateway.auth_type === 'none' ? 'none' : gateway.auth_type}
          </div>
        </div>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={runTest}
            disabled={testing}
            className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1.5 rounded border border-rule bg-canvas-sunken hover:bg-canvas text-ink transition-colors disabled:opacity-50"
            title="Probe the gateway and list its services"
          >
            {testing ? (
              <Loader2 className="w-3.5 h-3.5 animate-spin" />
            ) : (
              <PlugZap className="w-3.5 h-3.5" />
            )}
            Test
          </button>
          <button
            type="button"
            onClick={onEdit}
            className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1.5 rounded border border-rule bg-canvas-sunken hover:bg-canvas text-ink transition-colors"
          >
            <Pencil className="w-3.5 h-3.5" />
            Edit
          </button>
          <button
            type="button"
            onClick={onDelete}
            className="inline-flex items-center gap-1 text-[12px] px-2.5 py-1.5 rounded border border-danger/40 text-danger hover:bg-danger/10 transition-colors"
          >
            <Trash2 className="w-3.5 h-3.5" />
            Delete
          </button>
        </div>
      </div>

      {testResult && (
        <div className="mt-3 pt-3 border-t border-rule">
          {testResult.ok ? (
            <div>
              <div className="flex items-center gap-1.5 text-[12px] text-success mb-2">
                <CheckCircle2 className="w-3.5 h-3.5" />
                Connected — {testResult.services.length} service
                {testResult.services.length === 1 ? '' : 's'} available
              </div>
              {testResult.services.length > 0 && (
                <ul className="space-y-1.5">
                  {testResult.services.map((svc) => (
                    <li
                      key={svc.name}
                      className="text-[12px] bg-canvas-sunken border border-rule rounded px-2 py-1.5"
                    >
                      <div className="flex items-center gap-2">
                        <span className="font-mono text-ink">{svc.name}</span>
                        {svc.description && (
                          <span className="text-ink-muted">— {svc.description}</span>
                        )}
                      </div>
                      {svc.tools.length > 0 && (
                        <div className="flex flex-wrap gap-1 mt-1">
                          {svc.tools.map((tool) => (
                            <span
                              key={tool}
                              className="text-[10px] font-mono bg-canvas border border-rule rounded px-1 py-0.5 text-ink-muted"
                            >
                              {tool}
                            </span>
                          ))}
                        </div>
                      )}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ) : (
            <div className="flex items-start gap-1.5 text-[12px] text-danger">
              <XCircle className="w-3.5 h-3.5 mt-0.5 shrink-0" />
              <span>{testResult.error}</span>
            </div>
          )}
        </div>
      )}
    </li>
  );
}

// =============================================================================
// Editor modal — create + edit
// =============================================================================

function GatewayEditorModal({
  initial,
  agents,
  onClose,
  onSaved,
}: {
  initial: McpGateway | null;
  agents: AgentDescriptor[];
  onClose: () => void;
  onSaved: () => void;
}) {
  const isEdit = !!initial;
  const [form, setForm] = useState<McpGatewayInput>(() => {
    if (!initial) return { ...EMPTY_FORM };
    return {
      name: initial.name,
      description: initial.description ?? '',
      gateway_url: initial.gateway_url,
      auth_type: initial.auth_type,
      auth_user: initial.auth_user ?? '',
      auth_password: '',
      auth_token: '',
      services_filter: initial.services_filter ?? [],
      bound_agent_ids: initial.bound_agent_ids ?? [],
      enabled: initial.enabled,
    };
  });
  const [saving, setSaving] = useState(false);

  const setField = <K extends keyof McpGatewayInput>(key: K, value: McpGatewayInput[K]) => {
    setForm((f) => ({ ...f, [key]: value }));
  };

  const toggleAgent = (id: string) => {
    setForm((f) => {
      const set = new Set(f.bound_agent_ids);
      if (set.has(id)) set.delete(id);
      else set.add(id);
      return { ...f, bound_agent_ids: Array.from(set) };
    });
  };

  const submit = async () => {
    if (!form.name.trim()) {
      toast.error('Name is required');
      return;
    }
    if (!form.gateway_url.trim()) {
      toast.error('Gateway URL is required');
      return;
    }
    setSaving(true);
    try {
      // Strip empty credential fields so the backend doesn't blank stored
      // passwords on edits where the user didn't retype them.
      const payload: McpGatewayInput = { ...form };
      if (!payload.auth_password) delete payload.auth_password;
      if (!payload.auth_token) delete payload.auth_token;
      if (form.auth_type === 'none') {
        delete payload.auth_user;
        delete payload.auth_password;
        delete payload.auth_token;
      } else if (form.auth_type === 'basic') {
        delete payload.auth_token;
      } else if (form.auth_type === 'bearer') {
        delete payload.auth_user;
        delete payload.auth_password;
      }

      if (isEdit && initial) {
        await adminApi.updateMcpGateway(initial.id, payload);
        toast.success(`Updated ${form.name}`);
      } else {
        await adminApi.createMcpGateway(payload);
        toast.success(`Added ${form.name}`);
      }
      onSaved();
    } catch (err: unknown) {
      logger.error('save MCP gateway failed', err);
      const msg =
        err && typeof err === 'object' && 'response' in err
          ? // axios-style error
            ((err as { response?: { data?: { error?: string } } }).response?.data?.error ?? 'Save failed')
          : 'Save failed';
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="bg-canvas-raised border border-rule rounded-lg shadow-xl w-full max-w-2xl max-h-[90vh] overflow-y-auto">
        <header className="px-6 py-4 border-b border-rule">
          <h3 className="font-display text-lg text-ink leading-none">
            {isEdit ? 'Edit MCP gateway' : 'Add MCP gateway'}
          </h3>
          <p className="text-[12px] text-ink-muted mt-1">
            Configure a connection to an MCP Gateway service. The gateway fronts one or more MCP
            servers; the tools it exposes become SWAIG functions on the bound agents.
          </p>
        </header>

        <div className="px-6 py-5 space-y-5">
          <Field label="Name" required>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setField('name', e.target.value)}
              placeholder="e.g. Salesforce CRM"
              className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink"
            />
          </Field>

          <Field label="Description">
            <input
              type="text"
              value={form.description ?? ''}
              onChange={(e) => setField('description', e.target.value)}
              placeholder="Optional — what tools this exposes"
              className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink"
            />
          </Field>

          <Field label="Gateway URL" required>
            <input
              type="text"
              value={form.gateway_url}
              onChange={(e) => setField('gateway_url', e.target.value)}
              placeholder="https://gateway.example.com"
              className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink font-mono"
            />
            <p className="text-[11px] text-ink-muted mt-1">
              Not the URL of an MCP server itself — the URL of an MCP Gateway service that fronts
              your MCP server(s).
            </p>
          </Field>

          <Field label="Authentication">
            <div className="flex gap-2">
              {(['basic', 'bearer', 'none'] as McpGatewayAuthType[]).map((type) => (
                <button
                  key={type}
                  type="button"
                  onClick={() => setField('auth_type', type)}
                  className={`px-3 py-1.5 rounded text-[12px] border transition-colors ${
                    form.auth_type === type
                      ? 'bg-ai text-canvas border-ai'
                      : 'bg-canvas border-rule text-ink hover:bg-canvas-sunken'
                  }`}
                >
                  {type === 'basic' ? 'Basic' : type === 'bearer' ? 'Bearer token' : 'None'}
                </button>
              ))}
            </div>
          </Field>

          {form.auth_type === 'basic' && (
            <div className="grid grid-cols-2 gap-3">
              <Field label="Username">
                <input
                  type="text"
                  value={form.auth_user ?? ''}
                  onChange={(e) => setField('auth_user', e.target.value)}
                  className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink"
                />
              </Field>
              <Field
                label="Password"
                hint={isEdit ? 'Leave blank to keep current' : undefined}
              >
                <input
                  type="password"
                  value={form.auth_password ?? ''}
                  onChange={(e) => setField('auth_password', e.target.value)}
                  className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink"
                />
              </Field>
            </div>
          )}

          {form.auth_type === 'bearer' && (
            <Field
              label="Bearer token"
              hint={isEdit ? 'Leave blank to keep current' : undefined}
            >
              <input
                type="password"
                value={form.auth_token ?? ''}
                onChange={(e) => setField('auth_token', e.target.value)}
                className="w-full px-3 py-2 bg-canvas border border-rule rounded text-[13px] text-ink font-mono"
              />
            </Field>
          )}

          <Field
            label="Bound agents"
            hint="Agents that should load this gateway at boot."
          >
            <div className="space-y-1.5">
              {agents.length === 0 ? (
                <div className="flex items-center gap-2 text-[12px] text-ink-muted">
                  <Info className="w-3.5 h-3.5" />
                  No agents available — check the AI Agents tab first.
                </div>
              ) : (
                agents.map((a) => (
                  <label
                    key={a.id}
                    className="flex items-center gap-2 text-[13px] text-ink cursor-pointer hover:bg-canvas-sunken rounded px-2 py-1.5"
                  >
                    <input
                      type="checkbox"
                      checked={form.bound_agent_ids.includes(a.id)}
                      onChange={() => toggleAgent(a.id)}
                      className="rounded border-rule"
                    />
                    <span className="font-medium">{a.name}</span>
                    <span className="font-mono text-[11px] text-ink-muted">{a.route}</span>
                  </label>
                ))
              )}
            </div>
          </Field>

          <Field label="Status">
            <label className="flex items-center gap-2 text-[13px] text-ink cursor-pointer">
              <input
                type="checkbox"
                checked={form.enabled ?? true}
                onChange={(e) => setField('enabled', e.target.checked)}
                className="rounded border-rule"
              />
              Enabled (uncheck to disable without deleting)
            </label>
          </Field>
        </div>

        <footer className="px-6 py-4 border-t border-rule flex items-center justify-end gap-2">
          <button
            type="button"
            onClick={onClose}
            disabled={saving}
            className="px-3 py-2 rounded text-[13px] border border-rule text-ink hover:bg-canvas-sunken transition-colors disabled:opacity-50"
          >
            Cancel
          </button>
          <button
            type="button"
            onClick={submit}
            disabled={saving}
            className="px-3 py-2 rounded text-[13px] bg-ai text-canvas font-medium hover:bg-ai-hover transition-colors disabled:opacity-50"
          >
            {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : isEdit ? 'Save changes' : 'Add gateway'}
          </button>
        </footer>
      </div>
    </div>
  );
}

// =============================================================================
// Form field wrapper
// =============================================================================

function Field({
  label,
  required,
  hint,
  children,
}: {
  label: string;
  required?: boolean;
  hint?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label className="block text-[12px] uppercase tracking-wide text-ink-muted mb-1.5">
        {label}
        {required && <span className="text-danger ml-0.5">*</span>}
      </label>
      {children}
      {hint && <p className="text-[11px] text-ink-muted mt-1">{hint}</p>}
    </div>
  );
}
