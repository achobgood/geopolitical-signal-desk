import asyncio,copy,json
from datetime import datetime,timezone,timedelta
import pytest
from app.market_catalysts import parse_bea_calendar,parse_fomc,catalyst_calendar,BEA_CALENDAR
from app import market_macro as macro

NOW=datetime(2026,11,6,19,tzinfo=timezone.utc)
def event(uid='release',when='20261125T133000Z',sequence=0,status='CONFIRMED',title='GDP and corporate profits'):
 return f'BEGIN:VEVENT\r\nUID:{uid}\r\nSUMMARY:{title}\r\nDTSTART:{when}\r\nSEQUENCE:{sequence}\r\nSTATUS:{status}\r\nDTSTAMP:20261105T120000Z\r\nEND:VEVENT\r\n'
def feed(*events):return ('BEGIN:VCALENDAR\r\n'+''.join(events)+'END:VCALENDAR\r\n').encode()
def capture(id,payload,at=NOW):return {'id':id,'provider':payload['provider'],'captured_at':at.isoformat(),'payload':{**payload,'captured_at':at.isoformat()}}
def test_utc_calendar_folded_text_and_source_revision_are_not_first_detection():
 p=parse_bea_calendar(feed(event(title='Gross Domestic Product\\, third\r\n  quarter')),captured_at=NOW)
 e=p['events'][0];assert e['title']=='Gross Domestic Product, third quarter'
 assert e['scheduled_at']=='2026-11-25T13:30:00+00:00'and e['scheduled_start_date']=='2026-11-25'
 assert e['source_revision_at']=='2026-11-05T12:00:00+00:00'and p['published_at']is None
 c=catalyst_calendar([capture(1,p)],as_of=NOW)
 assert c['events'][0]['usable_from']==NOW.isoformat()and c['events'][0]['scheduled_at']!=c['events'][0]['usable_from']
def test_cancelled_revised_event_does_not_survive_as_upcoming():
 p=parse_bea_calendar(feed(event(),event(sequence=1,status='CANCELLED'),event(uid='other',when='20261201T133000Z')),captured_at=NOW)
 c=catalyst_calendar([capture(1,p)],as_of=NOW);assert [x['event_id']for x in c['events']]==['bea:other']
def test_equal_revision_conflict_and_unsupported_recurrence_fail_closed():
 with pytest.raises(ValueError):parse_bea_calendar(feed(event(),event(when='20261126T133000Z')),captured_at=NOW)
 with pytest.raises(ValueError):parse_bea_calendar(feed(event().replace('END:VEVENT','RRULE:FREQ=DAILY\r\nEND:VEVENT')),captured_at=NOW)
@pytest.mark.parametrize('when',['20261125T083000','20261125'])
def test_floating_or_date_only_bea_time_is_never_assumed_eastern(when):
 with pytest.raises(ValueError):parse_bea_calendar(feed(event(when=when)),captured_at=NOW)
def test_dst_ambiguity_fails_instead_of_choosing_an_offset():
 raw=feed(event(when='20261101T013000').replace('DTSTART:','DTSTART;TZID=America/New_York:'))
 with pytest.raises(ValueError):parse_bea_calendar(raw,captured_at=NOW)
def test_prior_event_time_is_not_shown_as_upcoming_and_unchanged_source_can_be_freshly_checked():
 p=parse_bea_calendar(feed(event(when='20261106T133000Z'),event(uid='future')),captured_at=NOW)
 old=NOW-timedelta(days=7);check={'provider':p['provider'],'status':'CAPTURED','capture_id':9,'finished_at':NOW.isoformat()}
 c=catalyst_calendar([capture(9,p,old)],as_of=NOW,checks=[check])
 assert [e['event_id']for e in c['events']]==['bea:future']and c['sources'][0]['freshness']=='CURRENTLY_CHECKED'
 check['status']='UNAVAILABLE';c=catalyst_calendar([capture(9,p,old)],as_of=NOW,checks=[check])
 assert c['sources'][0]['freshness']=='UNAVAILABLE_RETAINED_SCHEDULE'and c['warnings']
