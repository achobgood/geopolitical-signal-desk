-- Expose only public-source check health through a backend-only boundary.
create function private.market_macro_health_v1()returns jsonb language sql stable security definer set search_path='' as $$
 select coalesce(jsonb_agg(to_jsonb(x)),'[]'::jsonb)from(
 select distinct on(c.provider)c.provider,c.id as check_id,c.started_at,r.finished_at,
 coalesce(r.status,'CHECK_IN_PROGRESS')status,r.capture_id
 from private.market_macro_checks c left join private.market_macro_check_results r on r.check_id=c.id
 order by c.provider,c.id desc)x
$$;
revoke all on function private.market_macro_health_v1()from public,anon,authenticated;
grant execute on function private.market_macro_health_v1()to service_role;
create function public.market_macro_health_v1()returns jsonb language sql stable security invoker set search_path='' as $$select private.market_macro_health_v1()$$;
revoke all on function public.market_macro_health_v1()from public,anon,authenticated;
grant execute on function public.market_macro_health_v1()to service_role;
notify pgrst,'reload schema';
