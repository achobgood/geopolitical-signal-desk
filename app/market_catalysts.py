"""Official schedule vintages, not forecasts, reported results or consensus."""
from __future__ import annotations
from datetime import datetime,date,timedelta,timezone
from html.parser import HTMLParser
import calendar,hashlib,re
from pathlib import Path
from zoneinfo import ZoneInfo

NY=ZoneInfo('America/New_York')
FED_CALENDAR='https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm'
BEA_CALENDAR='https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics'
CALENDAR_PROVIDERS={'fed_fomc_calendar':FED_CALENDAR,'bea_release_calendar':BEA_CALENDAR}


def _clean(value):return ' '.join(value.split())


class FomcParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.year=None;self.heading=None;self.depth=0;self.row=None;self.field=None;self.records=[]
    def handle_starttag(self,tag,attrs):
        attrs=dict(attrs)
        if tag=='h4':self.heading=[]
        if tag!='div':return
        self.depth+=1;classes=set(attrs.get('class','').split())
        if 'fomc-meeting'in classes:
            if self.row is not None:raise ValueError('Nested FOMC meeting')
            self.row={'year':self.year,'depth':self.depth,'month':[],'days':[]}
        for cls,field in (('fomc-meeting__month','month'),('fomc-meeting__date','days')):
            if self.row is not None and cls in classes:self.field=(field,self.depth)
    def handle_data(self,data):
        if self.heading is not None:self.heading.append(data)
        if self.row is not None and self.field:self.row[self.field[0]].append(data)
    def handle_endtag(self,tag):
        if tag=='h4'and self.heading is not None:
            text=_clean(''.join(self.heading));match=re.fullmatch(r'(20\d{2}) FOMC Meetings',text)
            self.year=int(match[1])if match else None;self.heading=None
        if tag!='div':return
        if self.field and self.depth==self.field[1]:self.field=None
        if self.row is not None and self.depth==self.row['depth']:
            self.records.append(self.row);self.row=None
        self.depth-=1


def parse_fomc(blob,*,captured_at):
    parser=FomcParser();parser.feed(blob.decode('utf-8-sig'));parser.close();events=[];seen=set();years=set()
    months={m:i for names in (calendar.month_name,calendar.month_abbr)for i,m in enumerate(names)if m}
    for row in parser.records:
        year=row['year']
        if year is None or not captured_at.year<=year<=captured_at.year+1:continue
        month=_clean(''.join(row['month']));days=_clean(''.join(row['days']));projection='*'in days
        match=re.fullmatch(r'(\d{1,2})(?:\s*[-–]\s*(\d{1,2}))?\s*\*?',days)
        month_pair=month.split('/')
        if not match or not 1<=len(month_pair)<=2 or any(m not in months for m in month_pair):raise ValueError('Unrecognized current FOMC date')
        start=date(year,months[month_pair[0]],int(match[1]));end=date(year,months[month_pair[-1]],int(match[2]or match[1]))
        if not 0<=(end-start).days<=3:raise ValueError('Invalid FOMC meeting span')
        identity='fomc:'+str(start)+':'+str(end)
        if identity in seen:raise ValueError('Duplicate FOMC meeting')
        seen.add(identity);years.add(year)
        events.append({'event_id':identity,'title':'FOMC meeting'+(' — economic projections scheduled'if projection else''),
          'scheduled_start_date':str(start),'scheduled_end_date':str(end),'scheduled_at':None,'time_precision':'DATE_ONLY',
          'status':'SCHEDULED','source_url':FED_CALENDAR,'publisher':'Board of Governors of the Federal Reserve System',
          'scope':'POLICY_MEETING','projections_scheduled':projection})
    if captured_at.year not in years or not 4<=len(events)<=30:raise ValueError('Current FOMC schedule missing or structurally incomplete')
    return _receipt('fed_fomc_calendar',FED_CALENDAR,events,captured_at,
      'Source: Board of Governors of the Federal Reserve System. Meeting dates only; no policy outcome or release hour inferred.',
      'https://www.federalreserve.gov/disclaimer.htm')