def test_reschedule_and_removal_are_attributed_to_distinct_captured_vintages():
 previous=parse_bea_calendar(feed(event(),event(uid='removed')),captured_at=NOW)
 current=parse_bea_calendar(feed(event(when='20261126T133000Z',sequence=1)),captured_at=NOW)
 c=catalyst_calendar([capture(2,current),capture(1,previous)],as_of=NOW)
 assert {x['kind']for x in c['changes_since_previous_capture']}=={'RESCHEDULED','REMOVED_FROM_SOURCE_NOT_CONFIRMED_CANCELLED'}
 assert all(x['previous_capture_id']==1 and x['capture_id']==2 for x in c['changes_since_previous_capture'])
def test_fomc_dates_have_no_invented_hour_and_cross_month_is_preserved():
 rows=''.join(f'<div class="row fomc-meeting"><div class="fomc-meeting__month"><strong>{m}</strong></div><div class="fomc-meeting__date">{d}</div></div>'for m,d in [('January','27-28'),('March','17-18*'),('Apr/May','30-1'),('December','8-9*')])
 p=parse_fomc(('<h4>2026 FOMC Meetings</h4>'+rows).encode(),captured_at=NOW)
 cross=p['events'][2];assert cross['scheduled_start_date']=='2026-04-30'and cross['scheduled_end_date']=='2026-05-01'
 assert all(e['scheduled_at']is None and e['time_precision']=='DATE_ONLY'for e in p['events'])
 assert p['events'][-1]['projections_scheduled']is True
def test_bls_batch_is_one_request_and_series_order_is_not_trusted(monkeypatch):
 from test_market_macro_risk import bls
 calls=[];docs=bls();series=[x['Results']['series'][0]for x in docs.values()]
 async def one(url,**kwargs):calls.append((url,kwargs));return json.dumps({'status':'REQUEST_SUCCEEDED','Results':{'series':list(reversed(series))}}).encode(),{}
 monkeypatch.setattr(macro,'official_bytes',one);p=asyncio.run(macro.fetch_macro('bls_employment'))
 assert len(calls)==1 and calls[0][1]['method']=='POST'and calls[0][1]['json_body']['seriesid']==list(macro.BLS_SERIES.values())
 assert p['data']['payroll_change_jobs']=='165000'and p['source_urls']==[macro.BLS_BASE]
def test_bls_quota_reason_is_bounded_without_returning_arbitrary_messages():
 e=macro.OfficialSourceDeclined('REQUEST_NOT_PROCESSED',['Daily threshold for total number of requests reached for this system. secret-canary'])
 d=macro.failure_diagnostic(e);assert d['reason_code']=='DAILY_QUOTA'and 'secret-canary'not in str(d)

def test_brief_reads_each_provider_so_frequent_captures_cannot_evict_a_slow_calendar(monkeypatch):
 from app import market_risk as risk
 from test_market_macro_risk import market
 p=parse_bea_calendar(feed(event()),captured_at=NOW);requests=[]
 class Store:
  async def rows(self,table,**kwargs):
   requests.append(kwargs['filters'])
   assert table=='market_macro_captures'
   return [capture(1,p)]if kwargs['filters']['provider']=='eq.bea_release_calendar'else[]
  async def rpc(self,name,body):
   if name=='market_macro_health_v1':return []
   assert name=='market_record_risk_brief_v1';return {'id':8,'generated_at':NOW.isoformat(),'payload':body['p']}
 async def snapshot(*args,**kwargs):return market()
 async def geo(*args,**kwargs):return []
 monkeypatch.setattr(risk,'snapshot',snapshot);monkeypatch.setattr(risk,'_geopolitical_market_context',geo)
 result=asyncio.run(risk.generate_risk_brief(Store(),refresh_sources=False))
 assert {x['provider']for x in requests}=={'eq.'+x for x in macro.PROVIDERS}
 assert result['brief']['catalyst_calendar']['sources'][0]['capture_id']==1
