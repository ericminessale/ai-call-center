import { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  ArrowLeft,
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
} from 'lucide-react';
import { adminApi } from '../services/api';
import toast from 'react-hot-toast';
import { ConfirmModal } from '../components/shared/ConfirmModal';

interface Agent {
  id: string;
  name: string;
  route: string;
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

interface Assignment {
  id: number;
  agent_id: string;
  collection_id: number;
  collection_name: string;
  collection_display_name: string;
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

type TabId = 'routing' | 'knowledge' | 'assignments' | 'users';

export default function Admin() {
  const navigate = useNavigate();
  const [activeTab, setActiveTab] = useState<TabId>('routing');

  return (
    <div className="h-screen flex flex-col bg-gray-900 text-white">
      {/* Header */}
      <header className="bg-gray-800 border-b border-gray-700 px-6 py-4 flex items-center gap-4">
        <button
          onClick={() => navigate('/')}
          className="p-2 hover:bg-gray-700 rounded-lg transition-colors"
        >
          <ArrowLeft className="w-5 h-5 text-gray-400" />
        </button>
        <Settings className="w-5 h-5 text-blue-400" />
        <h1 className="text-lg font-semibold">Admin Settings</h1>
      </header>

      {/* Tab navigation */}
      <div className="bg-gray-800 border-b border-gray-700 px-6">
        <nav className="flex gap-1">
          <TabButton
            id="routing"
            icon={<Settings className="w-4 h-4" />}
            label="Agent Routing"
            active={activeTab === 'routing'}
            onClick={() => setActiveTab('routing')}
          />
          <TabButton
            id="knowledge"
            icon={<BookOpen className="w-4 h-4" />}
            label="Knowledge Base"
            active={activeTab === 'knowledge'}
            onClick={() => setActiveTab('knowledge')}
          />
          <TabButton
            id="assignments"
            icon={<Database className="w-4 h-4" />}
            label="Agent Documents"
            active={activeTab === 'assignments'}
            onClick={() => setActiveTab('assignments')}
          />
          <TabButton
            id="users"
            icon={<Users className="w-4 h-4" />}
            label="User Management"
            active={activeTab === 'users'}
            onClick={() => setActiveTab('users')}
          />
        </nav>
      </div>

      {/* Tab content */}
      <div className="flex-1 overflow-y-auto p-6">
        {activeTab === 'routing' && <AgentRoutingTab />}
        {activeTab === 'knowledge' && <KnowledgeBaseTab />}
        {activeTab === 'assignments' && <AgentAssignmentsTab />}
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
// Tab 1: Agent Routing
// =============================================================================

function AgentRoutingTab() {
  const [config, setConfig] = useState<Record<string, string>>({});
  const [agents, setAgents] = useState<Agent[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const loadConfig = useCallback(async () => {
    try {
      const resp = await adminApi.getAgentConfig();
      setConfig(resp.data.config);
      setAgents(resp.data.available_agents);
    } catch (err) {
      toast.error('Failed to load agent config');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadConfig(); }, [loadConfig]);

  const handleSave = async () => {
    setSaving(true);
    try {
      await adminApi.updateAgentConfig(config);
      toast.success('Agent routing saved');
    } catch (err) {
      toast.error('Failed to save config');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  const slots = [
    { key: 'initial_handler', label: 'Initial Call Handler', desc: 'Which agent answers incoming calls first' },
    { key: 'sales_specialist', label: 'Sales Specialist', desc: 'Agent for sales inquiries and transfers' },
    { key: 'support_specialist', label: 'Support Specialist', desc: 'Agent for support issues and transfers' },
  ];

  return (
    <div className="max-w-2xl">
      <div className="mb-6">
        <h2 className="text-xl font-semibold mb-1">Agent Routing Configuration</h2>
        <p className="text-sm text-gray-400">Configure which AI agent handles each routing slot.</p>
      </div>

      <div className="space-y-6">
        {slots.map(slot => (
          <div key={slot.key} className="bg-gray-800 rounded-lg p-4 border border-gray-700">
            <label className="block text-sm font-medium text-gray-300 mb-1">{slot.label}</label>
            <p className="text-xs text-gray-500 mb-3">{slot.desc}</p>
            <select
              value={config[slot.key] || ''}
              onChange={e => setConfig({ ...config, [slot.key]: e.target.value })}
              className="w-full bg-gray-700 border border-gray-600 rounded-lg px-3 py-2 text-white text-sm focus:outline-none focus:border-blue-500"
            >
              {agents.map(agent => (
                <option key={agent.id} value={agent.route}>
                  {agent.name} ({agent.route})
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>

      <div className="mt-6">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Save Configuration
        </button>
      </div>
    </div>
  );
}


// =============================================================================
// Tab 2: Knowledge Base
// =============================================================================

function KnowledgeBaseTab() {
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
                  <div className="text-xs text-gray-500 mt-0.5">{coll.name}</div>
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
                <div className="text-xs text-gray-500 mt-1">{coll.description}</div>
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
                <p className="text-xs text-gray-500">{selectedCollection.description}</p>
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
                <h3 className="text-sm font-medium mb-3">New Document</h3>
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
                <h3 className="text-sm font-medium mb-3">Edit Document</h3>
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
// Tab 3: Agent Document Assignments
// =============================================================================

function AgentAssignmentsTab() {
  const [agents, setAgents] = useState<Agent[]>([]);
  const [collections, setCollections] = useState<Collection[]>([]);
  const [assignments, setAssignments] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);

  const loadData = useCallback(async () => {
    try {
      const [configResp, collectionsResp, assignmentsResp] = await Promise.all([
        adminApi.getAgentConfig(),
        adminApi.getCollections(),
        adminApi.getAgentAssignments(),
      ]);

      setAgents(configResp.data.available_agents);
      setCollections(collectionsResp.data.collections);

      // Build assignment map: agent_id -> collection_id
      const map: Record<string, number> = {};
      for (const a of assignmentsResp.data.assignments) {
        map[a.agent_id] = a.collection_id;
      }
      setAssignments(map);
    } catch (err) {
      toast.error('Failed to load data');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { loadData(); }, [loadData]);

  const handleSave = async () => {
    setSaving(true);
    try {
      const assignmentList = Object.entries(assignments)
        .filter(([, collId]) => collId > 0)
        .map(([agentId, collId]) => ({ agent_id: agentId, collection_id: collId }));

      await adminApi.updateAgentAssignments({ assignments: assignmentList });
      toast.success('Assignments saved. Restart agents for changes to take effect.');
    } catch (err) {
      toast.error('Failed to save assignments');
    } finally {
      setSaving(false);
    }
  };

  if (loading) return <LoadingSpinner />;

  return (
    <div className="max-w-3xl">
      <div className="mb-6">
        <h2 className="text-xl font-semibold mb-1">Agent Document Assignments</h2>
        <p className="text-sm text-gray-400">Choose which knowledge base collection each agent searches.</p>
      </div>

      <div className="bg-yellow-900/20 border border-yellow-800/50 rounded-lg p-3 mb-6 flex items-start gap-2">
        <Info className="w-4 h-4 text-yellow-500 mt-0.5 flex-shrink-0" />
        <p className="text-sm text-yellow-300/80">
          Changing assignments requires an agent container restart to take effect.
          Document content changes (via "Publish Changes") take effect immediately.
        </p>
      </div>

      <div className="bg-gray-800 rounded-lg border border-gray-700 overflow-hidden">
        <table className="w-full">
          <thead>
            <tr className="border-b border-gray-700">
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Agent</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Assigned Collection</th>
              <th className="text-left px-4 py-3 text-xs font-medium text-gray-400 uppercase tracking-wider">Description</th>
            </tr>
          </thead>
          <tbody>
            {agents.map(agent => (
              <tr key={agent.id} className="border-b border-gray-700/50 hover:bg-gray-700/30">
                <td className="px-4 py-3">
                  <div className="text-sm font-medium">{agent.name}</div>
                  <div className="text-xs text-gray-500">{agent.route}</div>
                </td>
                <td className="px-4 py-3">
                  <select
                    value={assignments[agent.id] || 0}
                    onChange={e => setAssignments({ ...assignments, [agent.id]: parseInt(e.target.value) })}
                    className="bg-gray-700 border border-gray-600 rounded-lg px-3 py-1.5 text-sm text-white focus:outline-none focus:border-blue-500"
                  >
                    <option value={0}>None</option>
                    {collections.map(coll => (
                      <option key={coll.id} value={coll.id}>{coll.display_name}</option>
                    ))}
                  </select>
                </td>
                <td className="px-4 py-3 text-sm text-gray-400">{agent.description}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="mt-6">
        <button
          onClick={handleSave}
          disabled={saving}
          className="flex items-center gap-2 px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:opacity-50 rounded-lg text-sm font-medium transition-colors"
        >
          {saving ? <Loader2 className="w-4 h-4 animate-spin" /> : <Save className="w-4 h-4" />}
          Save Assignments
        </button>
      </div>
    </div>
  );
}


// =============================================================================
// Tab 4: User Management
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
                  {user.name && <div className="text-xs text-gray-500">{user.name}</div>}
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


function LoadingSpinner() {
  return (
    <div className="flex items-center justify-center py-12">
      <Loader2 className="w-8 h-8 animate-spin text-blue-400" />
    </div>
  );
}
