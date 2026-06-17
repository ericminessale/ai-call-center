import { ViewMode } from '../../pages/UnifiedAgentDesktop';
import { ContactList } from '../contacts/ContactList';
import { ActiveCallsList } from './ActiveCallsList';
import { QueueList } from './QueueList';
import { SupervisorPanel } from './SupervisorPanel';
import { CallbacksList } from './CallbacksList';
import { ContactMinimal, Call, QueueConfig } from '../../types/callcenter';
import { Callback } from '../../services/api';

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
  /** Queue view: select a WAITING call into the bespoke queue detail panel
   *  (stays in the queue view rather than navigating to the contact). */
  onSelectQueuedCall?: (call: Call) => void;
  /** Id of the queued call currently open in the queue detail panel. */
  selectedQueuedCallId?: number | null;
  isLoadingCalls?: boolean;
  isLoadingQueue?: boolean;
  // Queue configs for filter pills + badges
  queueConfigs?: QueueConfig[];
  // Callback System (Tier 2r)
  selectedCallbackId?: number | null;
  onSelectCallback?: (callback: Callback) => void;
  onPendingCallbackCountChange?: (count: number) => void;
  /** Optional one-shot override of the CallbacksList filter — used by the
   *  Contact banner so a deep link lands on a tab containing the row. */
  callbacksForceFilter?: 'pending' | 'mine' | 'completed' | null;
  /** Fired once the override has been applied so the parent can clear it. */
  onCallbacksFilterAck?: () => void;
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
  onSelectQueuedCall,
  selectedQueuedCallId,
  isLoadingCalls,
  isLoadingQueue,
  queueConfigs,
  selectedCallbackId,
  onSelectCallback,
  onPendingCallbackCountChange,
  callbacksForceFilter,
  onCallbacksFilterAck,
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
          selectedContactId={selectedContactId}
        />
      );

    case 'queue':
      return (
        <QueueList
          calls={queuedCalls}
          onSelectCall={onSelectQueuedCall ?? onSelectCall}
          onTakeCall={onTakeCall}
          isLoading={isLoadingQueue}
          queueConfigs={queueConfigs}
          selectedCallId={selectedQueuedCallId ?? undefined}
        />
      );

    case 'callbacks':
      return (
        <CallbacksList
          selectedId={selectedCallbackId ?? null}
          onSelect={(cb) => onSelectCallback?.(cb)}
          onPendingCountChange={onPendingCallbackCountChange}
          forceFilter={callbacksForceFilter ?? null}
          onForceFilterAck={onCallbacksFilterAck}
        />
      );

    case 'supervisor':
      return (
        <SupervisorPanel
          activeCalls={activeCalls}
          onSelectCall={onSelectCall}
          selectedContactId={selectedContactId}
        />
      );

    default:
      return null;
  }
}

export default LeftPanel;
