import asyncio
from datetime import datetime, timezone
from app.market_risk import build_brief, session_comparisons, _geopolitical_market_context
from test_market_macro_risk import observed

NOW=datetime(2026,11,7,16,tzinfo=timezone.utc)

def observations():
    return [observed('vix_daily_close','25.64',session='2026-11-06',id=60),
      observed('vxn_daily_close','34.02',session='2026-11-06',id=50),
      observed('vix_daily_close','28.18',id=34),observed('vxn_daily_close','38.26',id=18),
      observed('vix_5session_change_pct','32.03',session='2026-11-06',id=61),
      observed('vxn_5session_change_pct','19.37',session='2026-11-06',id=51)]

def test_daily_relief_and_elevated_five_session_stress_are_both_explained():
    report=build_brief({'observations':observations()},[],[],as_of=NOW)
    assert report['market_regime']=='MIXED'
    assert report['trajectory']=='LAST_SESSION_VOLATILITY_EASING_STRESS_ELEVATED'
    vix=report['session_comparisons'][0]
    assert vix['change_pct']=='-9.01'
    assert vix['current']['source_observation_id']==60
    assert vix['baseline']['source_observation_id']==34
    assert any('declined' in x for x in report['bullish_evidence'])
    assert any('remains elevated' in x for x in report['bearish_evidence'])
    assert report['probability'] is None

def test_missing_exact_adjacent_session_does_not_relabel_an_older_change_as_daily():
    rows=observations();rows[2]['event_at']='2026-11-04T00:00:00Z'
    comparison=session_comparisons({'observations':rows},expected='2026-11-06')[0]
    assert comparison['status']=='MISSING_ADJACENT_SESSION'
    assert comparison['baseline'] is None and 'change_pct' not in comparison
    report=build_brief({'observations':rows},[],[],as_of=NOW)
    assert report['trajectory']=='MIXED_VOLATILITY_EVIDENCE'

def test_weekend_baseline_and_unverified_same_named_metrics():
    rows=[observed('vix_daily_close','25',session='2026-11-09',id=1),
      observed('vix_daily_close','26',session='2026-11-06',id=2),
      observed('vix_daily_close','1',session='2026-11-09',id=3,status='unverified')]
    comparison=session_comparisons({'observations':rows},expected='2026-11-09')[0]
    assert comparison['baseline_session']=='2026-11-06'
    assert comparison['current']['value']=='25'

def test_treasury_changes_are_basis_points_and_never_issuer_credit_spreads():
    rows=[]
    for id,day,value in [(1,'2026-11-05','4.46'),(2,'2026-11-06','4.51')]:
        row=observed('us_treasury_2y_yield_pct',value,session=day,id=id,publisher='U.S. Department of the Treasury')
        row.update(source_url='https://home.treasury.gov/resource-center/data-chart-center/interest-rates/pages/xml?data=daily_treasury_yield_curve&field_tdr_date_value_month=202611',metric_unit='percent annual yield')
        rows.append(row)
    comparison=session_comparisons({'observations':rows},expected='2026-11-06')[2]
    assert comparison['change_bp']=='5.00'
    assert 'not an issuer spread' in comparison['interpretation']

def test_database_filters_precede_limit_and_competing_review_hypotheses_survive():
    class Store:
        async def rows(self,table,**kwargs):
            assert table=='assessments'
            assert kwargs['filters']['market_hypothesis']=='neq.'
            assert kwargs['filters']['analysis->>assessment_origin']=='neq.automated_triage'
            return [dict(id=n,created_at='2026-11-07T15:00:00Z',cluster_id=8,
              market_hypothesis='Conditional energy-risk interpretation '+str(n),scenario='Scenario '+str(n),
              analysis={'assessment_origin':'agent_analysis','review_key':'same-version',
                'evidence':[{'source_url':'https://example.org/official','origin_group':'one-origin',
                  'limitations':'No effects verified','reviewed_snapshot':{'retained_observation':'Statement announced','publication_time_note':'Nov 7'}}]})for n in (3,2,1)]
    context=asyncio.run(_geopolitical_market_context(Store(),NOW))
    assert len(context)==1 and len(context[0]['competing_hypotheses'])==2
    assert context[0]['sources'][0]['origin_group']=='one-origin'
    assert context[0]['sources'][0]['retained_observation']=='Statement announced'
    assert context[0]['status']=='RECORDED_ANALYST_HYPOTHESIS_NOT_INDEPENDENT_CONFIRMATION'
