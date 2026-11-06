"""Owner-entered portfolio arithmetic and explicitly hypothetical stress comparisons.

No account synchronization, broker credentials, orders, inferred positions or
fabricated prices. Unknown look-through remains unknown; no weight normalization.
"""
from __future__ import annotations
from collections import defaultdict
from datetime import datetime,timezone,timedelta
from decimal import Decimal
import math,statistics
from typing import Literal
from pydantic import Field,field_validator,model_validator
from app.models import StrictModel
from app.market_calendar import is_expected_market_session, NY
from app.market_sec import SEC_TICKERS

D=Decimal
ACCOUNT=Literal['BrokerageLink','BrokerageLink Roth','401(k)','Other']

class ManualPosition(StrictModel):
    ticker:str=Field(pattern=r'^[A-Z][A-Z0-9.-]{0,11}$')
    account_alias:ACCOUNT='Other'
    market_value:Decimal=Field(gt=0,le=Decimal('1e11'))
    quantity:Decimal|None=Field(default=None,gt=0,le=Decimal('1e10'))
    asset_type:Literal['EQUITY','BOND','OTHER']
    @field_validator('market_value','quantity')
    @classmethod
    def finite(cls,v):
        if v is not None and not v.is_finite():raise ValueError('Finite portfolio values required')
        return v

class ManualPortfolio(StrictModel):
    as_of:datetime
    source:str=Field(min_length=5,max_length=300)
    currency:Literal['USD']='USD'
    complete:bool
    cash:Decimal=Field(ge=0,le=Decimal('1e11'))
    positions:list[ManualPosition]=Field(max_length=100)
    @field_validator('as_of')
    @classmethod
    def time(cls,v):
        if v.utcoffset() is None or v>datetime.now(timezone.utc)+timedelta(minutes=5):raise ValueError('Dated nonfuture position source required')
        return v
    @field_validator('cash')
    @classmethod
    def finite(cls,v):
        if not v.is_finite():raise ValueError('Finite cash required')
        return v
    @model_validator(mode='after')
    def unique(self):
        keys=[(x.account_alias,x.ticker) for x in self.positions]
        if len(set(keys))!=len(keys):raise ValueError('Combine duplicate ticker entries within each account alias')
        return self

class PortfolioSave(StrictModel):
    record_key:str=Field(min_length=8,max_length=160,pattern=r'^[A-Za-z0-9:_-]+$')
    portfolio:ManualPortfolio

class PortfolioAnalyze(StrictModel):
    deploy_amount:Decimal=Field(default=Decimal('0'),ge=0,le=Decimal('1e11'))
    choices:list[str]=Field(default_factory=lambda:['VOO','MGV','VEU','IXN'],max_length=8)
    initial_fraction:Decimal=Field(default=Decimal('.25'),gt=0,le=1)
    @field_validator('choices')
    @classmethod
    def choices_valid(cls,v):
        if not v or len(set(v))!=len(v) or any(x not in {'VOO','MGV','VEU','IXN','IOO','SPMO','FDVV','SPEU','PPA','SPY','QQQ'} for x in v):raise ValueError('Unsupported or duplicate comparison funds')
        return v


def _norm(s):return ''.join(x for x in str(s).lower() if x.isalnum())
def _money(x):
    value=D(x).quantize(D('.01'));return str(abs(value)if value==0 else value)
def _pct(x,total):return str((D(x)/total*100).quantize(D('.01'))) if total else None


def _identity(row):
    # Exact US ticker links current direct/indirect securities. Other venues and
    # unresolved aliases/share classes stay distinct instead of guessed merging.
    ticker=str(row.get('ticker')or'').upper();venue=_norm(row.get('exchange',''));currency=row.get('currency') or row.get('market_currency')
    if row.get('asset_class')=='Equity' and currency=='USD' and venue in {'nasdaq','newyorkstockexchangeinc','newyorkstockexchange','nyse','nysearca','cboebzxexchange','nysearcaexch'} and ticker.isalpha():return 'US_TICKER:'+ticker
    return str(row['security_id'])


