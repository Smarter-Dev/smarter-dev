-- Alembic env.py sets `search_path TO skrift, public` per session.
-- The schema must exist before any migration runs.
CREATE SCHEMA IF NOT EXISTS skrift;
GRANT ALL ON SCHEMA skrift TO smarter_dev;
ALTER ROLE smarter_dev IN DATABASE smarter_dev SET search_path TO skrift, public;
