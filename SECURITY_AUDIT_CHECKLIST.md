# Security Audit Checklist

This checklist is the gate every milestone PR for hosted-demo work passes
through before merging. The mandate is in `memory/roadmap.md` →
`HOSTED DEMO MODE` → `Security audit gates`.

The audit runs against the *whole demo surface* at each milestone, not
just the new code in that PR — regressions in earlier work are caught
here too.

---

## How to use

1. Pick the section corresponding to the milestone that just landed
   (M1, M2, M3, M4, or M5+).
2. Walk every item. Each is either ✅ (pass), ❌ (fail), or N/A (with a
   one-line note explaining why it doesn't apply).
3. Any ❌ blocks merge. Fix it, push a follow-up, re-audit just that
   item.
4. After all items pass, paste the filled-in checklist into the PR
   description as the audit record. Don't keep audit results in this
   file — it's a template, not a log.
5. If a new attack surface appears that this checklist doesn't cover,
   add an item *now* so the next milestone audit catches it.

---

## Universal items — every audit, regardless of milestone

These items apply to every audit because they protect the canonical
clone-and-own path that the hosted demo runs on top of.

- [ ] **DEMO_MODE off ⇒ no demo behavior anywhere.** Set
      `DEMO_MODE=` (unset) in `.env`, restart, confirm:
      - `GET /api/config/runtime` returns `{"demo_mode": false, "demo_phone_numbers": []}`.
      - `POST /api/demo/start` returns `404`.
      - `users` table contains zero rows with `role='demo_agent'`.
      - Login flow renders the production form (not the landing card).
      - Existing user accounts (`admin@callcenter.com`, real teammates)
        still work normally.
- [ ] **Production secrets fail-fast.** With `SECRET_KEY` or
      `JWT_SECRET_KEY` unset, the backend refuses to start (no
      `'dev-secret'` fallback). See `app/__init__.py::_require_env`.
- [ ] **CORS allowlist enforced.** Browser requests from
      `https://evil.example.com` against the deployed origin do NOT
      receive an `Access-Control-Allow-Origin` header. See
      `app/__init__.py::_cors_origins`.
- [ ] **Webhook signature/auth in place.** SignalWire-facing endpoints
      (`/api/webhooks/*`, `/api/queues/<id>/route`) are protected by
      `@require_webhook_auth`. With `WEBHOOK_AUTH_REQUIRED=true`, an
      unauthenticated POST returns 401. See
      `app/utils/webhook_auth.py`.
- [ ] **No tracked secrets.** `git log -p` for the audited range shows
      no API tokens, passwords, JWTs, Fernet keys. `.env` is gitignored.
- [ ] **No new debug logging.** No `console.log` in frontend production
      builds (gated through `lib/logger`); no `print(..., flush=True)`
      in backend routes.

---

## M1 — DEMO_MODE flag + seeded personas + landing card

The first audit. Confirms the gate itself is unbypassable and the
demo-only entrypoint can't be hit from a production deployment.

- [ ] **All universal items pass.**
- [ ] **`/api/demo/start` is 404 in production.**  With `DEMO_MODE`
      unset, hitting the endpoint returns `404` — not 401, not 405.
      The route should not even hint that it exists.
- [ ] **`/api/demo/start` returns valid JWT in demo.** With
      `DEMO_MODE=true` and seeded personas, the endpoint returns
      `{access_token, refresh_token, user}` and the JWT verifies under
      `JWT_SECRET_KEY`.
- [ ] **No persona enumeration.** `/api/demo/start` always returns the
      *same* persona in M1 (the lease layer ships in M2). Verify it's
      not rotating, randomizing, or exposing other personas via path or
      query manipulation.
- [ ] **Demo personas isolated from User Management.**
      `GET /api/admin/users` (as a real admin) returns zero rows with
      `role='demo_agent'`, even though they exist in the DB.
- [ ] **Demo personas immutable from admin endpoints.**
      `PUT /api/admin/users/<demo_id>` (role/languages/permissions) and
      `DELETE /api/admin/users/<demo_id>` all return `403` with the
      "Demo personas cannot be modified" message. Verify by directly
      hitting each endpoint with a known demo persona ID.
- [ ] **Seed is idempotent.** Restart the backend twice with
      `DEMO_MODE=true`. The persona count stays at `DEMO_POOL_SIZE`;
      no duplicates, no errors.
- [ ] **Seed gated correctly.** Restart with `DEMO_MODE=` unset. No new
      `demo_agent` rows are inserted. Existing rows (if any) are not
      modified.
- [ ] **Demo persona credentials are unguessable.** Pick one demo
      persona's email and try logging in with the M1 placeholder
      password generation. Login should fail (random `token_urlsafe`
      hash, never the literal string "demo" or similar).
- [ ] **Runtime config exposes nothing sensitive.**
      `GET /api/config/runtime` payload contains only `demo_mode` and
      `demo_phone_numbers`. No env vars leaked, no internal hostnames,
      no DB connection info.
- [ ] **Login form does not appear when demo_mode=true.** Visiting
      `/login` renders `<DemoLanding />`, not the password form. There
      is no path that exposes the password form in demo mode.
- [ ] **`role='demo_agent'` rejected by VALID_USER_ROLES.** The role
      string is reserved — the admin role-update endpoint refuses to
      assign it via the API. Try `PUT /api/admin/users/<id>` with
      `{"role": "demo_agent"}` against a real teammate; expect `400`.

---

## M2 — Subscriber pool + lease management

Adds when M2 ships.

- [ ] **All universal + M1 items still pass** after M2's changes.
- [ ] **Lease cookie is session-only and HttpOnly.** Anonymous session
      cookie is `HttpOnly`, `SameSite=Lax`, `Secure` in production.
      Cannot be read by JS.
- [ ] **One-active-lease-per-subscriber enforced at the app layer.**
      Two browsers with two session cookies should never both hold
      `demo-agent-05`. Verify even if SignalWire allows concurrent
      tokens, our lease table refuses overlapping leases.
- [ ] **Lease release on disconnect works.** Closing the browser
      releases the lease within the configured idle timeout (≤5 min).
      Pool depth recovers without operator action.
- [ ] **Pool exhaustion is graceful.** When all 20 leases are held,
      the 21st `POST /api/demo/start` returns a clear "demo full" 503,
      not a crash, not a 5xx, not a leaked stale lease.
- [ ] **No subscriber CRUD via demo session JWT.** A demo persona's
      JWT has zero ability to call SignalWire CRUD endpoints
      (`/api/admin/*`, subscriber create/delete). Verified by hitting
      each admin endpoint with a demo JWT — expect `401`/`403`.
- [ ] **Concurrency test from research is settled.** Either: empirical
      test confirmed SignalWire kicks the old session, OR our app
      layer prevents concurrent leases anyway. Document which.

---

## M3 — Lockdowns

- [ ] **All universal + M1 + M2 items still pass.**
- [ ] **Outbound dial blocked at the frontend (primary gate).** The
      Call Fabric SDK's browser-direct WebRTC dial bypasses the
      backend entirely on the WebRTC half, so the *primary* gate is
      `CallFabricContext.makeCall` refusing immediately when
      `runtimeConfig.demo_mode === true`. Verify clicking any dial
      button (contact "Call", QuickDialDropdown, etc.) shows a toast
      and the network tab shows zero outbound API calls.
- [ ] **Outbound dial blocked at the backend (defense in depth).** Any
      code path that calls `client.calling.create_call()` (outbound)
      returns a "demo mode" error in `DEMO_MODE=true`. Verify by
      tracing every call site.
- [ ] **Subscriber CRUD blocked.** Even a real admin JWT cannot
      create/update/delete `demo_agent` users while in demo mode. The
      pool is operator-only, not user-editable.
- [ ] **Recording disabled by default.** SWML returned for inbound
      calls in demo mode does not include `record_call`. Verify the
      generated SWML payload by hitting `/api/swml/initial-call`.
- [ ] **Per-caller-ID inbound ratelimit enforced.** Spam an inbound
      from one phone number; verify the Nth call gets a polite reject
      SWML, not a successful agent connection.

---

## M4 — Daily reset cron

- [ ] **All universal + M1–M3 items still pass.**
- [ ] **Reset wipes the right tables.** After the cron runs:
      `calls`, `call_legs`, `transcriptions`, `contacts` (non-seed),
      `returns` (DemoShop), Redis queue state are empty / reset.
      Demo personas + DemoShop products + queue config are preserved.
- [ ] **Reset doesn't touch real teammate data.** A real
      `admin@callcenter.com`-style row survives untouched. Calls
      assigned to real users (if any in demo mode) get cleaned up
      without breaking FKs.
- [ ] **Active sessions notified gracefully.** A leased visitor at
      reset time sees a "demo refreshing, please reload" toast and
      gets logged out cleanly. No crashes.
- [ ] **Reset is idempotent.** Running the reset twice in a row is a
      no-op the second time, not a crash.

---

## M5 — Kamal deploy + domain + TLS (the big one)

The full pen-test of the public surface. Run *after* the demo is live
on the real domain.

- [ ] **All universal + M1–M4 items still pass against the live
      domain.**
- [ ] **TLS certificate valid.** Real Let's Encrypt cert for the
      domain, no self-signed warnings.
- [ ] **HTTPS-only.** Plain HTTP redirects to HTTPS; no cleartext
      paths.
- [ ] **No exposed dev/debug endpoints.** Hit the production domain
      with paths like `/debug`, `/_admin`, `/cgi-bin/*`,
      `/.git/config`, `/etc/passwd` (path traversal attempts), `/api`
      (without an endpoint). All return 404.
- [ ] **Cloudflare in front.** `dig` shows Cloudflare IPs; origin DO
      box is not directly reachable from the public internet by IP.
- [ ] **Rate limits hold under load.** Hit `/api/demo/start` 100 times
      from one IP rapidly; subsequent requests get 429.
- [ ] **Open ports**. From a remote box: only 80/443 are reachable on
      the DO droplet. Postgres (5432), Redis (6379), backend (5000),
      ai-agents (8080), demo-mcp-gateway (8100) are all internal-only.
- [ ] **Env vars not leaked.** Hit `/api/config/runtime` and any error
      pages. Confirm no env values appear in responses.
- [ ] **Subdomain takeover not possible.** If using a subdomain that
      has historically pointed elsewhere, confirm CNAME isn't dangling.
- [ ] **JWT secrets rotated.** The `JWT_SECRET_KEY` on the production
      box is fresh (`secrets.token_urlsafe(32)` output), not the
      development placeholder.
- [ ] **Logs don't capture sensitive data.** Spot-check a recent log
      window — no JWTs, passwords, API tokens, full credit cards (none
      should ever appear; this is a sanity check).

---

## Continuous — every subsequent commit on demo surface

Beyond the milestone audits, every codebase change in demo mode
re-runs the relevant section above. Lighter weight: just the items
covering the surface area touched by the change. Don't re-do the full
sweep on every commit.

If a security-relevant change ships outside a milestone (e.g., adding
a new endpoint that handles untrusted input), explicitly call it out
in the PR description and re-run the universal items + any milestone
section that section overlaps with.
