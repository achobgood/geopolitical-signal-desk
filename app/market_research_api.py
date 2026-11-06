"""Research API and separately scoped owner-private finance boundary."""
from __future__ import annotations
from collections import deque
from datetime import datetime,timezone,timedelta
import hashlib,time
from fastapi import Depends,HTTPException,Query
from pydantic import Field,model_validator
from app.models import StrictModel,PipelineIn
from app.store import get_store
from app.market_sec import SEC_TICKERS
from app.market_portfolio import ManualPortfolio,PortfolioSave,PortfolioAnalyze,analyze_portfolio

FINANCE_ATTEMPTS=deque()

class BriefRun(PipelineIn):
    force:bool=False
class ResearchRequest(StrictModel):
    ticker:str|None=Field(default=None,pattern=r'^[A-Z][A-Z0-9.-]{0,11}$')
    question:str=Field(default='',max_length=500)
    force:bool=False
    @model_validator(mode='after')
    def symbol(self):
        from app.market_ticker_research import SUPPORTED_TICKERS
        import re
        names={'NVIDIA':'NVDA','CISCO':'CSCO'}
        words=set(re.findall(r'\b[A-Z][A-Z0-9.-]{0,11}\b',self.question.upper()))
        found=(words&SUPPORTED_TICKERS)|{v for k,v in names.items()if k in words}
        if len(found)>1:raise ValueError('Select one supported ticker for this research request')
        if found:
            requested=next(iter(found))
            if self.ticker and self.ticker!=requested:raise ValueError('Selected ticker conflicts with the ticker request')
            self.ticker=requested
        if not self.ticker:raise ValueError('Select one supported ticker for this research request')
        if self.ticker not in SUPPORTED_TICKERS:raise ValueError('Unsupported research instrument')
        return self