def exposures(portfolio:ManualPortfolio,holdings:dict):
    total=portfolio.cash+sum((p.market_value for p in portfolio.positions),D(0));sectors=defaultdict(lambda:D(0));countries=defaultdict(lambda:D(0));issuers=defaultdict(lambda:{'value':D(0),'funds':set(),'ticker':None,'name':None})
    unknown=D(0);unknown_equity=D(0);unmodeled_other=D(0);internal_equity_cash=D(0);rounding=D(0);warnings=[];components=[];equity_value=sum((p.market_value for p in portfolio.positions if p.asset_type=='EQUITY'),D(0))
    direct_metadata={}
    for fund,source in holdings.items():
        if source.get('status')!='AVAILABLE':continue
        if source.get('source_as_of') and datetime.fromisoformat(source['source_as_of']).date()>portfolio.as_of.astimezone(NY).date():continue
        for row in source.get('holdings',[]):
            key=_identity(row)
            if key.startswith('US_TICKER:') and row.get('sector') and row.get('country'):
                prior=direct_metadata.get(key)
                if prior and prior.get('conflicting'):continue
                if prior and (prior['sector'],prior['country'])!=(row['sector'],row['country']):
                    direct_metadata[key]={'conflicting':True}
                elif not prior:
                    direct_metadata[key]={**row,'source_fund':fund,'source_as_of':source.get('source_as_of'),'source_url':source.get('source_url')}
    for pos in portfolio.positions:
        source=holdings.get(pos.ticker)or{}
        if source.get('status')=='AVAILABLE' and source.get('source_as_of'):
            source_day=datetime.fromisoformat(source['source_as_of']).date()
            if source_day>portfolio.as_of.astimezone(NY).date():
                source={'status':'UNAVAILABLE_FOR_ENTERED_ASOF','reason':'Fund composition postdates the entered position valuation'}
            elif (datetime.now(timezone.utc).date()-source_day).days>7:warnings.append(pos.ticker+': fund composition is more than seven days old; look-through is an estimate, not current verified holdings')
        if pos.ticker in SEC_TICKERS and source.get('status')!='AVAILABLE':
            issuer=issuers['US_TICKER:'+pos.ticker];issuer['value']+=pos.market_value;issuer['funds'].add('DIRECT');issuer['ticker']=pos.ticker;issuer['name']=pos.ticker
            metadata=direct_metadata.get('US_TICKER:'+pos.ticker)
            if metadata and not metadata.get('conflicting'):
                sectors[metadata['sector']]+=pos.market_value;countries[metadata['country']]+=pos.market_value
                components.append({'ticker':pos.ticker,'value_usd':_money(pos.market_value),'classification_basis':'Exact US ticker classification from dated issuer holding',
                  'classification_source_fund':metadata['source_fund'],'source_as_of':metadata['source_as_of'],'source_url':metadata['source_url']})
                continue
        if source.get('status')!='AVAILABLE':
            unknown+=pos.market_value
            if pos.asset_type=='EQUITY':unknown_equity+=pos.market_value
            else:unmodeled_other+=pos.market_value
            components.append({'ticker':pos.ticker,'value_usd':_money(pos.market_value),'coverage':'UNKNOWN','reason':source.get('reason','No permitted sourced look-through')});continue
        known=D(0);unquantified=False
        for row in source.get('holdings',[]):
            w=row.get('weight_pct')
            if w is None:unquantified=True;continue
            weight=D(str(w));asset=str(row.get('asset_class',''))
            if not weight.is_finite() or weight<0 or asset not in ('Equity','Cash','Money Market'):
                if weight!=0:unquantified=True
                continue
            amount=pos.market_value*weight/100;known+=amount
            if asset in ('Cash','Money Market'):
                if pos.asset_type=='EQUITY':internal_equity_cash+=amount
                sectors['Fund internal cash (not spendable brokerage cash)']+=amount;continue
            sector=row.get('sector')or'Unclassified';country=row.get('country')or'Unclassified'
            sector_unknown=_norm(sector) in ('','unclassified','unknown','other')
            country_unknown=_norm(country) in ('','unclassified','unknown')
            if sector_unknown or country_unknown:unknown+=amount
            if sector_unknown and pos.asset_type=='EQUITY':unknown_equity+=amount
            sectors[sector]+=amount;countries[country]+=amount
            key=_identity(row);issuer=issuers[key];issuer['value']+=amount;issuer['funds'].add(pos.ticker);issuer['ticker']=row.get('ticker');issuer['name']=row.get('name')
        residual=max(D(0),pos.market_value-known);unknown+=residual
        if pos.asset_type=='EQUITY':unknown_equity+=residual
        else:unmodeled_other+=residual
        if known>pos.market_value:rounding+=known-pos.market_value
        if unquantified:warnings.append(pos.ticker+': unquantified/derivative legs exist; simple long-equity look-through is incomplete')
        components.append({'ticker':pos.ticker,'value_usd':_money(pos.market_value),'source_as_of':source.get('source_as_of'),'source_url':source.get('source_url'),
          'source_captured_at':source.get('captured_at'),'document_sha256':source.get('document_sha256'),'known_long_exposure_usd':_money(known),
          'unknown_residual_usd':_money(residual),'unquantified_legs':unquantified,'weights_renormalized':False})
    tech=sum((v for k,v in sectors.items() if _norm(k) in ('informationtechnology','technology')),D(0))
    if unmodeled_other>0:warnings.append('Non-equity values of '+_money(unmodeled_other)+' USD are unmodeled in these equity-focused shocks; a zero assumed change is not a risk-free claim.')
    if rounding>0:warnings.append('Rounded or gross source weights exceed entered value by '+_money(rounding)+' USD; weights were not normalized.')
    overlap=[{'security_id':k,'ticker':x['ticker'],'name':x['name'],'known_value_usd':_money(x['value']),'known_weight_pct':_pct(x['value'],total),'funds':sorted(x['funds'])}for k,x in issuers.items()]
    overlap.sort(key=lambda x:D(x['known_value_usd']),reverse=True)
    return {'total_entered_value_usd':_money(total),'spendable_cash_usd':_money(portfolio.cash),'cash_weight_pct':_pct(portfolio.cash,total),
      'entered_equity_value_usd':_money(max(D(0),equity_value-internal_equity_cash)),'source_internal_equity_fund_cash_usd':_money(internal_equity_cash),'lookthrough_unknown_usd':_money(unknown),'lookthrough_unknown_weight_pct':_pct(unknown,total),
      'known_technology_value_usd':_money(tech),'known_technology_weight_pct':_pct(tech,total),
      'unknown_equity_lookthrough_usd':_money(unknown_equity),'unmodeled_non_equity_usd':_money(unmodeled_other),
      'technology_upper_bound_with_unknown_usd':_money(max(tech,min(total,tech+unknown_equity))),
      'source_rounding_excess_usd':_money(rounding),'sector_value_estimates_usd':{k:_money(v) for k,v in sectors.items()},
      'issuer_location_value_estimates_usd':{k:_money(v)for k,v in countries.items()},'underlying_securities':overlap,
      'cross_fund_overlap':[x for x in overlap if len(x['funds'])>1],'source_components':components,'warnings':warnings,
      'scope':'USER_ENTERED_VALUES_WITH_DATED_PARTIAL_LOOKTHROUGH','weights_renormalized':False,
      'identity_method':'Exact sourced identities and qualifying US ticker matches; unresolved aliases and share classes are not guessed.',
      'geography_method':'Issuer source location, not geographic revenue exposure.'}


