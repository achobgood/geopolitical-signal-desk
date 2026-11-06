"""Source-bound descriptive risk briefing, independent of equity API credentials."""
from __future__ import annotations
from datetime import datetime,timezone,timedelta,date
from decimal import Decimal
import hashlib,json
from pathlib import Path
from app.market import snapshot,parse_time,expected_completed_market_session
from app.market_calendar import NY

METHOD='source-bound-market-risk-v1'


def fingerprint(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str).encode()).hexdigest()


def _latest_metrics(market):
    # A reported/manual metric with the same name cannot displace a primary
    # provider receipt in the descriptive market regime.
    approved={
      'vix_daily_close':('Cboe Global Markets','https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'),
      'vix_5session_change_pct':('Cboe Global Markets','https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VIX_History.csv'),
      'vxn_daily_close':('Cboe Global Markets','https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv'),
      'vxn_5session_change_pct':('Cboe Global Markets','https://cdn-api.cboe.com/api/global/us_indices/daily_prices/VXN_History.csv')}
    found={}
    for row in sorted(market.get('observations',[]),key=lambda x:(str(x.get('event_at')or''),str(x.get('collected_at')or''),x.get('id',0)),reverse=True):
        code=row.get('metric_code')
        trusted=code in approved and row.get('publisher')==approved[code][0] and row.get('source_url')==approved[code][1]
        treasury=code in ('us_treasury_2y_yield_pct','us_treasury_10y_yield_pct') and row.get('publisher')=='U.S. Department of the Treasury' and str(row.get('source_url','')).startswith('https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=')
        if code in found or row.get('claim_status')!='primary_document' or not(trusted or treasury) or row.get('metric_value')is None:continue
        found[code]={'code':code,'value':row['metric_value'],'unit':row.get('metric_unit'),'publisher':row['publisher'],'source_url':row['source_url'],'published_at':row['published_at'],'first_collected_at':row['collected_at'],'event_at':row.get('event_at'),'source_observation_id':row['id']}
    return found


def _signal(metric, *, interpretation,quality='MEASURED'):
    return {'kind':quality,'metric':metric.get('code'),'value':str(metric.get('value')),'unit':metric.get('unit'),
      'publisher':metric.get('publisher'),'source_url':metric.get('source_url'),'published_or_available_at':metric.get('published_at'),
      'source_observation_id':metric.get('source_observation_id'),'first_collected_at':metric.get('first_collected_at'),
      'reference_session':str(metric.get('event_at') or '')[:10] or None,'interpretation':interpretation}