def register_research_routes(app,authorize,bearer):
    async def finance(credentials=Depends(bearer),store=Depends(get_store)):
        now=time.monotonic()
        while FINANCE_ATTEMPTS and FINANCE_ATTEMPTS[0]<now-60:FINANCE_ATTEMPTS.popleft()
        if len(FINANCE_ATTEMPTS)>=90:raise HTTPException(429,'Private research request limit reached')
        FINANCE_ATTEMPTS.append(now)
        supplied=credentials.credentials if credentials and credentials.scheme.lower()=='bearer' else ''
        if not 32<=len(supplied)<=200:raise HTTPException(401,'Dedicated owner-finance authorization required')
        result=await store.rpc('market_finance_authorize_v1',{'p_hash':hashlib.sha256(supplied.encode()).hexdigest()})
        if not result.get('configured'):raise HTTPException(503,'Owner-private financial research is not configured')
        if result.get('valid')is not True:raise HTTPException(401,'Dedicated owner-finance authorization required')
        return 'owner_finance'

    async def market_context(store):
        rows=await store.rows('market_risk_briefs',limit=1)
        if not rows:return {'status':'NO_PERSISTED_MARKET_CONTEXT'}
        row=rows[0];p=row['payload']
        return {'status':'SOURCE_BOUND_CONTEXT','brief_id':row['id'],'as_of':p['as_of'],'market_regime':p['market_regime'],
          'reference_equity_session':p['reference_equity_session'],'headline':p['headline'],'source_signature':row['source_signature'],
          'predictive_edge_established':False,'scope':'Current dated market context, not a validated timing advantage or authorization to trade'}

    async def ticker_report(store,ticker,force=False):
        from app.market_ticker_research import research_ticker
        existing=await store.rpc('market_finance_report_v1',{'p_ticker':ticker,'p_payload':None})
        if existing and not force and datetime.now(timezone.utc)-datetime.fromisoformat(existing['captured_at'].replace('Z','+00:00'))<timedelta(minutes=15):
            return {**existing,'cache_status':'CACHED_DATED_SOURCE_RESULT'}
        report=await research_ticker(store,ticker,force=force)
        report['market_risk_context']=await market_context(store)
        stored=await store.rpc('market_finance_report_v1',{'p_ticker':ticker,'p_payload':report})
        return {**stored,'cache_status':'RETRIEVED_ACTUAL_SOURCES'}

    @app.get('/v1/market/risk-brief',dependencies=[Depends(authorize)])
    async def risk_brief(store=Depends(get_store)):
        rows=await store.rows('market_risk_briefs',limit=1)
        return {'status':'SOURCE_BOUND_RESEARCH'if rows else 'NO_PERSISTED_BRIEF','record':rows[0]if rows else None,'source_monitoring_checks':await store.rpc('market_macro_health_v1',{}),'execution_enabled':False}

    @app.get('/v1/market/risk-brief/history',dependencies=[Depends(authorize)])
    async def risk_history(limit:int=Query(30,ge=1,le=100),before:int|None=Query(None,gt=0),store=Depends(get_store)):
        return await store.rows('market_risk_briefs',limit=limit,before=before)

    @app.post('/v1/market/risk-brief/run')
    async def run_brief(entry:BriefRun,actor=Depends(authorize),store=Depends(get_store)):
        if actor!='api:shared-key':raise HTTPException(403,'Only the owner may refresh public-source research')
        from app.market_risk import generate_risk_brief
        return await generate_risk_brief(store,refresh_sources=True,force=entry.force)

    @app.get('/v1/market/finance/status',dependencies=[Depends(finance)])
    async def finance_status(store=Depends(get_store)):
        from app.market_ticker_research import SUPPORTED_TICKERS
        latest=await store.rpc('market_portfolio_latest_v1',{})
        return {'status':'OWNER_PRIVATE','portfolio_recorded':bool(latest),'supported_tickers':sorted(SUPPORTED_TICKERS),
          'broker_connected':False,'position_source':'OWNER_MANUAL_INPUT_ONLY','execution_enabled':False}

    @app.post('/v1/market/finance/research',dependencies=[Depends(finance)])
    async def research(entry:ResearchRequest,store=Depends(get_store)):
        return await ticker_report(store,entry.ticker,entry.force)

    @app.get('/v1/market/finance/portfolio',dependencies=[Depends(finance)])
    async def portfolio_get(store=Depends(get_store)):
        latest=await store.rpc('market_portfolio_latest_v1',{})
        return {'status':'USER_REPORTED_PORTFOLIO'if latest else 'NO_MANUAL_PORTFOLIO','record':latest or None,'broker_connected':False,'execution_enabled':False}

    @app.post('/v1/market/finance/portfolio',dependencies=[Depends(finance)])
    async def portfolio_save(entry:PortfolioSave,store=Depends(get_store)):
        record=await store.rpc('market_portfolio_save_v1',{'p':entry.portfolio.model_dump(mode='json'),'p_key':entry.record_key})
        return {'status':'USER_REPORTED_PORTFOLIO_SAVED','record':record,'broker_connected':False,'execution_enabled':False}

    @app.post('/v1/market/finance/portfolio/analyze',dependencies=[Depends(finance)])
    async def portfolio_analyze(entry:PortfolioAnalyze,store=Depends(get_store)):
        latest=await store.rpc('market_portfolio_latest_v1',{})
        if not latest:return {'status':'BLOCKED_NO_MANUAL_POSITIONS','reason':'Enter actual dated holdings and cash through the owner-private form. ChatGPT Finances is not a backend connection.','execution_enabled':False}
        portfolio=ManualPortfolio.model_validate(latest['portfolio'])
        if entry.deploy_amount>portfolio.cash:raise HTTPException(422,'Proposed deployment exceeds entered cash')
        from app.market_etf_holdings import ETF_TICKERS
        from app.market_quotes import configuration
        holdings={};histories={};tickers=set(entry.choices)|{x.ticker for x in portfolio.positions}
        if configuration()['configured']:tickers.add('SPY')
        # Bounded owner-only source reads; unsupported holdings remain unknown.
        for ticker in sorted(tickers):
            if ticker not in ETF_TICKERS and ticker not in SEC_TICKERS:continue
            result=await ticker_report(store,ticker)
            sections=result.get('report',{}).get('sections',{})
            if sections.get('holdings'):holdings[ticker]=sections['holdings']
            bars=sections.get('price_history',{}).get('bars')
            if bars:histories[ticker]=bars
        result=analyze_portfolio(portfolio,holdings,histories,deploy_amount=entry.deploy_amount,choices=entry.choices,initial_fraction=entry.initial_fraction)
        result['market_risk_context']=await market_context(store)
        recorded=await store.rpc('market_portfolio_analysis_v1',{'p_snapshot_id':latest['id'],'p_payload':result})
        return {'status':result['status'],'record':recorded,'execution_enabled':False}