SCENARIOS=[
 {'id':'tech_30','name':'Technology −30%','technology':D('-.30'),'other_equity':D(0)},
 {'id':'tech_50','name':'Technology −50%','technology':D('-.50'),'other_equity':D(0)},
 {'id':'tech_70','name':'Technology −70%','technology':D('-.70'),'other_equity':D(0)},
 {'id':'broad_bear','name':'Broad equity −30%','uniform_equity':D('-.30')},
 {'id':'tech_collapse','name':'2000-style illustrative collapse','technology':D('-.70'),'other_equity':D('-.30')},
 {'id':'ai_credit','name':'Illustrative AI-financing contraction','technology':D('-.35'),'other_equity':D('-.15')},
 {'id':'rate_shock','name':'Illustrative sharp rate-rise repricing','technology':D('-.25'),'other_equity':D('-.15')},
 {'id':'oil_shock','name':'Illustrative geopolitical oil shock','technology':D('-.15'),'energy':D('.20'),'other_equity':D('-.10')}]


def stress(portfolio,exposure):
    total=D(exposure['total_entered_value_usd']);unknown=D(exposure['unknown_equity_lookthrough_usd']);equity=D(exposure['entered_equity_value_usd']);sectors=exposure['sector_value_estimates_usd'];results=[]
    for scenario in SCENARIOS:
        if 'uniform_equity'in scenario:
            point=equity*scenario['uniform_equity'];lo=hi=point;assumptions={'all_entered_equities':str(scenario['uniform_equity']),'cash':'0'}
        else:
            point=D(0);shocks=[scenario['technology'],scenario['other_equity']]
            if'energy'in scenario:shocks.append(scenario['energy'])
            for sector,value in sectors.items():
                if sector=='Fund internal cash (not spendable brokerage cash)' or _norm(sector) in ('','unclassified','unknown','other'):continue
                rate=scenario['technology']if _norm(sector)in('technology','informationtechnology')else scenario.get('energy',scenario['other_equity'])if _norm(sector)=='energy'else scenario['other_equity']
                point+=D(value)*rate
            lo=point+unknown*min(shocks);hi=point+unknown*max(shocks)
            assumptions={k:str(v)for k,v in scenario.items()if isinstance(v,D)};assumptions['cash']='0'
        results.append({'id':scenario['id'],'name':scenario['name'],'hypothetical':True,'known_exposure_change_usd':_money(point),
           'change_range_usd':[_money(lo),_money(hi)],'implied_portfolio_value_range_usd':[_money(total+lo),_money(total+hi)],
           'assumed_simple_returns':assumptions,'not_a_forecast':True,
           'range_is_complete':not(any(x.get('unquantified_legs') for x in exposure['source_components'])or D(exposure['unmodeled_non_equity_usd'])>0 or D(exposure['source_rounding_excess_usd'])>0),
           'limitations':['Simultaneous deterministic bucket shocks, not an estimated correlation model or probability.','Unknown look-through is shown as a sensitivity range; current source rounding/derivatives can add uncertainty.','No taxes, fees, cash interest, option convexity or path-dependent rebalancing modeled.','Non-equity positions are held unchanged in these equity-focused scenarios; no bond-duration model is inferred.']})
    return results