def build_brief(market,macro_captures,geopolitical, *, as_of):
    metrics=_latest_metrics(market);expected=str(expected_completed_market_session(as_of));evidence=[];bull=[];bear=[];gaps=[]
    vol={};current=True
    for symbol in ('vix','vxn'):
        close=metrics.get(symbol+'_daily_close');change=metrics.get(symbol+'_5session_change_pct')
        if not close or not change:
            current=False;gaps.append(symbol.upper()+': completed daily source missing');continue
        session=str(close.get('event_at') or '')[:10]
        change_session=str(change.get('event_at')or'')[:10]
        if session!=expected or change_session!=expected:
            current=False;gaps.append(symbol.upper()+': close/change sessions '+session+'/'+change_session+' do not both match expected '+expected)
        vol[symbol]={'close':Decimal(str(close['value'])),'change':Decimal(str(change['value'])),'session':session}
        evidence.extend([_signal(close,interpretation='Official volatility-index daily close; not an equity quote'),
          _signal(change,interpretation='Measured five-source-session volatility change; correlated with the other volatility signal, not independent corroboration')])
    elevated=any(x['close']>=threshold for key,x in vol.items() for threshold in [Decimal(25 if key=='vix' else 30)])
    rising=len(vol)==2 and all(x['change']>=20 for x in vol.values())
    easing=len(vol)==2 and all(x['change']<0 for x in vol.values())
    if not current or len(vol)!=2:
        regime='INSUFFICIENTLY_OBSERVED';trajectory='INCOMPLETE_OR_STALE_VOLATILITY_CONFIRMATION'
        headline='Current market regime is insufficiently observed; inspect the dated evidence and missing inputs.'
    elif rising and elevated:
        regime='RISK_OFF';trajectory='DETERIORATING_IN_LAST_COMPLETED_SESSION'
        headline='Risk-off backdrop: volatility stress has intensified; a durable equity rebound is not established.'
        bear.append('Both sourced volatility indices rose at least 20% over five reported sessions, with elevated closing levels.')
    elif easing:
        regime='MIXED';trajectory='STABILIZING_VOLATILITY'
        headline='Volatility is easing, but broad equity participation and financing repair still need confirmation.'
        bull.append('Both sourced volatility indices eased over five reported sessions; this is conditional stabilization evidence.')
    else:
        regime='MIXED';trajectory='MIXED_VOLATILITY_EVIDENCE'
        headline='Market-risk evidence is mixed; volatility alone does not establish equity direction.'
    for code in ('us_treasury_2y_yield_pct','us_treasury_10y_yield_pct'):
        if code in metrics:evidence.append(_signal(metrics[code],interpretation='Dated government par yield; a level alone cannot establish issuer financing access or monetary liquidity.'))
        else:gaps.append(code+': not collected')
    macros=[]
    for record in macro_captures:
        p=record['payload'];provider=record['provider'];data=p.get('data',{});item={k:v for k,v in p.items() if k not in ('history','raw_gzip_base64')};item['capture_id']=record['id'];macros.append(item)
        if provider=='bls_employment':
            gain=Decimal(str(data['payroll_change_jobs']));year,month=map(int,p['reference_period'].split('-'));month_age=(as_of.year-year)*12+as_of.month-month
            item['reference_age_months']=month_age
            if month_age>2:gaps.append('BLS employment reference period '+p['reference_period']+' is too old for a current labor assessment')
            elif gain>0:bull.append('Official payroll levels increased in '+p['reference_period']+'; this supports resilience during that reference period, not an intraday equity direction claim.')
        if provider=='ofr_fsi':
            item['comparison_caveat']='OFR reference '+p['source_as_of']+' must not be used as confirmation of conditions after that date; compare to the equity/volatility session.'
        if provider=='nyfed_sofr' and (as_of.date()-date.fromisoformat(p['source_as_of'])).days>4:gaps.append('SOFR effective date is more than four calendar days old')
        if provider=='ofr_fsi' and (as_of.date()-date.fromisoformat(p['source_as_of'])).days>7:gaps.append('OFR aggregate reference date is more than seven calendar days old')
        if provider=='nyfed_sofr':item['comparison_caveat']='SOFR is a secured overnight reference rate, not an AI issuer spread; its level alone does not diagnose funding stress.'
    present={r['provider'] for r in macro_captures}
    for provider in ('bls_employment','nyfed_sofr','ofr_fsi'):
        if provider not in present:gaps.append(provider+': no verified source capture')
    financing=[];unverified=[];reported=[]
    for o in market.get('observations',[]):
        if o.get('family') not in ('ai_credit','ai_capex_returns','policy_siting','earnings_demand'):continue
        item={'observation_id':o['id'],'title':o['source_title'],'summary':o['summary'],'source_url':o['source_url'],
          'publisher':o['publisher'],'published_at':o['published_at'],'first_collected_at':o['collected_at'],
          'usable_at':max(o['published_at'],o['collected_at']),'event_at':o.get('event_at'),
          'origin_group':o['origin_group'],'claim_status':o['claim_status'],'pressure':o['pressure'],
          'affected_tickers':o.get('tickers',[]),'limitations':o['caveats'],
          'freshness_class':'RECENT_REPORTED_DEVELOPMENT'if as_of-parse_time(o['published_at'])<=timedelta(days=3)else'HISTORICAL_CONTEXT_NOT_CURRENT_QUOTE',
          'age_days_since_publication':round((as_of-parse_time(o['published_at'])).total_seconds()/86400,2)}
        if o.get('claim_status') in ('unverified','disputed'):unverified.append(item)
        else:reported.append(item)
        if o['family']=='ai_credit':financing.append(item)
    financing=financing[:8];reported=reported[:10];unverified=unverified[:8]
    if financing:bear.append('Retained AI-financing reports identify borrower/project risks. Each report retains its date and lineage; they do not establish a universal credit freeze.')
    if any(o.get('phase')=='counterevidence' and o.get('family')=='ai_credit' for o in market.get('observations',[])):
        bull.append('Retained financing counterevidence describes continuing capital-market access for some issuers; its age must be checked.')
    gaps+=['Consolidated intraday equity quotes and advance/decline breadth are not connected.',
      'Live broad credit spreads, issuer CDS and a comprehensive earnings/consensus calendar are not connected.',
      'No private portfolio is included in this general market briefing.']
    if geopolitical:bear.append('Recorded geopolitical market hypotheses include energy/shipping and regional risks; inspect original public sources and alternatives before treating them as confirmed disruptions.')
    if not bull:bull.append('A rebound remains possible, but no sufficient positive confirmation is established by these inputs.')
    window='MORNING' if 7<=as_of.astimezone(NY).hour<12 else 'SESSION' if 12<=as_of.astimezone(NY).hour<17 else 'POST_CLOSE' if as_of.astimezone(NY).hour>=17 else 'OVERNIGHT'
    code_hash=hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    source_signature=fingerprint({'method':METHOD,'code_hash':code_hash,'brief_window':window,'observations':[(x['id'],x.get('source_reviewed_at')) for x in market.get('observations',[])],
       'macro_captures':[x['id'] for x in macro_captures],'geopolitical_assessments':[x['assessment_id'] for x in geopolitical],
       'reference_session':expected,'freshness_gaps':sorted(gaps)})
    return {'report_type':'MARKET_RISK_INTELLIGENCE_NOT_FORECAST','method_version':METHOD,'method_code_hash':code_hash,'source_signature':source_signature,
      'as_of':as_of.isoformat(),'brief_window':window,'reference_equity_session':expected,'market_regime':regime,'trajectory':trajectory,'headline':headline,
      'method':'Transparent descriptive rule using actual completed-session VIX/VXN levels and five-session changes; not a calibrated timing or return model.',
      'intraday_confirmation':'NOT_ESTABLISHED','evidence':evidence,'macro_evidence':macros,'bullish_evidence':bull,
      'bearish_evidence':bear,'financing_signals':financing,'reported_developments':reported,'unverified_leads':unverified,
      'geopolitical_watch':geopolitical,
      'invalidation_conditions':{'improvement':['Broad/equal-weight participation improves alongside technology and prices hold their lows.',
       'Volatility eases while primary market breadth and financing access stabilize.','Credible contractual/counterparty evidence resolves an unverified financing allegation.'],
       'deterioration':['New broad-market lows coincide with further volatility stress.','Documented financing failures or project cancellations expand across counterparties.',
       'Confirmed energy/shipping disruption changes inflation and financing conditions.']},
      'upcoming_events_status':'Calendar coverage is incomplete; official release times and results must be checked separately.',
      'missing_inputs':gaps,'probability':None,'predictive_edge_established':False,'execution_enabled':False,
      'publication_warning':'Some daily source publication clocks are unknown; stored collection/usable timestamps are conservative availability, not claimed earlier publication.',
      'portfolio_boundary':'General public-source research only. Actual portfolio amounts and personalized analysis require separately authorized owner-private input.'}


