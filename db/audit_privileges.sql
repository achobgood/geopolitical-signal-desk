-- Supabase's default privileges granted service_role ALL on the new table.
-- Narrow that inherited creation-time grant to append/read only.
revoke all on public.audit_events from service_role;
grant select,insert on public.audit_events to service_role;
