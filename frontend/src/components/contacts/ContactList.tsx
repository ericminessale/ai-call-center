import { useState, useEffect } from 'react';
import { Search, Plus, Star, Building2 } from 'lucide-react';
import { ContactMinimal } from '../../types/callcenter';
import { contactsApi } from '../../services/api';
import { ContactListSkeletonGroup } from '../shared/Skeleton';

interface ContactListProps {
  contacts: ContactMinimal[];
  selectedContactId?: number;
  onSelectContact: (contact: ContactMinimal) => void;
  onSearch: (query: string) => void;
  onContactCreated?: (contact: ContactMinimal) => void;
  searchQuery: string;
  isLoading: boolean;
}

export function ContactList({
  contacts,
  selectedContactId,
  onSelectContact,
  onSearch,
  onContactCreated,
  searchQuery,
  isLoading,
}: ContactListProps) {
  const [showNewContactModal, setShowNewContactModal] = useState(false);

  const handleContactCreated = (contact: ContactMinimal) => {
    onContactCreated?.(contact);
    onSelectContact(contact);
  };

  const activeContacts = contacts.filter(c => c.activeCall);
  const recentContacts = contacts.filter(c => !c.activeCall && c.lastInteractionAt);
  const otherContacts = contacts.filter(c => !c.activeCall && !c.lastInteractionAt);

  return (
    <div className="h-full flex flex-col">
      {/* Header */}
      <div className="px-4 pt-4 pb-3 border-b border-rule">
        <div className="flex items-center justify-between mb-3">
          <span className="kicker">Contacts</span>
          <span className="mono text-[11px] text-ink-dim">{contacts.length}</span>
        </div>

        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-dim" />
          <input
            type="text"
            placeholder="Search contacts…"
            value={searchQuery}
            onChange={(e) => onSearch(e.target.value)}
            className="input pl-8 pr-8 py-[7px]"
          />
          <kbd className="absolute right-2 top-1/2 -translate-y-1/2 mono text-[9px] text-ink-dim px-1 py-0.5 rounded bg-canvas-raised border border-rule pointer-events-none">
            /
          </kbd>
        </div>

        <button
          onClick={() => setShowNewContactModal(true)}
          className="mt-2.5 w-full flex items-center justify-center gap-2 px-3 py-2 rounded bg-canvas-raised hover:bg-canvas-hover border border-rule hover:border-sw-blue/40 text-[13px] text-ink-muted hover:text-ink transition-colors group"
        >
          <Plus className="w-3.5 h-3.5 group-hover:text-sw-blue" />
          <span>New contact</span>
        </button>
      </div>

      {/* List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <ContactListSkeletonGroup />
        ) : contacts.length === 0 ? (
          <EmptyList query={searchQuery} />
        ) : (
          <>
            {activeContacts.length > 0 && (
              <Section title="On call" count={activeContacts.length} tone="live">
                {activeContacts.map(c => (
                  <ContactCard key={c.id} contact={c} isSelected={c.id === selectedContactId} onClick={() => onSelectContact(c)} />
                ))}
              </Section>
            )}
            {recentContacts.length > 0 && (
              <Section title="Recent" count={recentContacts.length}>
                {recentContacts.slice(0, 20).map(c => (
                  <ContactCard key={c.id} contact={c} isSelected={c.id === selectedContactId} onClick={() => onSelectContact(c)} />
                ))}
              </Section>
            )}
            {otherContacts.length > 0 && (
              <Section title="All contacts" count={otherContacts.length}>
                {otherContacts.map(c => (
                  <ContactCard key={c.id} contact={c} isSelected={c.id === selectedContactId} onClick={() => onSelectContact(c)} />
                ))}
              </Section>
            )}
          </>
        )}
      </div>

      {showNewContactModal && (
        <NewContactModal
          onClose={() => setShowNewContactModal(false)}
          onCreated={handleContactCreated}
        />
      )}
    </div>
  );
}

function EmptyList({ query }: { query: string }) {
  return (
    <div className="p-8 text-center">
      <p className="font-display text-[20px] text-ink-muted mb-1">
        {query ? 'Nothing matches' : 'No contacts yet'}
      </p>
      <p className="text-[12px] text-ink-dim">
        {query ? `No results for "${query}"` : 'Contacts will appear here after your first call.'}
      </p>
    </div>
  );
}

function Section({ title, count, tone = 'default', children }: {
  title: string;
  count: number;
  tone?: 'default' | 'live';
  children: React.ReactNode;
}) {
  return (
    <div>
      <div className="sticky top-0 z-10 bg-canvas-sunken/95 backdrop-blur-sm flex items-center justify-between px-4 py-1.5 border-b border-rule">
        <div className="flex items-center gap-2">
          {tone === 'live' && <span className="dot dot-live" />}
          <span className="kicker">{title}</span>
        </div>
        <span className="mono text-[10px] text-ink-dim">{count}</span>
      </div>
      <div>{children}</div>
    </div>
  );
}

function ContactCard({
  contact,
  isSelected,
  onClick,
}: {
  contact: ContactMinimal;
  isSelected: boolean;
  onClick: () => void;
}) {
  const hasActiveCall = !!contact.activeCall;
  const tierChip = contact.accountTier && contact.accountTier !== 'prospect' ? contact.accountTier : null;

  return (
    <button
      onClick={onClick}
      className={`relative w-full px-4 py-3 flex items-center gap-3 text-left border-b border-rule/60 transition-colors ${
        isSelected ? 'row-selected' : 'hover:bg-canvas-hover/40'
      }`}
    >
      {/* Avatar — flat, initials. Ring appears on live. */}
      <div className="relative shrink-0">
        <div className={`w-9 h-9 rounded flex items-center justify-center font-semibold text-[14px] tracking-tight ${
          hasActiveCall
            ? 'bg-live/15 text-live-soft border border-live/30'
            : 'bg-canvas-raised text-ink-muted border border-rule'
        }`}>
          {contact.displayName.charAt(0).toUpperCase()}
        </div>
        {hasActiveCall && (
          <span className="absolute -top-0.5 -right-0.5 w-2 h-2 rounded-full bg-live shadow-[0_0_6px_rgba(63,183,126,0.8)]" />
        )}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-1.5">
          <span className={`font-medium truncate text-[13.5px] ${
            isSelected ? 'text-ink' : 'text-ink'
          }`}>
            {contact.displayName}
          </span>
          {contact.isVip && (
            <Star className="w-3 h-3 text-wait fill-wait flex-shrink-0" />
          )}
        </div>
        <div className="flex items-center gap-1.5 text-[11.5px] text-ink-dim mt-0.5 min-w-0">
          {contact.company ? (
            <>
              <Building2 className="w-3 h-3 flex-shrink-0" />
              <span className="truncate">{contact.company}</span>
            </>
          ) : contact.phone ? (
            <span className="mono truncate">{contact.phone}</span>
          ) : null}
        </div>
      </div>

      {/* Right side: calls count + tier */}
      <div className="flex flex-col items-end gap-1 shrink-0">
        {hasActiveCall ? (
          <span className="chip chip-live">Live</span>
        ) : contact.totalCalls > 0 ? (
          <span className="mono text-[10.5px] text-ink-dim">
            {contact.totalCalls} {contact.totalCalls === 1 ? 'call' : 'calls'}
          </span>
        ) : null}
        {tierChip && (
          <span className={`chip ${
            tierChip === 'enterprise' ? 'chip-ai' :
            tierChip === 'pro' ? 'chip-info' :
            'chip-muted'
          }`}>
            {tierChip}
          </span>
        )}
      </div>
    </button>
  );
}

