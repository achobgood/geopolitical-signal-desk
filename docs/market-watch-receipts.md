# Market Watch: real source-linked confirmations, not predictions

The independent database evaluator is in db/market_watch_engine_v1.sql.
It was already applied to the existing Supabase project as the migration
market_descriptive_watch_engine_v1.

The first sourced Cboe VXN daily five-session move was +51.95% as of
November 5, after the stock-market decline. The evaluator detected a
vxn_5day_spike and linked it to the original stored Cboe observation.
This was an AFTER-THE-FACT volatility confirmation, never a pre-crash alert.
Both the source first-collected time and the actual watch-detected time
are stored, and history/replay must not show the watch before either.

## Provisional attention thresholds (not predictive models)

- VXN five-session volatility increase >=20%.
- VIX five-session volatility increase >=20%.
- Treasury 10-year par yield five-session increase >=30 basis points.
- VXN or VIX five-session volatility decrease <=-20% is a cooling observation,
  not proof of a rebound or a bottom.

The sources must be official and have the latest expected completed U.S.
trading session. Stale or missing sources create explicit gaps. Thresholds
are uncalibrated and have no measured false-positive or forecast performance.

Signal identity is deduplicated by source observation ID and rule ID.
The evaluator never accesses portfolios, cash, secret keys or trade execution.
Public market research can display these watches, but no brokerage order
or personalized portfolio output is published.

## Scheduling and proof

The existing geopolitical cron stays unchanged. Official market reference
collection is configured for minutes :15 at UTC hours 0, 13 and 23.
The independent source-evidence evaluation runs at :25 and :50 of the same
hours. Actual automated completion must be proven from cron.job_run_details
and from the private market_watch_checks database receipts, not inferred from
registered jobs. The first evaluation was manual and is labeled manual.

The public watch UI shows source URL, original session, source-collected time,
watch-detected time, value, threshold and the limitation text. It must show
NO RECORDED SIGNAL rather than NO MARKET RISK if there are no matches.

## Still missing for trade-ready capability

- Live entitled SIP exchange quote and issuer credit/CDS sources
- Earnings and broad market-breadth coverage
- Portfolio account data under private authenticated access
- Calibrated historical returns and false-positive evaluation
- Outbound notifications and independently tested delivery receipts
- Any brokerage execution (deliberately disabled)

No trade can be justified solely by a VIX/VXN threshold or from press reports.
