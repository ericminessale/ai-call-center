-- Database initialization script
-- This creates the database, user, and initial schema

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

-- Create users table first (required by calls foreign key)
CREATE TABLE IF NOT EXISTS users (
    id SERIAL PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    name VARCHAR(255),
    role VARCHAR(50) DEFAULT 'agent',
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create tables for the call center
CREATE TABLE IF NOT EXISTS agents (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    email VARCHAR(255) UNIQUE NOT NULL,
    status VARCHAR(50) DEFAULT 'offline',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS queues (
    id SERIAL PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    description TEXT,
    priority INTEGER DEFAULT 5,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS calls (
    id SERIAL PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    signalwire_call_sid VARCHAR(255) UNIQUE,
    destination VARCHAR(255) NOT NULL,
    destination_type VARCHAR(20) NOT NULL,
    status VARCHAR(50) DEFAULT 'initiated',
    transcription_active BOOLEAN NOT NULL DEFAULT false,
    recording_url TEXT,
    summary TEXT,
    duration INTEGER,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    answered_at TIMESTAMP,
    ended_at TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_calls_signalwire_call_sid ON calls(signalwire_call_sid);

CREATE TABLE IF NOT EXISTS transcriptions (
    id SERIAL PRIMARY KEY,
    call_id INTEGER NOT NULL REFERENCES calls(id) ON DELETE CASCADE,
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

CREATE TABLE IF NOT EXISTS webhook_events (
    id SERIAL PRIMARY KEY,
    call_id INTEGER REFERENCES calls(id) ON DELETE SET NULL,
    event_type VARCHAR(100) NOT NULL,
    payload JSON NOT NULL,
    processed BOOLEAN DEFAULT false,
    error_message TEXT,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Grant permissions on tables
GRANT ALL ON ALL TABLES IN SCHEMA public TO ccuser;
GRANT ALL ON ALL SEQUENCES IN SCHEMA public TO ccuser;

-- Insert default queues
INSERT INTO queues (name, description, priority) VALUES
    ('sales', 'Sales inquiry queue', 5),
    ('support', 'Technical support queue', 5)
ON CONFLICT DO NOTHING;

-- Insert default admin user
-- Password: Admin123!
INSERT INTO users (email, password_hash, name, role, is_active)
VALUES (
    'admin@callcenter.com',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY.hsF8U6FOvaPi',
    'System Administrator',
    'admin',
    true
) ON CONFLICT (email) DO NOTHING;

-- Insert default agent user
-- Password: Agent123!
INSERT INTO users (email, password_hash, name, role, is_active)
VALUES (
    'agent@callcenter.com',
    '$2b$12$4g7zkPzbPc.M0K6V7WYAVOkBYZJpM0PglU9rBxPLlZQp8h6clImGS',
    'Call Center Agent',
    'agent',
    true
) ON CONFLICT (email) DO NOTHING;