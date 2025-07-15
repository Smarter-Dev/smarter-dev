-- Initialize database extensions and settings
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- Set timezone
SET timezone = 'UTC';

-- Create indexes for performance (will be managed by Alembic migrations later)
-- This file is mainly for any initial database setup that's needed