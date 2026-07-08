-- Database initialization script
-- This creates the database, user, and the COMPLETE schema
-- (includes all Alembic migration changes so `down -v` produces a working DB)

-- Create user only if it doesn't exist
DO
$$
BEGIN
  CREATE USER ccuser WITH PASSWORD 'changeme';
  EXCEPTION WHEN duplicate_object THEN
  RAISE NOTICE 'User ccuser already exists, skipping';
END
$$;

-- Create database only if it doesn't exist
SELECT 'CREATE DATABASE callcenter OWNER ccuser'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'callcenter')\gexec

-- Connect to the callcenter database
\c callcenter;

-- Grant all privileges
GRANT ALL PRIVILEGES ON DATABASE callcenter TO ccuser;
GRANT ALL ON SCHEMA public TO ccuser;

-- Enable extensions for RAG/vector search
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ============================================================
-- Users table (includes Call Fabric subscriber fields)
-- ============================================================
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50) NOT NULL DEFAULT 'agent',
    is_active BOOLEAN NOT NULL DEFAULT true,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    -- Call Fabric subscriber fields
    signalwire_subscriber_id VARCHAR(100),
    signalwire_username VARCHAR(100),
    signalwire_password_encrypted VARCHAR(500),
    signalwire_address VARCHAR(255),
    fabric_subscriber_created_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_users_email ON users(email);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_signalwire_subscriber_id ON users(signalwire_subscriber_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_signalwire_username ON users(signalwire_username);

-- ============================================================
-- Contacts table
-- ============================================================
CREATE TABLE IF NOT EXISTS contacts (
    id SERIAL PRIMARY KEY,
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    display_name VARCHAR(200),
    phone VARCHAR(20) NOT NULL,
    email VARCHAR(255),
    avatar_url VARCHAR(500),
    company VARCHAR(200),
    job_title VARCHAR(100),
    account_tier VARCHAR(20),
    account_status VARCHAR(20),
    external_id VARCHAR(100),
    is_vip BOOLEAN NOT NULL DEFAULT false,
    is_blocked BOOLEAN NOT NULL DEFAULT false,
    tags TEXT,
    notes TEXT,
    custom_fields TEXT,
    total_calls INTEGER NOT NULL DEFAULT 0,
    last_interaction_at TIMESTAMP,
    average_sentiment FLOAT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_contacts_phone ON contacts(phone);
CREATE INDEX IF NOT EXISTS ix_contacts_email ON contacts(email);
CREATE INDEX IF NOT EXISTS ix_contacts_external_id ON contacts(external_id);

-- ============================================================
-- Calls table (includes contact, queue tracking, conference fields)
-- ============================================================
CREATE TABLE IF NOT EXISTS calls (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    signalwire_call_sid VARCHAR(255),
    from_number VARCHAR(255),
    destination VARCHAR(255) NOT NULL,
    destination_type VARCHAR(20) NOT NULL,
    status VARCHAR(50) DEFAULT 'initiated',
    transcription_active BOOLEAN NOT NULL DEFAULT false,
    recording_url TEXT,
    summary TEXT,
    duration INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at TIMESTAMP,
    ended_at TIMESTAMP,
    -- Contact fields
    contact_id INTEGER REFERENCES contacts(id),
    direction VARCHAR(10),
    handler_type VARCHAR(10),
    ai_agent_name VARCHAR(100),
    sentiment_score FLOAT,
    ai_context TEXT,
    -- Queue tracking fields
    queue_id VARCHAR(50),
    assigned_agent_id INTEGER REFERENCES users(id),
    assigned_at TIMESTAMP,
    conference_name VARCHAR(255)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_calls_signalwire_call_sid ON calls(signalwire_call_sid);
CREATE INDEX IF NOT EXISTS ix_calls_contact_id ON calls(contact_id);
CREATE INDEX IF NOT EXISTS ix_calls_queue_id ON calls(queue_id);

-- ============================================================
-- Transcriptions table
-- ============================================================
CREATE TABLE IF NOT EXISTS transcriptions (
    id SERIAL PRIMARY KEY,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    transcript TEXT,
    summary TEXT,
    confidence FLOAT,
    is_final BOOLEAN DEFAULT false,
    sequence_number INTEGER,
    speaker VARCHAR(50),
    language VARCHAR(10) DEFAULT 'en-US',
    keywords JSON,
    sentiment VARCHAR(20),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Webhook events table
-- ============================================================
CREATE TABLE IF NOT EXISTS webhook_events (
    id SERIAL PRIMARY KEY,
    call_id INTEGER REFERENCES calls(id),
    event_type VARCHAR(100) NOT NULL,
    payload JSON NOT NULL,
    processed BOOLEAN DEFAULT false,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Conferences tables
-- ============================================================
CREATE TABLE IF NOT EXISTS conferences (
    id SERIAL PRIMARY KEY,
    conference_name VARCHAR(255) NOT NULL,
    conference_type VARCHAR(50) NOT NULL,
    owner_user_id INTEGER REFERENCES users(id),
    owner_ai_agent VARCHAR(100),
    queue_id VARCHAR(50),
    status VARCHAR(50),
    created_at TIMESTAMP,
    ended_at TIMESTAMP,
    call_id VARCHAR(255)
);

CREATE UNIQUE INDEX IF NOT EXISTS ix_conferences_conference_name ON conferences(conference_name);
CREATE INDEX IF NOT EXISTS ix_conferences_call_id ON conferences(call_id);

CREATE TABLE IF NOT EXISTS conference_participants (
    id SERIAL PRIMARY KEY,
    conference_id INTEGER NOT NULL REFERENCES conferences(id),
    call_id INTEGER REFERENCES calls(id),
    participant_type VARCHAR(50) NOT NULL,
    participant_id VARCHAR(255) NOT NULL,
    call_sid VARCHAR(255),
    direction VARCHAR(20),
    status VARCHAR(50),
    joined_at TIMESTAMP,
    left_at TIMESTAMP,
    duration INTEGER,
    is_muted BOOLEAN,
    is_deaf BOOLEAN
);

CREATE INDEX IF NOT EXISTS ix_conference_participants_conference_id ON conference_participants(conference_id);

-- ============================================================
-- Call legs table
-- ============================================================
CREATE TABLE IF NOT EXISTS call_legs (
    id SERIAL PRIMARY KEY,
    call_id INTEGER NOT NULL REFERENCES calls(id),
    user_id INTEGER REFERENCES users(id),
    leg_type VARCHAR(50) NOT NULL,
    leg_number INTEGER,
    ai_agent_name VARCHAR(100),
    status VARCHAR(50),
    started_at TIMESTAMP,
    ended_at TIMESTAMP,
    duration INTEGER,
    transition_reason VARCHAR(100),
    summary TEXT,
    conference_id INTEGER REFERENCES conferences(id),
    conference_name VARCHAR(255)
);

CREATE INDEX IF NOT EXISTS ix_call_legs_call_id ON call_legs(call_id);

-- ============================================================
-- Admin: System configuration (key-value store)
-- ============================================================
CREATE TABLE IF NOT EXISTS system_config (
    key VARCHAR(100) PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_by INTEGER REFERENCES users(id)
);

-- ============================================================
-- Admin: Document collections for RAG knowledge bases
-- ============================================================
CREATE TABLE IF NOT EXISTS document_collections (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) UNIQUE NOT NULL,
    display_name VARCHAR(200) NOT NULL,
    description TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Admin: Documents within collections
-- ============================================================
CREATE TABLE IF NOT EXISTS documents (
    id SERIAL PRIMARY KEY,
    collection_id INTEGER NOT NULL REFERENCES document_collections(id) ON DELETE CASCADE,
    title VARCHAR(300) NOT NULL,
    content TEXT NOT NULL,
    is_published BOOLEAN DEFAULT false,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- ============================================================
-- Admin: Agent-to-collection assignments
-- ============================================================
CREATE TABLE IF NOT EXISTS agent_collection_assignments (
    id SERIAL PRIMARY KEY,
    agent_id VARCHAR(50) NOT NULL,
    collection_id INTEGER NOT NULL REFERENCES document_collections(id) ON DELETE CASCADE,
    UNIQUE(agent_id, collection_id)
);

-- ============================================================
-- Alembic version tracking (stamp at latest migration)
-- ============================================================
CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(32) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

INSERT INTO alembic_version (version_num) VALUES ('e5f6a7b8c9d0')
ON CONFLICT (version_num) DO NOTHING;

-- ============================================================
-- Grant permissions on all tables
-- ============================================================
GRANT ALL ON ALL TABLES IN SCHEMA public TO ccuser;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ccuser;

-- ============================================================
-- Seed data
-- ============================================================

-- DEPLOY-C3 (2026-07-07 pre-deploy): the default admin/agent users with
-- publicly-known passwords (Admin123! / Agent123!) were REMOVED from the
-- seed. On a public instance those committed credentials let anyone sign in
-- as admin. The first admin is now provisioned from ADMIN_EMAIL/ADMIN_PASSWORD
-- by backend/seed_first_admin.py (run from entrypoint.sh after migrations);
-- if those env vars are unset no admin is created at all. Do NOT reintroduce
-- a hardcoded credential here.

-- Default routing config
INSERT INTO system_config (key, value) VALUES
    ('route.initial_handler', '/receptionist'),
    ('route.sales_specialist', '/sales-ai'),
    ('route.support_specialist', '/support-ai')
ON CONFLICT (key) DO NOTHING;

-- Default document collections
INSERT INTO document_collections (name, display_name, description) VALUES
    ('sales_knowledge', 'Sales Knowledge Base', 'Product info, pricing, sales scripts'),
    ('support_knowledge', 'Support Knowledge Base', 'Troubleshooting guides, FAQs, diagnostics')
ON CONFLICT (name) DO NOTHING;

-- Default agent-collection assignments
INSERT INTO agent_collection_assignments (agent_id, collection_id)
SELECT agent_id, collection_id FROM (VALUES
    ('sales-ai', 1),
    ('outbound-sales', 1),
    ('support-ai', 2),
    ('outbound-support', 2)
) AS defaults(agent_id, collection_id)
WHERE EXISTS (SELECT 1 FROM document_collections WHERE id = collection_id)
ON CONFLICT (agent_id, collection_id) DO NOTHING;