def _ics_time(value,params):
    if value.endswith('Z')and not params.get('TZID'):
        return datetime.strptime(value,'%Y%m%dT%H%M%SZ').replace(tzinfo=timezone.utc)
    if params.get('TZID')=='America/New_York':
        local=datetime.strptime(value,'%Y%m%dT%H%M%S');instant=local.replace(tzinfo=NY)
        # Ambiguous/nonexistent local times cannot silently choose a DST offset.
        if instant.utcoffset()!=local.replace(tzinfo=NY,fold=1).utcoffset():raise ValueError('Ambiguous calendar clock')
        if instant.astimezone(timezone.utc).astimezone(NY).replace(tzinfo=None)!=local:raise ValueError('Nonexistent calendar clock')
        return instant.astimezone(timezone.utc)
    raise ValueError('Unspecified or unsupported source calendar timezone')


def _ics_text(value):
    return re.sub(r'\\([nN,;\\])',lambda m:'\n'if m[1]in('n','N')else m[1],value)


def parse_bea_calendar(blob,*,captured_at):
    text=blob.decode('utf-8-sig');lines=re.sub(r'\r?\n[ \t]','',text).splitlines()
    if not lines or lines[0]!='BEGIN:VCALENDAR'or lines[-1]!='END:VCALENDAR':raise ValueError('Invalid BEA calendar envelope')
    row=None;rows=[]
    for line in lines:
        if line=='BEGIN:VEVENT':
            if row is not None:raise ValueError('Nested calendar event')
            row={};continue
        if line=='END:VEVENT':
            if row is None:raise ValueError('Unexpected event end')
            rows.append(row);row=None;continue
        if row is None:continue
        if ':'not in line:raise ValueError('Invalid event property')
        key,value=line.split(':',1);parts=key.split(';');name=parts[0].upper();params={}
        for pair in parts[1:]:
            if '='not in pair:raise ValueError('Invalid property parameter')
            k,v=pair.split('=',1);params[k.upper()]=v.strip('"')
        if name in ('RRULE','RDATE','EXDATE','RECURRENCE-ID'):raise ValueError('Recurring calendar events require an explicit supported expansion')
        if name not in ('UID','SUMMARY','DTSTART','DTSTAMP','SEQUENCE','STATUS'):continue
        if name in row:raise ValueError('Duplicate event property')
        row[name]=(value,params)
    if row is not None or not 1<=len(rows)<=1500:raise ValueError('Incomplete or excessive BEA calendar')
    chosen={}
    for row in rows:
        if not all(k in row for k in ('UID','SUMMARY','DTSTART')):raise ValueError('Required event identity/date absent')
        uid=row['UID'][0];title=_clean(_ics_text(row['SUMMARY'][0]));sequence=int(row.get('SEQUENCE',('0',{}))[0]);status=row.get('STATUS',('CONFIRMED',{}))[0]
        if not 1<=len(uid)<=180 or not 5<=len(title)<=400 or not 0<=sequence<=100000 or status not in ('CONFIRMED','TENTATIVE','CANCELLED'):raise ValueError('Invalid calendar identity or status')
        when=_ics_time(*row['DTSTART']);day=str(when.astimezone(NY).date())
        stamp=_ics_time(*row['DTSTAMP']).isoformat()if'DTSTAMP'in row else None
        item={'event_id':'bea:'+uid,'title':title,'scheduled_at':when.isoformat(),'scheduled_start_date':day,'scheduled_end_date':day,
          'time_precision':'TIMESTAMP','timezone':'America/New_York','source_revision_at':stamp,'source_sequence':sequence,
          'status':'CANCELLED'if status=='CANCELLED'else'SCHEDULED','source_tentative':status=='TENTATIVE',
          'source_url':BEA_CALENDAR,'publisher':'U.S. Bureau of Economic Analysis','scope':'ECONOMIC_RELEASE'}
        old=chosen.get(uid)
        if old and old['source_sequence']==sequence and old!=item:raise ValueError('Conflicting equal-sequence event')
        if not old or sequence>old['source_sequence']:chosen[uid]=item
    events=sorted(chosen.values(),key=lambda x:(x['scheduled_at'],x['event_id']))
    if not any(x['scheduled_end_date']>=str(captured_at.astimezone(NY).date())for x in events):raise ValueError('BEA feed has no upcoming coverage')
    return _receipt('bea_release_calendar',BEA_CALENDAR,events,captured_at,
      'Source: U.S. Bureau of Economic Analysis, official online calendar subscription. Scheduled times are not release outcomes or analyst estimates.',
      'https://www.bea.gov/about/policies-and-information')


