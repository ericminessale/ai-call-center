import { ViewMode } from '../../pages/UnifiedAgentDesktop';
import { ContactList } from '../contacts/ContactList';
import { ActiveCallsList } from './ActiveCallsList';
import { QueueList } from './QueueList';
import { SupervisorPanel } from './SupervisorPanel';
import { ContactMinimal, Call, QueueConfig } from '../../types/callcenter';

interface LeftPanelProps {
  viewMode: ViewMode;
  // Contact props
  contacts: ContactMinimal[];
  selectedContactId?: number;
  onSelectContact: (contact: ContactMinimal) => void;
  onSearch: (query: string) => void;
  onContactCreated: (contact: ContactMinimal) => void;
  searchQuery: string;
  isLoadingContacts: boolean;
  // Call props
  activeCalls: Call[];
  queuedCalls: Call[];
  onSelectCall: (call: Call) => void;
  onTakeCall: (call: Call) => void;
  isLoadingCalls?: boolean;
  isLoadingQueue?: boolean;
  // Queue configs for filter pills + badges
  queueConfigs?: QueueConfig[];
}

export function LeftPanel({
  viewMode,
  contacts,
  selectedContactId,
  onSelectContact,
  onSearch,
  onContactCreated,
  searchQuery,
  isLoadingContacts,
  activeCalls,
  queuedCalls,
  onSelectCall,
  onTakeCall,
  isLoadingCalls,
  isLoadingQueue,
  queueConfigs,
}: LeftPanelProps) {
  switch (viewMode) {
    case 'contacts':
      return (
        <ContactList
          contacts={contacts}
          selectedContactId={selectedContactId}
          onSelectContact={onSelectContact}
          onSearch={onSearch}
          onContactCreated={onContactCreated}
          searchQuery={searchQuery}
          isLoading={isLoadingContacts}
        />
      );

    case 'calls':
      return (
        <ActiveCallsList
          calls={activeCalls}
          onSelectCall={onSelectCall}
          isLoading={isLoadingCalls}
          queueConfigs={queueConfigs}
        />
      );

    case 'queue':
      return (
        <QueueList
          calls={queuedCalls}
          onSelectCall={onSelectCall}
          onTakeCall={onTakeCall}
          isLoading={isLoadingQueue}
          queueConfigs={queueConfigs}
        />
      );

    case 'supervisor':
      return (
        <SupervisorPanel
          activeCalls={activeCalls}
          onSelectCall={onSelectCall}
        />
      );

    default:
      return null;
  }
}

export default LeftPanel;
