import { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import { useAuthStore } from '../stores/authStore';
import { ContactList } from '../components/contacts/ContactList';
import { ContactDetailView } from '../components/contacts/ContactDetailView';
import { GlobalNav } from '../components/callcenter/GlobalNav';
import { contactsApi } from '../services/api';
import { Contact, ContactMinimal } from '../types/callcenter';
import { Phone, Users, Activity, Settings, LogOut } from 'lucide-react';

export function ContactsPage() {
  const { contactId } = useParams<{ contactId?: string }>();
  const navigate = useNavigate();
  const { user, logout } = useAuthStore();

  const [contacts, setContacts] = useState<ContactMinimal[]>([]);
  const [selectedContact, setSelectedContact] = useState<Contact | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [searchQuery, setSearchQuery] = useState('');

  // Load contacts
  const loadContacts = useCallback(async () => {
    try {
      const response = await contactsApi.list({
        search: searchQuery || undefined,
        per_page: 100,
        sort_by: 'last_interaction',
      });
      setContacts(response.data.contacts);
    } catch (error) {
      console.error('Failed to load contacts:', error);
    } finally {
      setIsLoading(false);
    }
  }, [searchQuery]);

  // Load selected contact details
  const loadContactDetail = useCallback(async (id: number) => {
    try {
      const response = await contactsApi.get(id);
      setSelectedContact(response.data);
    } catch (error) {
      console.error('Failed to load contact:', error);
      setSelectedContact(null);
    }
  }, []);

  // Initial load
  useEffect(() => {
    loadContacts();
  }, [loadContacts]);

  // Load contact detail when URL changes
  useEffect(() => {
    if (contactId) {
      const id = parseInt(contactId, 10);
      if (!isNaN(id)) {
        loadContactDetail(id);
      }
    } else {
      setSelectedContact(null);
    }
  }, [contactId, loadContactDetail]);

  // Handle contact selection
  const handleContactSelect = (contact: ContactMinimal) => {
    navigate(`/contacts/${contact.id}`);
  };

  // Handle search
  const handleSearch = (query: string) => {
    setSearchQuery(query);
  };

  // Debounced search effect
  useEffect(() => {
    const timer = setTimeout(() => {
      loadContacts();
    }, 300);
    return () => clearTimeout(timer);
  }, [searchQuery, loadContacts]);

  // Handle contact update from detail view
  const handleContactUpdate = (updatedContact: Contact) => {
    setSelectedContact(updatedContact);
    // Update in list too
    setContacts(prev =>
      prev.map(c =>
        c.id === updatedContact.id
          ? {
              ...c,
              displayName: updatedContact.displayName,
              phone: updatedContact.phone,
              company: updatedContact.company,
              accountTier: updatedContact.accountTier,
              isVip: updatedContact.isVip,
              totalCalls: updatedContact.totalCalls,
              lastInteractionAt: updatedContact.lastInteractionAt,
            }
          : c
      )
    );
  };

  // Handle new contact created
  const handleContactCreated = (newContact: ContactMinimal) => {
    setContacts(prev => [newContact, ...prev]);
    navigate(`/contacts/${newContact.id}`);
  };

  return (
    <div className="h-screen flex flex-col bg-gray-900">
      {/* Top Navigation */}
      <header className="h-14 bg-gray-800 border-b border-gray-700 flex items-center justify-between px-4">
        <div className="flex items-center gap-4">
          <h1 className="text-lg font-semibold text-white">SignalWire Call Center</h1>
          <nav className="flex items-center gap-1 ml-8">
            <NavButton
              icon={<Users className="w-4 h-4" />}
              label="Contacts"
              active={true}
              onClick={() => navigate('/contacts')}
            />
            <NavButton
              icon={<Phone className="w-4 h-4" />}
              label="Active Calls"
              onClick={() => navigate('/dashboard')}
            />
            <NavButton
              icon={<Activity className="w-4 h-4" />}
              label="Supervisor"
              onClick={() => navigate('/supervisor')}
            />
          </nav>
        </div>

        <div className="flex items-center gap-4">
          <span className="text-sm text-gray-400">{user?.email}</span>
          <button
            onClick={() => logout()}
            className="p-2 text-gray-400 hover:text-white hover:bg-gray-700 rounded-lg transition-colors"
            title="Logout"
          >
            <LogOut className="w-4 h-4" />
          </button>
        </div>
      </header>

      {/* Main Content */}
      <div className="flex-1 flex overflow-hidden">
        {/* Contact List (Left Panel) */}
        <div className="w-80 border-r border-gray-700 flex flex-col bg-gray-800">
          <ContactList
            contacts={contacts}
            selectedContactId={selectedContact?.id}
            onSelectContact={handleContactSelect}
            onSearch={handleSearch}
            onContactCreated={handleContactCreated}
            searchQuery={searchQuery}
            isLoading={isLoading}
          />
        </div>

        {/* Contact Detail (Right Panel) */}
        <div className="flex-1 bg-gray-900 overflow-hidden">
          {selectedContact ? (
            <ContactDetailView
              contact={selectedContact}
              onContactUpdate={handleContactUpdate}
            />
          ) : (
            <div className="h-full flex items-center justify-center text-gray-500">
              <div className="text-center">
                <Users className="w-16 h-16 mx-auto mb-4 opacity-50" />
                <p className="text-lg">Select a contact to view details</p>
                <p className="text-sm mt-2">
                  Or create a new contact to get started
                </p>
              </div>
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

// Simple nav button component
function NavButton({
  icon,
  label,
  active,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  active?: boolean;
  onClick?: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-2 px-3 py-2 rounded-lg text-sm transition-colors ${
        active
          ? 'bg-blue-600 text-white'
          : 'text-gray-400 hover:text-white hover:bg-gray-700'
      }`}
    >
      {icon}
      <span>{label}</span>
    </button>
  );
}

export default ContactsPage;
