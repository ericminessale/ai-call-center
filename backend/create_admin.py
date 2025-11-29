#!/usr/bin/env python3
"""Create admin user for the call center"""

from app import create_app, db
from app.models.user import User

app = create_app()

with app.app_context():
    # Delete old admin if exists
    User.query.filter_by(email='admin@callcenter.com').delete()
    db.session.commit()

    # Create new admin with correct password
    admin = User(email='admin@callcenter.com')
    admin.set_password('Admin123!')
    db.session.add(admin)
    db.session.commit()
    print("Admin user created successfully!")

    # Also create agent user
    User.query.filter_by(email='agent@callcenter.com').delete()
    agent = User(email='agent@callcenter.com')
    agent.set_password('Agent123!')
    db.session.add(agent)
    db.session.commit()
    print("Agent user created successfully!")

    # Verify they work
    test_admin = User.query.filter_by(email='admin@callcenter.com').first()
    test_agent = User.query.filter_by(email='agent@callcenter.com').first()

    print("\nUsers created:")
    print(f"  Admin: {test_admin.email} - Password check: {test_admin.check_password('Admin123!')}")
    print(f"  Agent: {test_agent.email} - Password check: {test_agent.check_password('Agent123!')}")