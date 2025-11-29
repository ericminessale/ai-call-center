-- Fix users table to use VARCHAR for id instead of INTEGER
-- This matches what the User model expects

-- First, drop the existing users table and recreate it with correct schema
DROP TABLE IF EXISTS users CASCADE;

CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) UNIQUE NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    is_active BOOLEAN DEFAULT true,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Create an index on email for faster lookups
CREATE INDEX idx_users_email ON users(email);

-- Also ensure the calls table uses VARCHAR for user_id if it exists
ALTER TABLE calls
    DROP COLUMN IF EXISTS user_id CASCADE;

ALTER TABLE calls
    ADD COLUMN user_id VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL;