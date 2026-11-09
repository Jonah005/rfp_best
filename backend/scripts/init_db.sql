-- Runs once at container init, as the superuser.
CREATE EXTENSION IF NOT EXISTS vector;
CREATE EXTENSION IF NOT EXISTS citext;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- The application connects as a NON-superuser role without BYPASSRLS, so that
-- FORCE ROW LEVEL SECURITY actually constrains it. Migrations run as the owner.
DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_app') THEN
    CREATE ROLE bidready_app LOGIN PASSWORD 'bidready'
      NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS;
  END IF;
  -- Owns exactly one SECURITY DEFINER function: the pre-tenant login lookup.
  -- Cannot log in. Holds BYPASSRLS so that function can resolve an email to a
  -- tenant before any tenant context exists.
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'bidready_auth') THEN
    CREATE ROLE bidready_auth NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE BYPASSRLS;
  END IF;
END $$;

GRANT CONNECT ON DATABASE bidready TO bidready_app;
GRANT USAGE ON SCHEMA public TO bidready_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
  GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO bidready_app;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO bidready_app;
