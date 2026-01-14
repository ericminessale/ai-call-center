-- Seed admin user for frontend
-- Default credentials: admin@callcenter.com / Admin123!

-- Connect to the callcenter database
\c callcenter;

-- Create admin user with bcrypt hashed password
-- Password: Admin123! (bcrypt hash)
INSERT INTO users (email, password_hash, name, role, is_active, created_at)
VALUES (
    'admin@callcenter.com',
    '$2b$12$LQv3c1yqBWVHxkd0LHAkCOYz6TtxMQJqhN8/LewY.hsF8U6FOvaPi',  -- Admin123!
    'System Administrator',
    'admin',
    true,
    CURRENT_TIMESTAMP
) ON CONFLICT (email) DO UPDATE
SET
    password_hash = EXCLUDED.password_hash,
    name = EXCLUDED.name,
    role = EXCLUDED.role,
    is_active = EXCLUDED.is_active;

-- Also create a regular agent user
INSERT INTO users (email, password_hash, name, role, is_active, created_at)
VALUES (
    'agent@callcenter.com',
    '$2b$12$4g7zkPzbPc.M0K6V7WYAVOkBYZJpM0PglU9rBxPLlZQp8h6clImGS',  -- Agent123!
    'Call Center Agent',
    'agent',
    true,
    CURRENT_TIMESTAMP
) ON CONFLICT (email) DO UPDATE
SET
    password_hash = EXCLUDED.password_hash,
    name = EXCLUDED.name,
    role = EXCLUDED.role,
    is_active = EXCLUDED.is_active;

-- Confirm users were created
SELECT email, name, role, is_active FROM users;