import { useState, useEffect } from 'react';
import { Search, Plus, Phone, Star, Building2 } from 'lucide-react';
import { ContactMinimal } from '../../types/callcenter';
import { contactsApi } from '../../services/api';

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
    onSelectContact(contact); // Auto-select the new contact
  };

  // Separate contacts with active calls
  const activeContacts = contacts.filter(c => c.activeCall);
  const recentContacts = contacts.filter(c => !c.activeCall && c.lastInteractionAt);
  const otherContacts = contacts.filter(c => !c.activeCall && !c.lastInteractionAt);

  return (
    <div className="h-full flex flex-col">
      {/* Search Header */}
      <div className="p-3 border-b border-gray-700">
        <div className="relative">
          <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-gray-400" />
          <input
            type="text"
            placeholder="Search contacts..."
            value={searchQuery}
            onChange={(e) => onSearch(e.target.value)}
            className="w-full pl-9 pr-4 py-2 bg-gray-700 border border-gray-600 rounded-lg text-sm text-white placeholder-gray-400 focus:outline-none focus:border-blue-500"
          />
        </div>
        <button
          onClick={() => setShowNewContactModal(true)}
          className="mt-2 w-full flex items-center justify-center gap-2 px-3 py-2 bg-blue-600 hover:bg-blue-700 text-white text-sm rounded-lg transition-colors"
        >
          <Plus className="w-4 h-4" />
          New Contact
        </button>
      </div>

      {/* Contact List */}
      <div className="flex-1 overflow-y-auto">
        {isLoading ? (
          <div className="p-4 text-center text-gray-400">
            <div className="animate-spin w-6 h-6 border-2 border-gray-400 border-t-transparent rounded-full mx-auto mb-2" />
            Loading contacts...
          </div>
        ) : contacts.length === 0 ? (
          <div className="p-4 text-center text-gray-400">
            {searchQuery ? 'No contacts found' : 'No contacts yet'}
          </div>
        ) : (
          <>
            {/* Active Calls Section */}
            {activeContacts.length > 0 && (
              <div className="mb-2">
                <div className="px-3 py-2 text-xs font-semibold text-green-400 uppercase tracking-wider bg-gray-800/50">
                  Active Calls ({activeContacts.length})
                </div>
                {activeContacts.map(contact => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    isSelected={contact.id === selectedContactId}
                    onClick={() => onSelectContact(contact)}
                  />
                ))}
              </div>
            )}

            {/* Recent Contacts Section */}
            {recentContacts.length > 0 && (
              <div className="mb-2">
                <div className="px-3 py-2 text-xs font-semibold text-gray-400 uppercase tracking-wider bg-gray-800/50">
                  Recent
                </div>
                {recentContacts.slice(0, 10).map(contact => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    isSelected={contact.id === selectedContactId}
                    onClick={() => onSelectContact(contact)}
                  />
                ))}
              </div>
            )}

            {/* All Contacts Section */}
            {otherContacts.length > 0 && (
              <div>
                <div className="px-3 py-2 text-xs font-semibold text-gray-400 uppercase tracking-wider bg-gray-800/50">
                  All Contacts
                </div>
                {otherContacts.map(contact => (
                  <ContactCard
                    key={contact.id}
                    contact={contact}
                    isSelected={contact.id === selectedContactId}
                    onClick={() => onSelectContact(contact)}
                  />
                ))}
              </div>
            )}
          </>
        )}
      </div>

      {/* New Contact Modal */}
      {showNewContactModal && (
        <NewContactModal
          onClose={() => setShowNewContactModal(false)}
          onCreated={handleContactCreated}
        />
      )}
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

  return (
    <button
      onClick={onClick}
      className={`w-full px-3 py-3 flex items-center gap-3 text-left transition-colors ${
        isSelected
          ? 'bg-blue-600/20 border-l-2 border-blue-500'
          : 'hover:bg-gray-700/50 border-l-2 border-transparent'
      }`}
    >
      {/* Avatar */}
      <div className={`w-10 h-10 rounded-full flex items-center justify-center text-white font-medium ${
        hasActiveCall ? 'bg-green-600' : 'bg-gray-600'
      }`}>
        {contact.displayName.charAt(0).toUpperCase()}
      </div>

      {/* Info */}
      <div className="flex-1 min-w-0">
        <div className="flex items-center gap-2">
          <span className="font-medium text-white truncate">
            {contact.displayName}
          </span>
          {contact.isVip && (
            <Star className="w-3 h-3 text-yellow-400 fill-yellow-400 flex-shrink-0" />
          )}
        </div>
        <div className="flex items-center gap-2 text-xs text-gray-400">
          {contact.company && (
            <>
              <Building2 className="w-3 h-3" />
              <span className="truncate">{contact.company}</span>
            </>
          )}
          {!contact.company && contact.phone && (
            <span>{contact.phone}</span>
          )}
        </div>
      </div>

      {/* Status indicators */}
      <div className="flex flex-col items-end gap-1">
        {hasActiveCall && (
          <div className="flex items-center gap-1 text-xs text-green-400">
            <Phone className="w-3 h-3 animate-pulse" />
            <span>Active</span>
          </div>
        )}
        {contact.totalCalls > 0 && !hasActiveCall && (
          <span className="text-xs text-gray-500">
            {contact.totalCalls} call{contact.totalCalls !== 1 ? 's' : ''}
          </span>
        )}
        {contact.accountTier !== 'prospect' && (
          <span className={`text-xs px-1.5 py-0.5 rounded ${
            contact.accountTier === 'enterprise' ? 'bg-purple-500/20 text-purple-400' :
            contact.accountTier === 'pro' ? 'bg-blue-500/20 text-blue-400' :
            'bg-gray-500/20 text-gray-400'
          }`}>
            {contact.accountTier}
          </span>
        )}
      </div>
    </button>
  );
}

function NewContactModal({
  onClose,
  onCreated
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

  // Auto-generate display name from first/last name
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

    if (!formData.phone) {
      setError('Phone number is required');
      return;
    }

    if (!formData.displayName) {
      setError('Display name is required');
      return;
    }

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

      // Convert to ContactMinimal for the list
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
    <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
      <div className="bg-gray-800 rounded-lg p-6 w-full max-w-md mx-4 max-h-[90vh] overflow-y-auto">
        <h2 className="text-lg font-semibold text-white mb-4">New Contact</h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          <div className="grid grid-cols-2 gap-3">
            <div>
              <label className="block text-sm text-gray-400 mb-1">First Name</label>
              <input
                type="text"
                value={formData.firstName}
                onChange={(e) => setFormData({ ...formData, firstName: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
                placeholder="John"
              />
            </div>
            <div>
              <label className="block text-sm text-gray-400 mb-1">Last Name</label>
              <input
                type="text"
                value={formData.lastName}
                onChange={(e) => setFormData({ ...formData, lastName: e.target.value })}
                className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
                placeholder="Doe"
              />
            </div>
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Display Name *</label>
            <input
              type="text"
              value={formData.displayName}
              onChange={(e) => setFormData({ ...formData, displayName: e.target.value })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
              placeholder="John Doe"
              required
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Phone *</label>
            <input
              type="tel"
              value={formData.phone}
              onChange={(e) => setFormData({ ...formData, phone: e.target.value })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
              placeholder="+1 (555) 123-4567"
              required
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Email</label>
            <input
              type="email"
              value={formData.email}
              onChange={(e) => setFormData({ ...formData, email: e.target.value })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
              placeholder="john@example.com"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Company</label>
            <input
              type="text"
              value={formData.company}
              onChange={(e) => setFormData({ ...formData, company: e.target.value })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
              placeholder="Acme Inc"
            />
          </div>

          <div>
            <label className="block text-sm text-gray-400 mb-1">Account Tier</label>
            <select
              value={formData.accountTier}
              onChange={(e) => setFormData({ ...formData, accountTier: e.target.value as any })}
              className="w-full px-3 py-2 bg-gray-700 border border-gray-600 rounded-lg text-white text-sm focus:outline-none focus:border-blue-500"
            >
              <option value="prospect">Prospect</option>
              <option value="free">Free</option>
              <option value="pro">Pro</option>
              <option value="enterprise">Enterprise</option>
            </select>
          </div>

          {error && (
            <div className="p-3 bg-red-500/20 border border-red-500 rounded-lg text-red-400 text-sm">
              {error}
            </div>
          )}

          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              onClick={onClose}
              className="px-4 py-2 bg-gray-700 hover:bg-gray-600 text-white rounded-lg transition-colors"
            >
              Cancel
            </button>
            <button
              type="submit"
              disabled={isSaving}
              className="px-4 py-2 bg-blue-600 hover:bg-blue-700 disabled:bg-gray-600 text-white rounded-lg transition-colors"
            >
              {isSaving ? 'Creating...' : 'Create Contact'}
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}

export default ContactList;
