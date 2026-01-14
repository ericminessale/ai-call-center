import React, { useState, useEffect } from 'react';
import * as DropdownMenu from '@radix-ui/react-dropdown-menu';
import { motion, AnimatePresence } from 'framer-motion';
import {
  CheckCircle,
  Phone,
  Clock,
  Coffee,
  XCircle,
  ChevronDown
} from 'lucide-react';
import { cn, formatTime } from '../../lib/utils';
import type { AgentStatus } from '../../types/callcenter';

interface StatusOption {
  id: AgentStatus;
  label: string;
  color: string;
  icon: React.ComponentType<any>;
  shortcut: string;
}

interface StatusSelectorProps {
  currentStatus: AgentStatus;
  onStatusChange: (status: AgentStatus) => void;
  statusDuration?: number;
}

const statuses: StatusOption[] = [
  {
    id: 'available',
    label: 'Available',
    color: 'green',
    icon: CheckCircle,
    shortcut: '1'
  },
  {
    id: 'busy',
    label: 'On Call',
    color: 'red',
    icon: Phone,
    shortcut: '2'
  },
  {
    id: 'after-call',
    label: 'After Call',
    color: 'orange',
    icon: Clock,
    shortcut: '3'
  },
  {
    id: 'break',
    label: 'Break',
    color: 'yellow',
    icon: Coffee,
    shortcut: '4'
  },
  {
    id: 'offline',
    label: 'Offline',
    color: 'gray',
    icon: XCircle,
    shortcut: '5'
  }
];

const statusColors = {
  green: 'bg-green-100 border-green-500 text-green-700',
  red: 'bg-red-100 border-red-500 text-red-700',
  orange: 'bg-orange-100 border-orange-500 text-orange-700',
  yellow: 'bg-yellow-100 border-yellow-500 text-yellow-700',
  gray: 'bg-gray-100 border-gray-500 text-gray-700'
};

const statusIconColors = {
  green: 'text-green-600',
  red: 'text-red-600',
  orange: 'text-orange-600',
  yellow: 'text-yellow-600',
  gray: 'text-gray-600'
};

export const StatusSelector: React.FC<StatusSelectorProps> = ({
  currentStatus,
  onStatusChange,
  statusDuration = 0
}) => {
  const [open, setOpen] = useState(false);
  const current = statuses.find(s => s.id === currentStatus);
  const StatusIcon = current?.icon || CheckCircle;

  // Keyboard shortcut handler
  useEffect(() => {
    const handleKeyPress = (e: KeyboardEvent) => {
      if (e.altKey) {
        const status = statuses.find(s => s.shortcut === e.key);
        if (status) {
          e.preventDefault();
          onStatusChange(status.id);
        }
      }
    };

    window.addEventListener('keydown', handleKeyPress);
    return () => window.removeEventListener('keydown', handleKeyPress);
  }, [onStatusChange]);

  return (
    <DropdownMenu.Root open={open} onOpenChange={setOpen}>
      <DropdownMenu.Trigger asChild>
        <motion.button
          className={cn(
            'flex items-center space-x-2 px-4 py-2 rounded-lg transition-all',
            'border-2 hover:shadow-md focus:outline-none focus:ring-2',
            current ? statusColors[current.color as keyof typeof statusColors] : '',
            current ? `focus:ring-${current.color}-400` : ''
          )}
          whileHover={{ scale: 1.02 }}
          whileTap={{ scale: 0.98 }}
        >
          <StatusIcon
            className={cn(
              'w-5 h-5',
              current ? statusIconColors[current.color as keyof typeof statusIconColors] : ''
            )}
          />
          <span className="font-medium">{current?.label}</span>
          {statusDuration > 0 && (
            <span className="text-sm opacity-75">
              ({formatTime(statusDuration)})
            </span>
          )}
          <ChevronDown
            className={cn(
              'w-4 h-4 transition-transform',
              open ? 'rotate-180' : ''
            )}
          />
        </motion.button>
      </DropdownMenu.Trigger>

      <AnimatePresence>
        {open && (
          <DropdownMenu.Portal forceMount>
            <DropdownMenu.Content asChild>
              <motion.div
                className="bg-white rounded-lg shadow-xl border border-gray-200 p-1 min-w-[200px] z-50"
                initial={{ opacity: 0, y: -10 }}
                animate={{ opacity: 1, y: 0 }}
                exit={{ opacity: 0, y: -10 }}
                transition={{ duration: 0.15 }}
              >
                {statuses.map((status, index) => {
                  const Icon = status.icon;
                  const isActive = status.id === currentStatus;

                  return (
                    <DropdownMenu.Item key={status.id} asChild>
                      <motion.button
                        onClick={() => {
                          onStatusChange(status.id);
                          setOpen(false);
                        }}
                        className={cn(
                          'w-full flex items-center px-3 py-2.5 rounded-md',
                          'hover:bg-gray-50 focus:bg-gray-50 focus:outline-none',
                          'transition-colors cursor-pointer',
                          isActive && 'bg-gray-100'
                        )}
                        whileHover={{ x: 2 }}
                      >
                        <Icon
                          className={cn(
                            'w-5 h-5 mr-3',
                            statusIconColors[status.color as keyof typeof statusIconColors]
                          )}
                        />
                        <span className="flex-1 text-left text-sm font-medium">
                          {status.label}
                        </span>
                        <kbd className="text-xs bg-gray-100 px-2 py-0.5 rounded border border-gray-200">
                          Alt+{status.shortcut}
                        </kbd>
                      </motion.button>
                    </DropdownMenu.Item>
                  );
                })}

                {/* Status Duration Info */}
                {statusDuration > 0 && (
                  <div className="px-3 py-2 mt-1 pt-2 border-t border-gray-200">
                    <p className="text-xs text-gray-500">
                      Current status for: {formatTime(statusDuration)}
                    </p>
                  </div>
                )}
              </motion.div>
            </DropdownMenu.Content>
          </DropdownMenu.Portal>
        )}
      </AnimatePresence>
    </DropdownMenu.Root>
  );
};