def historical_risk(histories,weights,benchmark='SPY'):
    if not histories or benchmark not in histories:return {'status':'BLOCKED_PRICE_HISTORY','reason':'Aligned genuine daily price histories and a benchmark are required; none inferred.'}
    series={}
    for k,rows in histories.items():
        if k not in weights and k!=benchmark:continue
        values={}
        for row in rows:
            if not isinstance(row.get('session'),str) or len(row['session'])!=10:raise ValueError('Canonical daily session date required')
            day=datetime.fromisoformat(row['session']).date();close=float(row['close'])
            if not is_expected_market_session(day) or day>datetime.now(timezone.utc).date():raise ValueError('Non-session or future historical price')
            if row['session'] in values or not math.isfinite(close) or close<=0:raise ValueError('Duplicate or invalid historical price')
            values[row['session']]=close
        series[k]=values
    if any(k not in series for k in weights):return {'status':'BLOCKED_PRICE_HISTORY','reason':'A weighted holding lacks sourced history'}
    dates=sorted(set.intersection(*(set(x)for x in series.values())))
    if len(dates)<61:return {'status':'INSUFFICIENT_ALIGNED_SESSIONS','aligned_prices':len(dates),'required':61}
    day=datetime.fromisoformat(dates[0]).date();last=datetime.fromisoformat(dates[-1]).date();missing=[]
    while day<=last:
        if is_expected_market_session(day) and day.isoformat() not in dates:missing.append(day.isoformat())
        day+=timedelta(days=1)
    if missing:return {'status':'MISSING_ALIGNED_SESSIONS','missing_expected_sessions':missing,'reason':'Multi-day returns are not silently annualized as daily returns.'}
    returns={k:[values[b]/values[a]-1 for a,b in zip(dates,dates[1:])]for k,values in series.items()}
    p=[sum(float(w)*returns[k][i]for k,w in weights.items())for i in range(len(dates)-1)];b=returns[benchmark];var=statistics.variance(b)
    beta=sum((x-statistics.mean(p))*(y-statistics.mean(b))for x,y in zip(p,b))/(len(p)-1)/var if var else None
    wealth=peak=1.;drawdown=0.
    for r in p:wealth*=1+r;peak=max(peak,wealth);drawdown=min(drawdown,wealth/peak-1)
    correlations={}
    for x in weights:
        for y in weights:
            if x<y:
                try:correlations[x+' / '+y]=statistics.correlation(returns[x],returns[y])
                except statistics.StatisticsError:correlations[x+' / '+y]=None
    return {'status':'CALCULATED_CURRENT_WEIGHT_HYPOTHESIS','aligned_return_sessions':len(p),'annualized_price_volatility':statistics.stdev(p)*math.sqrt(252),
      'beta_vs_'+benchmark:beta,'maximum_price_drawdown':drawdown,'pairwise_price_return_correlations':correlations,
      'scope':'Hypothetical current-weight, daily-rebalanced price-return basket; not actual historical personal performance.','dividends_included':False,
      'first_session':dates[0],'last_session':dates[-1],'omitted_source_dates':{k:len(v)-len(dates)for k,v in series.items()}}


