import { useState, useEffect } from 'react';
import { Search, Plus, Star } from 'lucide-react';
import { ContactMinimal } from '../../types/callcenter';
import { contactsApi } from '../../services/api';
import { ContactListSkeletonGroup } from '../shared/Skeleton';
import { logger } from '../../lib/logger';
import { Button, Chip, StatusDot, RailContactRow } from '../restraint';

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
          <span className="text-[11px] font-medium text-ink-dim">Contacts</span>
          <span className="mono text-[11px] text-ink-dim">{contacts.length}</span>
        </div>

        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 -translate-y-1/2 w-3.5 h-3.5 text-ink-dim z-10" />
          <input
            type="text"
            placeholder="Search contacts…"
            value={searchQuery}
            onChange={(e) => onSearch(e.target.value)}
            className="w-full pl-8 pr-8 py-2 rounded-lg border border-rule-strong bg-canvas text-ink text-sm font-medium placeholder:text-ink-dim focus:outline-none focus:border-sw-fuchsia"
          />
          <kbd className="absolute right-2 top-1/2 -translate-y-1/2 mono text-[9px] text-ink-dim px-1 py-0.5 rounded-sm bg-canvas-raised border border-rule-strong pointer-events-none">
            /
          </kbd>
        </div>

        <Button
          variant="secondary"
          icon={<Plus className="w-3.5 h-3.5" />}
          onClick={() => setShowNewContactModal(true)}
          className="mt-2.5 w-full justify-center"
        >
          New contact
        </Button>
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
    <div className="pb-1">
      {/* Lightweight railhead — label + count, no full-width boxed border. */}
      <div className="sticky top-0 z-10 bg-canvas-raised/95 backdrop-blur-sm flex items-center justify-between px-3 pt-2.5 pb-1.5">
        <div className="flex items-center gap-2">
          {tone === 'live' && <StatusDot status="success" />}
          <span className="text-[11px] font-medium text-ink-dim">{title}</span>
        </div>
        <span className="mono text-[10px] text-ink-dim">{count}</span>
      </div>
      {/* Rows float: inset, rounded, small gaps (rs-clist). */}
      <div className="px-2 flex flex-col gap-0.5">{children}</div>
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

  // Floating rs-crow: avatar + name(+VIP) + phone, with a right-side marker.
  // Live > call count > tier. Tier-when-active info still lives in the detail KPI.
  const trailing = hasActiveCall ? (
    <Chip dot="success">Live</Chip>
  ) : contact.totalCalls > 0 ? (
    <span className="mono text-[10.5px] text-ink-dim">{contact.totalCalls}</span>
  ) : tierChip ? (
    <Chip className="capitalize">{tierChip}</Chip>
  ) : null;

  return (
    <RailContactRow
      name={contact.displayName}
      phone={contact.phone}
      avatar={contact.displayName.charAt(0).toUpperCase()}
      selected={isSelected}
      onClick={onClick}
      badge={contact.isVip ? <Star className="w-3 h-3 text-status-warning fill-status-warning" /> : undefined}
      trailing={trailing}
    />
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
    // displayName is deliberately sampled: once a user edits it, name changes
    // must not overwrite their explicit value.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
      logger.error('Failed to create contact:', err);
      setError(err.response?.data?.error || 'Failed to create contact');
    } finally {
      setIsSaving(false);
    }
  };

  const fieldClass = 'w-full px-3 py-2 rounded-lg border border-rule-strong bg-canvas text-ink text-sm font-medium placeholder:text-ink-dim focus:outline-none focus:border-sw-fuchsia';

  return (
    <div className="fixed inset-0 bg-black/70 backdrop-blur-sm flex items-center justify-center z-50 animate-fade-in">
      <div className="bg-canvas-raised border border-rule rounded-lg shadow-panel p-6 w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <div className="mb-5">
          <div className="kicker mb-1">New</div>
          <h2 className="font-display text-[26px] text-ink leading-none">Add contact</h2>
        </div>

        <form onSubmit={handleSubmit} className="space-y-3.5">
          <div className="grid grid-cols-2 gap-3">
            <Field label="First name">
              <input className={fieldClass} type="text"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                placeholder="Jane" />
            </Field>
            <Field label="Last name">
              <input className={fieldClass} type="text"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                placeholder="Doe" />
            </Field>
          </div>

          <Field label="Display name" required>
            <input className={fieldClass} type="text" required
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })}
              placeholder="Jane Doe" />
          </Field>

          <Field label="Phone" required>
            <input className={`${fieldClass} mono`} type="tel" required
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
              placeholder="+1 (555) 123-4567" />
          </Field>

          <Field label="Email">
            <input className={fieldClass} type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              placeholder="jane@acme.com" />
          </Field>

          <Field label="Company">
            <input className={fieldClass} type="text"
              value={formData.company}
              onChange={(e) => setFormData({ ...formData, company: e.target.value })}
              placeholder="Acme, Inc." />
          </Field>

          <Field label="Account tier">
            <select className={fieldClass}
              value={formData.accountTier}
              onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}>
              <option value="prospect">Prospect</option>
              <option value="free">Free</option>
              <option value="pro">Pro</option>
              <option value="enterprise">Enterprise</option>
            </select>
          </Field>

          {error && (
            <div className="relative overflow-hidden p-2.5 pl-3.5 bg-canvas-raised border border-rule rounded-lg text-status-error text-[12.5px] mono">
              <span className="absolute left-0 top-0 bottom-0 w-0.5 bg-status-error" />
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-3 border-t border-rule">
            <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
            <Button type="submit" variant="primary" disabled={isSaving}>
              {isSaving ? 'Creating…' : 'Create contact'}
            </Button>
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
        {label}{required && <span className="text-sw-fuchsia ml-0.5">*</span>}
      </span>
      {children}
    </label>
  );
}

export default ContactList;
