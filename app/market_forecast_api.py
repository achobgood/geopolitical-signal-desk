"""Additive server API for prospective forecasts and protected experiments."""
import hashlib
import os
import secrets
from datetime import datetime, timezone

from fastapi import Depends, HTTPException, Query
from fastapi.security import HTTPAuthorizationCredentials
from pydantic import Field
from typing import Literal

from app.models import StrictModel, PipelineIn
from app.store import get_store
from app.market_forecasting import run_forecast_cycle, public_forecasts, summarize_performance, summarize_issuance_coverage, clock


class StudyRequest(StrictModel):
    study_id: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9:_-]+$')
    instrument: Literal['SPY','QQQ']


def register_forecast_routes(app, authorize, bearer):
    @app.get('/v1/market/forecasts/public', include_in_schema=False)
    async def public(store=Depends(get_store)):
        return await public_forecasts(store)

    @app.get('/v1/market/forecasts', dependencies=[Depends(authorize)])
    async def forecasts(limit: int=Query(50,ge=1,le=250), before:int|None=Query(None,gt=0),store=Depends(get_store)):
        return await store.rows('market_forecasts',limit=limit,before=before)

    @app.get('/v1/market/forecasts/evaluations', dependencies=[Depends(authorize)])
    async def evaluations(limit: int=Query(50,ge=1,le=250),before:int|None=Query(None,gt=0),store=Depends(get_store)):
        return await store.rows('market_forecast_evaluations',limit=limit,before=before)

    @app.get('/v1/market/forecasts/runs', dependencies=[Depends(authorize)])
    async def runs(limit: int=Query(50,ge=1,le=250),store=Depends(get_store)):
        return {'runs':await store.rows('market_forecast_runs',limit=limit),
                'receipts':await store.rows('market_forecast_run_receipts',limit=limit)}

    @app.get('/v1/market/forecasts/performance', dependencies=[Depends(authorize)])
    async def performance(store=Depends(get_store)):
        # Explicitly bounded recent cohort, never imply lifetime accuracy from a
        # truncated REST response. Outcomes scoped to those exact forecasts.
        rows=await store.rows('market_forecasts',limit=500)
        outcomes=[]
        if rows:
            before=None
            while True:
                page=await store.rows('market_forecast_evaluations',limit=500,before=before,
                      filters={'forecast_id':'in.('+','.join(str(f['id']) for f in rows)+')'})
                outcomes.extend(page)
                if len(outcomes)>15000:
                    raise HTTPException(503,'Evaluation cohort exceeds safe bound; metrics withheld')
                if len(page)<500:break
                cursor=page[-1]['id']
                if before is not None and cursor>=before:
                    raise HTTPException(503,'Evaluation pagination failed; metrics withheld')
                before=cursor
        result=summarize_performance(rows,outcomes)
        receipts=await store.rows('market_forecast_run_receipts',limit=500)
        result['issuance_coverage']=summarize_issuance_coverage(receipts,now=clock())
        result['cohort']={'scope':'MOST_RECENT_500_FORECASTS','forecast_limit':500,
                          'evaluation_rows':len(outcomes),'recent_forecast_cohort':len(rows)>=500}
        return result

    @app.post('/v1/market/forecasts/run')
    async def manual(value:PipelineIn,actor=Depends(authorize),store=Depends(get_store)):
        if actor!='api:shared-key':
            raise HTTPException(403,'Forecast writes require owner authorization')
        return await run_forecast_cycle(store,value.run_key,'manual')

    @app.post('/v1/market/forecasts/scheduled')
    async def scheduled(value:PipelineIn,credentials:HTTPAuthorizationCredentials|None=Depends(bearer),store=Depends(get_store)):
        expected=os.getenv('SIGNAL_DESK_SCHEDULER_KEY_SHA256','')
        supplied=credentials.credentials if credentials and credentials.scheme.lower()=='bearer' else ''
        if len(expected)!=64 or not secrets.compare_digest(hashlib.sha256(supplied.encode()).hexdigest(),expected):
            raise HTTPException(401,'Invalid scheduler credential')
        return await run_forecast_cycle(store,value.run_key,'supabase_cron')

    @app.get('/v1/market/experiments',dependencies=[Depends(authorize)])
    async def ledger(limit:int=Query(50,ge=1,le=250),before:int|None=Query(None,gt=0),store=Depends(get_store)):
        return await store.rows('market_strategy_experiments',limit=limit,before=before)

    @app.post('/v1/market/experiments/run')
    async def experiment(value:StudyRequest,actor=Depends(authorize),store=Depends(get_store)):
        if actor!='api:shared-key':
            raise HTTPException(403,'Strategy experiments require owner authorization')
        from app.market_experiment_inputs import run_stored_experiments
        return await run_stored_experiments(store,study_id=value.study_id,instrument=value.instrument)
