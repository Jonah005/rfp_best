-- Authentication happens BEFORE a tenant is known, but RLS is FORCEd on
-- app_user, so an ordinary query at login time correctly returns nothing.
--
-- The escape hatch is deliberately one function with a fixed, minimal
-- projection: given an email, return only the identity fields the login path
-- needs. It is owned by a dedicated NOLOGIN role that may bypass RLS, and
-- EXECUTE is granted only to the application role. The application role still
-- cannot read app_user across tenants by any other route.
CREATE OR REPLACE FUNCTION auth_lookup_user(p_email text)
RETURNS TABLE (
  user_id       uuid,
  org_id        uuid,
  password_hash text,
  is_active     boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = public, pg_temp
AS $$
  SELECT u.id, u.org_id, u.password_hash, u.is_active
  FROM app_user u
  WHERE lower(u.email) = lower(p_email)
  LIMIT 1;
$$;

REVOKE ALL ON FUNCTION auth_lookup_user(text) FROM PUBLIC;
