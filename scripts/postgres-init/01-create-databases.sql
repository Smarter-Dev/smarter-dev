-- Bootstrap the single application database (mirrors prod):
--   smarter_dev -> app DB; Skrift core + app tables in the `skrift` schema
--
-- The `smarter_dev` database itself is created by POSTGRES_DB; we only need
-- to create the skrift schema and search_path.
\connect smarter_dev
CREATE SCHEMA IF NOT EXISTS skrift;
GRANT ALL ON SCHEMA skrift TO smarter_dev;
ALTER ROLE smarter_dev IN DATABASE smarter_dev SET search_path TO skrift, public;
