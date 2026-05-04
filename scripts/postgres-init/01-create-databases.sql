-- Bootstrap two databases that mirror prod:
--   smarter_dev   -> main app DB; new-app + Skrift core in `skrift` schema
--   bc_websites   -> legacy DB; legacy/bot tables in `public`, Skrift core in `skrift`
--
-- The `smarter_dev` database itself is created by POSTGRES_DB; we only need to
-- create the legacy database here.
CREATE DATABASE bc_websites OWNER smarter_dev;

-- skrift schema in main DB
\connect smarter_dev
CREATE SCHEMA IF NOT EXISTS skrift;
GRANT ALL ON SCHEMA skrift TO smarter_dev;
ALTER ROLE smarter_dev IN DATABASE smarter_dev SET search_path TO skrift, public;

-- skrift schema in legacy DB (legacy admin uses Skrift's auth)
\connect bc_websites
CREATE SCHEMA IF NOT EXISTS skrift;
GRANT ALL ON SCHEMA skrift TO smarter_dev;
ALTER ROLE smarter_dev IN DATABASE bc_websites SET search_path TO public, skrift;