def _receipt(provider,url,events,captured_at,attribution,terms):
    return {'provider':provider,'source_urls':[url],'source_as_of':str(captured_at.date()),'published_at':None,
      'publication_basis':'Schedule retrieved at the stored collection time. Future event dates are not publication or first-detection timestamps; source revision stamps are not assumed first publication.',
      'data':{'scheduled_event_count':len(events)},'events':events,'attribution':attribution,'terms_url':terms,
      'limitations':['Scheduled dates can change. This feed does not prove an event occurred or establish its result.','Coverage is limited to this official publisher; absence is not an all-clear.']}


def catalyst_calendar(captures,*,as_of,checks=(),horizon_days=90):
    today=as_of.astimezone(NY).date();end=today+timedelta(days=horizon_days);events=[];sources=[];changes=[];warnings=[]
    health={r['provider']:r for r in checks}
    for provider,url in CALENDAR_PROVIDERS.items():
        rows=sorted((r for r in captures if r['provider']==provider),key=lambda x:x['id'],reverse=True)
        if not rows:warnings.append(provider+': no verified schedule capture');continue
        latest=rows[0];p=latest['payload'];check=health.get(provider,{})
        successful=check.get('finished_at')if check.get('status')=='CAPTURED'and check.get('capture_id')==latest['id']else None
        known_time=successful or latest.get('captured_at')or p.get('captured_at');freshness='UNKNOWN'
        if known_time:
            age=as_of-datetime.fromisoformat(known_time.replace('Z','+00:00'));freshness='CURRENTLY_CHECKED'if timedelta(0)<=age<=timedelta(days=1)else'STALE_CHECK'
        if check.get('status')in('UNAVAILABLE','CHECK_IN_PROGRESS'):freshness=check['status']+'_RETAINED_SCHEDULE'
        if freshness!='CURRENTLY_CHECKED':warnings.append(provider+': '+freshness.lower().replace('_',' '))
        source={'provider':provider,'capture_id':latest['id'],'source_url':url,'first_collected_at':p.get('captured_at')or latest.get('captured_at'),
          'last_successful_check_at':successful,'freshness':freshness,'document_sha256':latest.get('document_sha256')or p.get('document_sha256'),
          'publication_basis':p['publication_basis'],'attribution':p['attribution']};sources.append(source)
        for item in p['events']:
            day=date.fromisoformat(item['scheduled_end_date'])
            if item['status']=='CANCELLED'or not today<=day<=end:continue
            if item.get('scheduled_at')and datetime.fromisoformat(item['scheduled_at'])<as_of:continue
            events.append({**item,'capture_id':latest['id'],'usable_from':source['first_collected_at'],'source_freshness':freshness})
        if len(rows)>1:
            old={x['event_id']:x for x in rows[1]['payload']['events']};current={x['event_id']:x for x in p['events']}
            for key,before in old.items():
                if before['scheduled_end_date']<str(today):continue
                after=current.get(key)
                if after is None:kind='REMOVED_FROM_SOURCE_NOT_CONFIRMED_CANCELLED'
                elif before.get('scheduled_at')!=after.get('scheduled_at')or before['scheduled_start_date']!=after['scheduled_start_date']or before['scheduled_end_date']!=after['scheduled_end_date']:kind='RESCHEDULED'
                elif before['status']!=after['status']:kind='STATUS_CHANGED'
                elif before['title']!=after['title']:kind='TITLE_CHANGED'
                else:continue
                changes.append({'event_id':key,'kind':kind,'previous_capture_id':rows[1]['id'],'capture_id':latest['id'],'before':before,'after':after})
    events.sort(key=lambda x:(x['scheduled_start_date'],x.get('scheduled_at')or'',x['event_id']))
    return {'method_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),'status':'PARTIAL_OFFICIAL_CALENDAR'if sources else'NO_VERIFIED_CALENDAR','horizon_days':horizon_days,'events':events[:30],
      'eligible_event_count':len(events),'display_limit':30,'sources':sources,'changes_since_previous_capture':changes[:30],
      'warnings':warnings,'coverage':'Official BEA releases and FOMC meeting dates only. BLS jobs/CPI, Treasury auctions, earnings, consensus and unscheduled events are not covered.',
      'interpretation':'Schedule evidence only. No release outcome, surprise, directional forecast or calibrated market-impact probability is inferred.'}