def analyze_portfolio(portfolio,holdings,histories=None, *, deploy_amount=D(0),choices=(),initial_fraction=D('.25')):
    if deploy_amount>portfolio.cash:raise ValueError('Proposed deployment exceeds entered spendable cash')
    exposure=exposures(portfolio,holdings);total=D(exposure['total_entered_value_usd']);warnings=[]
    if not portfolio.complete:warnings.append('Entered portfolio is explicitly incomplete; weights describe only supplied assets, not the entire account.')
    if datetime.now(timezone.utc)-portfolio.as_of>timedelta(days=7):warnings.append('Position values are more than seven days old; obtain current values before treating weights as current.')
    comparisons=[]
    for ticker in (choices if deploy_amount>0 else ()):
        for strategy,fraction in (('DEPLOY_ALL_NOW',D(1)),('INITIAL_STAGED_TRANCHE',initial_fraction)):
            amount=deploy_amount*fraction;updated=portfolio.model_copy(deep=True);updated.cash-=amount
            found=next((p for p in updated.positions if p.ticker==ticker),None)
            if found:found.market_value+=amount
            elif amount>0:updated.positions.append(ManualPosition(ticker=ticker,market_value=amount,account_alias='Other',asset_type='EQUITY'))
            e=exposures(updated,holdings);comparisons.append({'fund':ticker,'strategy':strategy,'deployed_now_usd':_money(amount),'remaining_cash_usd':_money(updated.cash),
             'hypothetical_lookthrough':{k:v for k,v in e.items()if k not in ('underlying_securities','cross_fund_overlap','source_components')},'hypothetical_stress':stress(updated,e),'not_an_order_or_recommendation':True})
    weights=defaultdict(lambda:D(0))
    if total:
        for p in portfolio.positions:weights[p.ticker]+=p.market_value/total
    return {'status':'PARTIAL_SOURCE_COVERAGE'if D(exposure['lookthrough_unknown_usd'])>0 or warnings or exposure['warnings'] else 'SOURCE_BOUND_ESTIMATE',
      'position_basis':'USER_REPORTED_MANUAL_NOT_BROKER_SYNCHRONIZED','as_of':portfolio.as_of.isoformat(),'source':portfolio.source,
      'complete_portfolio_attested':portfolio.complete,'warnings':warnings+exposure['warnings'],'exposure':exposure,'stress_scenarios':stress(portfolio,exposure),
      'historical_risk':historical_risk(histories or{},dict(weights)),
      'cash_comparison':{'budget_usd':_money(deploy_amount),'waiting':{'remaining_cash_usd':_money(portfolio.cash),'exposure':exposure},'alternatives':comparisons,
        'tradeoff':'Staging reduces initial exposure and may miss a rapid rebound. Diversification depends on actual holdings; an unvalidated timing signal supplies no expected-return advantage.'},
      'execution_enabled':False,'orders_placed':False,'actual_historical_performance_claimed':False,'probability':None}
