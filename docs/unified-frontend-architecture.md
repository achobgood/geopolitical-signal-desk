# Signal Desk: one product, one frontend, modular backends

## Decision: consolidate UX rather than preserve two websites

**Current technical facts**

- The original user-visible geopolitical command center is hosted on ChatGPT Sites.
- Its API is already hosted on the user's existing Render service.
- The persistent database is the existing Supabase project.
- The new Market Intelligence prototype UI was served directly at Render's /market route because it could be deployed and verified through the existing GitHub-to-Render workflow. That was a **deployment convenience**, not a deliberate architectural target.
- The Trade Synthesis APIs are being built in the same backend. There is NO live brokerage integration or current portfolio sync and NO verified real-time bid/ask or AI-issuer credit pricing feed.

**Recommended target architecture**

A SINGLE authenticated, responsive web application (ideally on a project-owned domain),
with a consistent layout, navigation, account-level permissions, public research vs
private trade views, operational health and audit tools. It can run on the existing
Render service, which already hosts the API, without buying another deployment stack.
The original ChatGPT Site should be treated as a temporary client or redirect during
migration, not maintained indefinitely as a second independent frontend.

Suggested structure:

    /                      Global risk posture and critical alerts
    /geopolitical           Intelligence alerts, leads, event clusters, investigations
    /markets                AI financing, official market data, market risk evidence
    /markets/replay         Historical research replays with no lookahead bias
    /trades                 PRIVATE owner-only actionable research tickets
    /trades/history         PRIVATE recommendation history and performance evaluation
    /operations             Collector health, sources, failures, scheduling

Backend API remains versioned separately under /v1 and retains isolated
geopolitical and market data models. Existing geopolitical watchers, schedulers,
database rows and authorizations must remain intact.

## Why not just embed every capability into ChatGPT Sites?

A ChatGPT Site can remain an excellent research presentation and public dashboard,
but the planned Trade Synthesis experience needs authenticated private portfolio
positions, signed-in owner-level actions, live licensing considerations, quote
staleness and asynchronous collection/alert services. This calls for a product
security model and operational UI intentionally designed around those flows.

Do not assume Sites cannot do the required work without checking current
capabilities. It remains a viable frontend candidate if it can support:
owner-only account context, authenticated server data access, private views,
real-time updates, safe action confirmation, component navigation and reliable
deployment/versioning. If its editor/deployment and permission model passes those
tests, preserving Sites as the single frontend would also be valid.

The technical priority is **one supported user experience**, not the brand of
frontend hosting. If adopting a dedicated web app, migrate the geopolitical view
before calling the interface consolidation complete. A market-only web frontend
plus a geopolitical ChatGPT Site is a transitional state, not the target.

## Security and deployment requirements before private Trade UI

1. Use real server-validated owner sessions, not a bearer token pasted into
   JavaScript, stored in localStorage or exposed in HTML.
2. Separate public market research from protected account data and trade plans.
   Do not put user balances or positions in public market research snapshots.
3. Keep the service-role Supabase credential only server-side. Audit every
   owner action; enforce least privilege and back-end risk limits.
4. Protect mutations from CSRF, injection and excessive requests, enforce CORS,
   and verify all user-facing pages render without leaking private API schemas.
5. Prevent unsupported trading execution or silent broker order placement.
   "Approval requested" and "executed" must remain distinct auditable states.
6. Test on a preview deployment and use a reversible cutover, preserving the
   already functional geopolitical Site until parity and security tests pass.
7. Use the existing Render and Supabase resources wherever practical;
   add recurring jobs only with explicit coverage needs and cost review.

## Cutover acceptance tests

- Existing geopolitical investigations/alerts, source traces and scheduler
  remain accessible at least as well as in the ChatGPT Site.
- Market risk timeline, source caveats, official feeds and replay work with
  coherent top navigation and visual language.
- Private trade route requires an authenticated owner and cannot be opened
  by unauthenticated or general reader users.
- End-to-end quote/portfolio freshness guard causes NO_TRADE when feeds fail.
- Both source data and buy/sell proposals show exact evidence and valid
  as-of timestamps; no user account data enters public route responses.
- All independent system health checks pass; URLs and bookmarks redirect
  without losing deep links.
- No duplicate live schedulers or duplicate paid infrastructure are created.

## Next engineering phase

Implement a minimal unified web app shell with consistent styling, navigation,
and secure owner sign-in. Port the existing geopolitical interface first;
then integrate the market and private trade pages. Do NOT retire the existing
ChatGPT Site or its permissions until all parity tests pass. Keep code in
the existing geopolitical-signal-desk repository rather than beginning another
untracked project.
