import { cn } from '../../lib/utils';

interface SkeletonProps {
  className?: string;
}

export function Skeleton({ className }: SkeletonProps) {
  return (
    <div
      className={cn(
        'animate-pulse rounded-md bg-gray-700/50',
        className
      )}
    />
  );
}

/** Skeleton for a single contact list item */
export function ContactListSkeleton() {
  return (
    <div className="px-3 py-3 flex items-center gap-3">
      <Skeleton className="w-10 h-10 rounded-full flex-shrink-0" />
      <div className="flex-1 min-w-0 space-y-2">
        <Skeleton className="h-4 w-3/4" />
        <Skeleton className="h-3 w-1/2" />
      </div>
    </div>
  );
}

/** Skeleton for a list of contacts */
export function ContactListSkeletonGroup({ count = 8 }: { count?: number }) {
  return (
    <div className="divide-y divide-gray-700/30">
      {Array.from({ length: count }).map((_, i) => (
        <ContactListSkeleton key={i} />
      ))}
    </div>
  );
}

/** Skeleton for a call card in active calls or queue */
export function CallCardSkeleton() {
  return (
    <div className="px-3 py-3 flex items-center gap-3">
      <Skeleton className="w-10 h-10 rounded-full flex-shrink-0" />
      <div className="flex-1 min-w-0 space-y-2">
        <Skeleton className="h-4 w-2/3" />
        <Skeleton className="h-3 w-1/3" />
      </div>
      <div className="space-y-2">
        <Skeleton className="h-3 w-16" />
        <Skeleton className="h-3 w-12 ml-auto" />
      </div>
    </div>
  );
}

/** Skeleton for call cards list */
export function CallListSkeletonGroup({ count = 5 }: { count?: number }) {
  return (
    <div className="divide-y divide-gray-700/30">
      {Array.from({ length: count }).map((_, i) => (
        <CallCardSkeleton key={i} />
      ))}
    </div>
  );
}

/** Skeleton for the contact detail header area */
export function ContactDetailSkeleton() {
  return (
    <div className="p-6 space-y-6">
      {/* Header */}
      <div className="flex items-center gap-4">
        <Skeleton className="w-16 h-16 rounded-full" />
        <div className="space-y-2 flex-1">
          <Skeleton className="h-6 w-48" />
          <Skeleton className="h-4 w-32" />
          <Skeleton className="h-3 w-24" />
        </div>
      </div>
      {/* Tabs */}
      <div className="flex gap-2">
        <Skeleton className="h-8 w-20 rounded-md" />
        <Skeleton className="h-8 w-20 rounded-md" />
        <Skeleton className="h-8 w-20 rounded-md" />
        <Skeleton className="h-8 w-20 rounded-md" />
      </div>
      {/* Content */}
      <div className="space-y-3">
        <Skeleton className="h-16 w-full rounded-lg" />
        <Skeleton className="h-16 w-full rounded-lg" />
        <Skeleton className="h-16 w-full rounded-lg" />
      </div>
    </div>
  );
}

/** Skeleton for queue items */
export function QueueItemSkeleton() {
  return (
    <div className="px-3 py-3 flex items-center gap-3">
      <div className="flex-1 min-w-0 space-y-2">
        <div className="flex items-center gap-2">
          <Skeleton className="h-5 w-5 rounded-full" />
          <Skeleton className="h-4 w-1/2" />
        </div>
        <Skeleton className="h-3 w-2/3" />
      </div>
      <Skeleton className="h-8 w-16 rounded-md" />
    </div>
  );
}
