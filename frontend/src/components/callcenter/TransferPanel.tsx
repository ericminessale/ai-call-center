import React, { useState } from 'react';
import { motion, AnimatePresence } from 'framer-motion';
import * as Dialog from '@radix-ui/react-dialog';
import * as Tabs from '@radix-ui/react-tabs';
import { X, Phone, Users, Building, ChevronRight } from 'lucide-react';
import { cn } from '../../lib/utils';
import type { Call, Queue } from '../../types/callcenter';

interface TransferPanelProps {
  isOpen: boolean;
  onClose: () => void;
  onTransfer: (destination: string, type: 'warm' | 'cold', notes?: string) => void;
  currentCall: Call | null;
  queues: Queue[];
}

export const TransferPanel: React.FC<TransferPanelProps> = ({
  isOpen,
  onClose,
  onTransfer,
  currentCall,
  queues
}) => {
  const [selectedQueue, setSelectedQueue] = useState<string>('');
  const [transferType, setTransferType] = useState<'warm' | 'cold'>('warm');
  const [transferNotes, setTransferNotes] = useState('');
  const [includeContext, setIncludeContext] = useState(true);

  const handleTransfer = () => {
    if (selectedQueue) {
      onTransfer(selectedQueue, transferType, transferNotes);
      // Reset state
      setSelectedQueue('');
      setTransferNotes('');
      setTransferType('warm');
    }
  };

  return (
    <AnimatePresence>
      {isOpen && (
        <Dialog.Root open={isOpen} onOpenChange={onClose}>
          <Dialog.Portal>
            <Dialog.Overlay asChild>
              <motion.div
                className="fixed inset-0 bg-black/50 z-40"
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                exit={{ opacity: 0 }}
              />
            </Dialog.Overlay>

            <Dialog.Content asChild>
              <motion.div
                className="fixed right-0 top-0 h-full w-[450px] bg-white shadow-xl z-50"
                initial={{ x: '100%' }}
                animate={{ x: 0 }}
                exit={{ x: '100%' }}
                transition={{ type: 'spring', damping: 25 }}
              >
                <div className="h-full flex flex-col">
                  {/* Header */}
                  <div className="px-6 py-4 border-b border-gray-200">
                    <div className="flex items-center justify-between">
                      <div>
                        <h2 className="text-lg font-semibold text-gray-900">Transfer Call</h2>
                        <p className="text-sm text-gray-600 mt-1">
                          Select destination and transfer type
                        </p>
                      </div>
                      <Dialog.Close asChild>
                        <button className="p-2 hover:bg-gray-100 rounded-lg transition-colors">
                          <X className="w-5 h-5 text-gray-500" />
                        </button>
                      </Dialog.Close>
                    </div>
                  </div>

                  {/* Content */}
                  <div className="flex-1 overflow-y-auto p-6">
                    <Tabs.Root defaultValue="department" className="w-full">
                      <Tabs.List className="grid w-full grid-cols-3 mb-6">
                        <Tabs.Trigger
                          value="department"
                          className="px-3 py-2 text-sm font-medium text-gray-700 border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600"
                        >
                          <Building className="w-4 h-4 inline mr-1" />
                          Department
                        </Tabs.Trigger>
                        <Tabs.Trigger
                          value="agent"
                          className="px-3 py-2 text-sm font-medium text-gray-700 border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600"
                        >
                          <Users className="w-4 h-4 inline mr-1" />
                          Agent
                        </Tabs.Trigger>
                        <Tabs.Trigger
                          value="external"
                          className="px-3 py-2 text-sm font-medium text-gray-700 border-b-2 border-transparent data-[state=active]:border-blue-600 data-[state=active]:text-blue-600"
                        >
                          <Phone className="w-4 h-4 inline mr-1" />
                          External
                        </Tabs.Trigger>
                      </Tabs.List>

                      {/* Department Tab */}
                      <Tabs.Content value="department" className="space-y-4">
                        <div className="space-y-2">
                          {queues.map((queue) => (
                            <button
                              key={queue.id}
                              onClick={() => setSelectedQueue(queue.id)}
                              className={cn(
                                'w-full p-3 rounded-lg border text-left transition-all',
                                selectedQueue === queue.id
                                  ? 'border-blue-500 bg-blue-50'
                                  : 'border-gray-200 hover:border-gray-300'
                              )}
                            >
                              <div className="flex items-center justify-between">
                                <div>
                                  <div className="font-medium text-gray-900">{queue.name}</div>
                                  <div className="text-sm text-gray-500">
                                    {queue.waiting} waiting • Avg wait: {Math.floor(queue.avgWait / 60)}m
                                  </div>
                                </div>
                                <ChevronRight className="w-4 h-4 text-gray-400" />
                              </div>
                            </button>
                          ))}
                        </div>

                        {/* Transfer with Context */}
                        <div className="p-4 bg-blue-50 rounded-lg border border-blue-200">
                          <label className="flex items-center">
                            <input
                              type="checkbox"
                              checked={includeContext}
                              onChange={(e) => setIncludeContext(e.target.checked)}
                              className="mr-2 rounded"
                            />
                            <div>
                              <span className="text-sm font-medium text-blue-900">
                                Transfer with AI Context
                              </span>
                              <p className="text-xs text-blue-700 mt-0.5">
                                Include AI summary and extracted information
                              </p>
                            </div>
                          </label>
                        </div>
                      </Tabs.Content>

                      {/* Agent Tab */}
                      <Tabs.Content value="agent">
                        <div className="text-center text-gray-500 py-8">
                          <Users className="w-12 h-12 text-gray-300 mx-auto mb-2" />
                          <p>Agent selection coming soon</p>
                        </div>
                      </Tabs.Content>

                      {/* External Tab */}
                      <Tabs.Content value="external">
                        <div className="text-center text-gray-500 py-8">
                          <Phone className="w-12 h-12 text-gray-300 mx-auto mb-2" />
                          <p>External transfer coming soon</p>
                        </div>
                      </Tabs.Content>
                    </Tabs.Root>

                    {/* Transfer Type */}
                    <div className="mt-6">
                      <label className="block text-sm font-medium text-gray-700 mb-2">
                        Transfer Type
                      </label>
                      <div className="grid grid-cols-2 gap-2">
                        <button
                          onClick={() => setTransferType('warm')}
                          className={cn(
                            'px-4 py-2 rounded-lg border text-sm font-medium transition-colors',
                            transferType === 'warm'
                              ? 'border-blue-500 bg-blue-50 text-blue-700'
                              : 'border-gray-200 text-gray-700 hover:bg-gray-50'
                          )}
                        >
                          Warm Transfer
                        </button>
                        <button
                          onClick={() => setTransferType('cold')}
                          className={cn(
                            'px-4 py-2 rounded-lg border text-sm font-medium transition-colors',
                            transferType === 'cold'
                              ? 'border-blue-500 bg-blue-50 text-blue-700'
                              : 'border-gray-200 text-gray-700 hover:bg-gray-50'
                          )}
                        >
                          Cold Transfer
                        </button>
                      </div>
                      <p className="text-xs text-gray-500 mt-2">
                        {transferType === 'warm'
                          ? 'You will introduce the caller before transferring'
                          : 'Direct transfer without introduction'}
                      </p>
                    </div>

                    {/* Transfer Notes */}
                    <div className="mt-6">
                      <label className="block text-sm font-medium text-gray-700 mb-2">
                        Notes for Receiving Agent
                      </label>
                      <textarea
                        value={transferNotes}
                        onChange={(e) => setTransferNotes(e.target.value)}
                        className="w-full px-3 py-2 border border-gray-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-blue-500"
                        rows={3}
                        placeholder="Add any relevant notes..."
                      />
                    </div>
                  </div>

                  {/* Footer */}
                  <div className="px-6 py-4 border-t border-gray-200 bg-gray-50">
                    <div className="flex justify-between items-center">
                      <button
                        onClick={onClose}
                        className="px-4 py-2 text-sm font-medium text-gray-700 hover:text-gray-900"
                      >
                        Cancel
                      </button>
                      <button
                        onClick={handleTransfer}
                        disabled={!selectedQueue}
                        className={cn(
                          'px-6 py-2 rounded-lg text-sm font-medium transition-colors',
                          selectedQueue
                            ? 'bg-blue-600 text-white hover:bg-blue-700'
                            : 'bg-gray-200 text-gray-400 cursor-not-allowed'
                        )}
                      >
                        Transfer Call
                      </button>
                    </div>
                  </div>
                </div>
              </motion.div>
            </Dialog.Content>
          </Dialog.Portal>
        </Dialog.Root>
      )}
    </AnimatePresence>
  );
};