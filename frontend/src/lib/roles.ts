// Role predicates — the UI mirror of backend/app/models/user.py's role tables.
// Keep the two in sync; the backend is the enforcement point, this only decides
// what renders.
//
// 'visitor' is the hosted-demo workspace owner (HIGH-3). Visitors used to be
// provisioned as full 'admin' users, which put the entire /api/admin/* surface
// in reach of an anonymous member of the public. They now have their own role:
// same operational reach inside their workspace, no admin-management powers.
// Anywhere this file's helpers are used instead of a `role === 'admin'` literal,
// that distinction is what's being expressed.

/** Supervisory reach over the whole workspace — see calls they don't own,
 *  listen/whisper/barge, read the scorecards. Mirrors SUPERVISORY_ROLES. */
export const SUPERVISORY_ROLES = ['admin', 'supervisor', 'visitor'] as const;

/** May open Settings and the /api/admin/* surface. Mirrors ADMIN_SURFACE_ROLES. */
export const ADMIN_SURFACE_ROLES = ['admin', 'visitor'] as const;

/** Admin-MANAGEMENT actions: user CRUD, permission grants, MCP-gateway writes.
 *  Mirrors FULL_ADMIN_ROLES — deliberately excludes 'visitor'. */
export const FULL_ADMIN_ROLES = ['admin'] as const;

const has = (roles: readonly string[], role?: string | null): boolean =>
  !!role && roles.includes(role);

export const isSupervisory = (role?: string | null): boolean =>
  has(SUPERVISORY_ROLES, role);

/** Can reach Settings — includes hosted visitors configuring their own demo. */
export const isAdminSurface = (role?: string | null): boolean =>
  has(ADMIN_SURFACE_ROLES, role);

/** Full workspace admin. Gate admin-management UI on this, not isAdminSurface,
 *  so a visitor never sees a control the backend will 403. */
export const isFullAdmin = (role?: string | null): boolean =>
  has(FULL_ADMIN_ROLES, role);
