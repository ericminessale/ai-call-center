#!/usr/bin/env python3
"""Create/reset the admin (and optional agent) user for the call center.

DEPLOY-C3 (2026-07-07 pre-deploy): this helper used to hardcode
``Admin123!`` / ``Agent123!`` — publicly-known passwords in a public repo.
It now reads credentials from the environment and refuses to run without
them, so there is no committed default admin password anywhere.

For the normal deploy path you don't need this — entrypoint.sh runs
``seed_first_admin.py`` which idempotently seeds from ADMIN_EMAIL/ADMIN_PASSWORD.
This script remains as a manual "reset the admin password" convenience:

    ADMIN_EMAIL=you@example.com ADMIN_PASSWORD='<strong>' python create_admin.py
"""

import os
import sys

from app import create_app, db
from app.models.user import User


def _require(name: str) -> str:
    val = os.getenv(name)
    if not val:
        print(f"ERROR: {name} must be set (no default is provided).", file=sys.stderr)
        sys.exit(1)
    return val


def main() -> None:
    admin_email = (os.getenv('ADMIN_EMAIL') or 'admin@callcenter.com').strip().lower()
    admin_password = _require('ADMIN_PASSWORD')
    if len(admin_password) < 12:
        print("ERROR: ADMIN_PASSWORD must be at least 12 characters.", file=sys.stderr)
        sys.exit(1)

    app = create_app()
    with app.app_context():
        User.query.filter_by(email=admin_email).delete()
        db.session.commit()

        admin = User(email=admin_email, name='Administrator', role='admin', is_active=True)
        admin.set_password(admin_password)
        db.session.add(admin)
        db.session.commit()
        print(f"Admin user '{admin_email}' created/reset successfully!")

        # Optional agent user — only if AGENT_EMAIL/AGENT_PASSWORD are provided.
        agent_email = os.getenv('AGENT_EMAIL')
        agent_password = os.getenv('AGENT_PASSWORD')
        if agent_email and agent_password:
            agent_email = agent_email.strip().lower()
            User.query.filter_by(email=agent_email).delete()
            agent = User(email=agent_email, name='Call Center Agent', role='agent', is_active=True)
            agent.set_password(agent_password)
            db.session.add(agent)
            db.session.commit()
            print(f"Agent user '{agent_email}' created/reset successfully!")


if __name__ == '__main__':
    main()