async def _geopolitical_market_context(store,now):
    assessments=await store.rows('assessments',limit=60,filters={'created_at':'gte.'+(now-timedelta(hours=72)).isoformat()})
    result=[];groups=set()
    for a in assessments:
        analysis=a.get('analysis') or {}
        if not a.get('market_hypothesis') or analysis.get('assessment_origin')=='automated_triage':continue
        group=a.get('cluster_id') or ('lead:'+str(a.get('lead_id')))
        if group in groups:continue
        refs=[{'url':e['source_url'],'limitations':e.get('limitations')} for e in analysis.get('evidence',[]) if e.get('source_url')]
        result.append({'assessment_id':a['id'],'recorded_at':a['created_at'],'as_of':analysis.get('as_of') or a['created_at'],
          'scenario':a['scenario'],'market_hypothesis':a['market_hypothesis'],'counterevidence':a.get('counterevidence'),
          'falsification':analysis.get('falsification_criteria'),'historical_backfill':bool(analysis.get('historical_backfill')),
          'status':'RECORDED_ANALYST_HYPOTHESIS_NOT_INDEPENDENT_CONFIRMATION','sources':refs[:4]})
        groups.add(group)
        if len(result)>=5:break
    return result


async def generate_risk_brief(store, *, refresh_sources=True,force=False):
    now=datetime.now(timezone.utc);source_results=[]
    if refresh_sources:
        from app.market_macro import refresh_macro
        source_results=await refresh_macro(store,force=force)
    now=datetime.now(timezone.utc)
    market=await snapshot(store,as_of=now,mode='live')
    captures=await store.rows('market_macro_captures',limit=15,filters={'select':'id,provider,captured_at,payload'})
    seen=set();latest=[]
    for c in captures:
        if c['provider'] not in seen:latest.append(c);seen.add(c['provider'])
    geo=await _geopolitical_market_context(store,now)
    report=build_brief(market,latest,geo,as_of=datetime.now(timezone.utc))
    health=await store.rpc('market_macro_health_v1',{})
    report['monitoring_checks']=health
    failed=[x['provider'] for x in health if x['status']=='UNAVAILABLE']
    report['missing_inputs'] += [x+': latest retrieval failed; any retained values remain explicitly dated' for x in failed]
    report['source_signature']=fingerprint({'evidence':report['source_signature'],'retrieval_health':[(x['provider'],x['status'])for x in health]})
    receipt=await store.rpc('market_record_risk_brief_v1',{'p':report})
    return {'status':'SOURCE_BOUND_RESEARCH','brief_id':receipt['id'],'recorded_at':receipt['generated_at'],
      'brief':receipt['payload'],'source_refresh':source_results,'execution_enabled':False}
