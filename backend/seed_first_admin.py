#!/usr/bin/env python3
"""Idempotently seed the first admin user from environment variables.

DEPLOY-C3 (2026-07-07 pre-deploy): the old bootstrap seeded
``admin@callcenter.com`` / ``Admin123!`` on every fresh DB via init.sql and
create_admin.py — a publicly-known password in a public repo, so anyone could
sign in as admin on the hosted instance. This replaces that with an
env-provided credential and NO default:

  * If ``ADMIN_PASSWORD`` (and optionally ``ADMIN_EMAIL``) are set, create the
    admin only if it doesn't already exist. Existing admins are never
    overwritten (safe to run on every boot).
  * If ``ADMIN_PASSWORD`` is unset, do nothing and log a warning. A real
    operator provisions the admin explicitly; we never fall back to a known
    password.

Run by entrypoint.sh after ``flask db upgrade``.
"""

import os
import sys


def main() -> int:
    email = (os.getenv('ADMIN_EMAIL') or 'admin@callcenter.com').strip().lower()
    password = os.getenv('ADMIN_PASSWORD')

    if not password:
        print(
            "[seed_first_admin] ADMIN_PASSWORD not set — skipping admin seed. "
            "Provision an admin explicitly (set ADMIN_EMAIL/ADMIN_PASSWORD) "
            "before going live.",
            flush=True,
        )
        return 0

    if len(password) < 12:
        print(
            "[seed_first_admin] ADMIN_PASSWORD is shorter than 12 characters — "
            "refusing to seed a weak admin credential on a public instance.",
            flush=True,
        )
        return 1

    # Import inside main so a bad env doesn't blow up at module load.
    from app import create_app, db
    from app.models.user import User

    app = create_app()
    with app.app_context():
        existing = User.query.filter_by(email=email).first()
        if existing:
            print(
                f"[seed_first_admin] admin '{email}' already exists — leaving it "
                "untouched.",
                flush=True,
            )
            return 0

        admin = User(email=email, name='Administrator', role='admin', is_active=True)
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        print(f"[seed_first_admin] created admin '{email}'.", flush=True)
        return 0


if __name__ == '__main__':
    sys.exit(main())