function NewContactModal({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated?: (contact: ContactMinimal) => void;
}) {
  const [formData, setFormData] = useState({
    firstName: '',
    lastName: '',
    displayName: '',
    phone: '',
    email: '',
    company: '',
    accountTier: 'prospect' as const,
  });
  const [isSaving, setIsSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (formData.firstName || formData.lastName) {
      const name = `${formData.firstName} ${formData.lastName}`.trim();
      if (name && !formData.displayName) {
        setFormData(prev => ({ ...prev, displayName: name }));
      }
    }
  }, [formData.firstName, formData.lastName]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    setError(null);
    if (!formData.phone) return setError('Phone number is required');
    if (!formData.displayName) return setError('Display name is required');

    setIsSaving(true);
    try {
      const response = await contactsApi.create({
        firstName: formData.firstName || undefined,
        lastName: formData.lastName || undefined,
        displayName: formData.displayName,
        phone: formData.phone,
        email: formData.email || undefined,
        company: formData.company || undefined,
        accountTier: formData.accountTier,
      });
      const newContact: ContactMinimal = {
        id: response.data.id,
        displayName: response.data.displayName,
        phone: response.data.phone,
        company: response.data.company,
        accountTier: response.data.accountTier,
        isVip: response.data.isVip,
        totalCalls: 0,
      };
      onCreated?.(newContact);
      onClose();
    } catch (err: any) {
      console.error('Failed to create contact:', err);
      setError(err.response?.data?.error || 'Failed to create contact');
    } finally {
      setIsSaving(false);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50 animate-fade-in">
      <div className="panel-raised rounded-md shadow-panel p-6 w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <div className="mb-5">
          <div className="kicker mb-1">New</div>
          <h2 className="font-display text-[26px] text-ink leading-none">Add contact</h2>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5">
          <div className="grid grid-cols-2 gap-3">
            <Field label="First name">
              <input className="input" type="text"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                placeholder="Jane" />
            </Field>
            <Field label="Last name">
              <input className="input" type="text"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                placeholder="Doe" />
            </Field>
          </div>

          <Field label="Display name" required>
            <input className="input" type="text" required
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })}
              placeholder="Jane Doe" />
          </Field>

          <Field label="Phone" required>
            <input className="input mono" type="tel" required
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
              placeholder="+1 (555) 123-4567" />
          </Field>

          <Field label="Email">
            <input className="input" type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              placeholder="jane@acme.com" />
          </Field>

          <Field label="Company">
            <input className="input" type="text"
              value={formData.company}
              onChange={(e) => setFormData({ ...formData, company: e.target.value })}
              placeholder="Acme, Inc." />
          </Field>

          <Field label="Account tier">
            <select className="input"
              value={formData.accountTier}
              onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}>
              <option value="prospect">Prospect</option>
              <option value="free">Free</option>
              <option value="pro">Pro</option>
              <option value="enterprise">Enterprise</option>
            </select>
          </Field>

          {error && (
            <div className="p-2.5 bg-urgent/10 border border-urgent/30 rounded text-urgent-soft text-[12.5px] mono">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-3 border-t border-rule">
            <button type="button" onClick={onClose} className="btn-ghost">Cancel</button>
            <button type="submit" disabled={isSaving} className="btn-primary">
              {isSaving ? 'Creating…' : 'Create contact'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

function Field({ label, required, children }: { label: string; required?: boolean; children: React.ReactNode }) {
  return (
    <label className="block">
      <span className="block kicker mb-1">
        {label}{required && <span className="text-signal-soft ml-0.5">*</span>}
      </span>
      {children}
    </label>
  );
}

export default ContactList;
