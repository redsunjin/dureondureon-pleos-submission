'use strict';
const $ = id => document.getElementById(id);
const labels = {vacant:'빈 공간',occupied:'점유',blocked:'차단',unknown:'확인 필요',assigned:'안내 중',closed:'구역 폐쇄'};
const reasons = {fresh_report:'유효한 최신 관측입니다.',unobserved:'아직 관측이 없습니다.',stale:'관측 유효 시간이 지났습니다.',clock_mismatch:'관측 시각을 다시 확인해야 합니다.',sharing_inactive:'정보 공유가 꺼졌거나 새 동의 이후 관측이 없습니다.',low_confidence:'신뢰도가 낮아 배정할 수 없습니다.',occluded:'가림·판단 불가로 현장 확인이 필요합니다.',conflicting_reports:'빈 공간과 점유 관측이 충돌합니다. 현장 확인이 필요합니다.',obstacle_reported:'차단물 관측이 있습니다.'};
const errorLabels = {vacancy_not_available:'현재 배정 가능한 공간이 아닙니다.',vacancy_arrival_already_assigned:'이 차량은 이미 다른 공간으로 안내 중입니다.',vacancy_occupancy_not_confirmed:'신선한 점유 관측을 먼저 등록해 주세요.',vacancy_sharing_disabled:'해당 가상 차량의 공간 정보 공유를 먼저 허용해 주세요.',observation_out_of_order:'더 최신 관측이 있습니다. 최신 상태에서 다시 등록해 주세요.',vacancy_revision_changed:'운영 상태가 바뀌었습니다. 최신 상태를 다시 확인합니다.',vacancy_map_changed:'지도 정보가 바뀌었습니다. 페이지를 새로 열어 주세요.',vacancy_assignment_not_active:'이미 종료되거나 만료된 안내입니다.',invalid_observation:'관측 시각·신뢰도·유효 시간을 확인해 주세요.'};
const eventLabels={observation:'관측 등록',assignment:'입차 안내',assignment_expired:'안내 만료',assignment_cancelled:'안내 취소',arrival:'도착 확인',zone_opened:'구역 개방',zone_closed:'구역 폐쇄',sharing_enabled:'공간 정보 공유 허용',sharing_disabled:'공간 정보 공유 철회'};
const sourceLabels={'STAFF-1':'현장요원 1','CAR-1':'가상 차량 1','CAR-2':'가상 차량 2','CAR-3':'가상 차량 3'};
const pendingKey='swarm-vacancy-pending-v1';
let snapshot=null, selected='A-01', received=0, roundTrip=0, connected=false, polling=false, posting=false, pending=null, lastHistory=-1;
try {pending=JSON.parse(localStorage.getItem(pendingKey)||'null');} catch {localStorage.removeItem(pendingKey);}
function text(id,value){const el=$(id);if(el.textContent!==String(value))el.textContent=String(value);}
function tell(value,error=false){text('feedback',value);$('feedback').classList.toggle('error',error);}
function fresh(){return connected && snapshot && performance.now()-received<3500;}
function serverNow(){return snapshot ? snapshot.serverTimeMillis+roundTrip+(performance.now()-received) : 0;}
function writable(){return fresh() && !pending && !posting;}
function displayState(s){if(!fresh())return 'unknown';if(!s.zoneOpen)return 'closed';if(s.occupancyUntilMillis>serverNow() && s.occupancy==='occupied')return 'occupied';if(s.occupancyUntilMillis>serverNow() && s.occupancy==='blocked')return 'blocked';if(s.assignment)return 'assigned';if(s.available && s.availableUntilMillis>serverNow())return 'vacant';return 'unknown';}
function assignable(s){return writable() && s.available && s.availableUntilMillis>serverNow();}
function time(value){return new Intl.DateTimeFormat('ko-KR',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Seoul'}).format(new Date(value));}
function apply(data,started){
  if(snapshot && (data.sequence<snapshot.sequence || data.sequence===snapshot.sequence && data.serverTimeMillis<snapshot.serverTimeMillis))return;
  snapshot=data;received=performance.now();roundTrip=received-started;connected=true;buildPlan();render();
}
async function request(path,options={}){
  const controller=new AbortController(), timeout=setTimeout(()=>controller.abort(),4000);
  try {
    const response=await fetch(path,{...options,cache:'no-store',signal:controller.signal});
    const data=await response.json();
    if(!response.ok)throw Object.assign(new Error(data.error||'request_failed'),{status:response.status,code:data.error});
    return data;
  } finally {clearTimeout(timeout);}
}
async function poll(){
  if(polling)return;polling=true;const start=performance.now();
  try {apply(await request('/v1/vacancy/state?eventId=festival-demo'),start);}
  catch {connected=false;render();}
  finally {polling=false;}
  if(pending && !posting)sendPending();
}
async function mutate(path,body){
  if(!writable())return;
  pending={path,body:{eventId:snapshot.eventId,mapVersion:snapshot.mapVersion,requestId:crypto.randomUUID(),...body}};
  try {localStorage.setItem(pendingKey,JSON.stringify(pending));}
  catch {pending=null;tell('요청을 기기에 저장하지 못해 전송하지 않았습니다.',true);return;}
  render();await sendPending();
}
async function sendPending(){
  if(!pending || posting)return;posting=true;render();const start=performance.now();
  try {
    if(!pending.path.startsWith('/v1/vacancy/'))throw Object.assign(new Error('invalid_pending'),{status:400});
    const data=await request(pending.path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending.body)});
    const operation=pending.path;pending=null;localStorage.removeItem(pendingKey);apply(data,start);
    const result=data.actionResult;
    if(result?.kind==='assignment')tell(result.status==='expired'?'이전 안내 요청은 이미 만료되었습니다. 새 안내가 생성되지 않았습니다.':result.status==='arrived'?'점유 관측과 도착을 확인했습니다.':result.status==='cancelled'?'안내를 취소했습니다.':'입차 차량 안내를 저장했습니다. 실제 점유는 별도로 확인해 주세요.');
    else tell(operation.endsWith('/observations')?'관측을 저장했습니다. 같은 공간의 여러 관측은 한 곳으로 계산합니다.':'운영 설정을 저장했습니다.');
  } catch(error) {
    if(error.status>=400 && error.status<500){pending=null;localStorage.removeItem(pendingKey);tell(errorLabels[error.code]||'요청을 반영하지 못했습니다. 입력과 최신 상태를 확인해 주세요.',true);}
    else {connected=false;tell('요청 결과 확인 대기 · 같은 요청으로 다시 확인하며 추가 입력을 잠급니다.',true);}
  } finally {posting=false;render();}
}
function buildPlan(){
  if($('plan').children.length)return;
  for(const z of snapshot.zones){
    const section=document.createElement('section');section.className='zone';section.dataset.zone=z.id;
    const heading=document.createElement('div');heading.className='zone-heading';
    const title=document.createElement('div'),strong=document.createElement('strong');strong.textContent=z.id+' 구역';title.append(strong,document.createTextNode(z.label));
    const counts=document.createElement('span');counts.id='zone-count-'+z.id;heading.append(title,counts);
    const spaces=document.createElement('div');spaces.className='zone-spaces';
    for(const s of snapshot.spaces.filter(x=>x.zone_id===z.id)){
      const button=document.createElement('button');button.type='button';button.className='space';button.dataset.space=s.id;
      const title=document.createElement('strong');title.textContent=s.id;const detail=document.createElement('small');button.append(title,detail);spaces.append(button);
    }
    section.append(heading,spaces);$('plan').append(section);
  }
}
function observationLabel(s){
  if(!fresh())return '마지막 수신';
  if(s.reason==='stale' || s.occupancyUntilMillis && s.occupancyUntilMillis<=serverNow())return '관측 만료';
  if(s.reason==='unobserved')return '미관측';
  if(s.reason==='conflicting_reports')return '관측 상충';
  return labels[s.occupancy]||'확인 필요';
}
function render(){
  text('connection',fresh()?'관제 연결됨':'연결 끊김 · 마지막 수신');$('connection').className=fresh()?'online':'offline';
  $('retry').hidden=!pending;$('retry').disabled=posting;
  if(!snapshot){text('banner','관제에 연결하면 가상 행사장의 관측 현황을 확인할 수 있습니다.');return;}
  const isFresh=fresh(), counts=snapshot.counts;
  text('last-received',`마지막 수신 ${time(snapshot.serverTimeMillis)}`);
  const currentCounts={...counts,available:snapshot.spaces.filter(s=>s.available && s.availableUntilMillis>serverNow()).length,occupied:snapshot.spaces.filter(s=>s.occupancy==='occupied' && s.occupancyUntilMillis>serverNow()).length,unknown:snapshot.spaces.filter(s=>s.occupancy==='unknown' || s.occupancyUntilMillis && s.occupancyUntilMillis<=serverNow()).length};
  for(const k of ['available','assigned','occupied','unknown'])text('count-'+k,isFresh?currentCounts[k]:'—');
  text('banner',!isFresh?'연결이 끊겼거나 수신이 지연됐습니다. 마지막 화면을 표시하며 배정을 잠급니다.':pending?'요청 결과를 확인 중입니다. 다른 변경은 잠시 잠깁니다.':`가상 지도 ${snapshot.mapVersion}판 · 신뢰도 80% 이상인 유효 관측만 사용 · 미확인/상충/오래된 정보는 배정 제외`);
  $('banner').classList.toggle('alert',!isFresh);
  for(const z of snapshot.zones){
    const zone=document.querySelector(`[data-zone="${z.id}"]`);zone.hidden=$('zone-filter').value!=='all' && $('zone-filter').value!==z.id;
    const available=snapshot.spaces.filter(s=>s.zone_id===z.id && s.available && s.availableUntilMillis>serverNow()).length;
    text('zone-count-'+z.id,!isFresh?'마지막 수신':!z.open?'구역 폐쇄':`가능 ${available} · 안내 ${z.counts.assigned} · 확인 ${z.counts.unknown}`);
  }
  for(const s of snapshot.spaces){const button=document.querySelector(`[data-space="${s.id}"]`),status=displayState(s);button.className='space '+status+(selected===s.id?' selected':'');button.querySelector('small').textContent=labels[status];button.setAttribute('aria-label',`${s.id} · ${labels[status]}`);button.setAttribute('aria-pressed',String(selected===s.id));}
  const s=snapshot.spaces.find(s=>s.id===selected),zone=snapshot.zones.find(z=>z.id===s.zone_id),status=displayState(s);
  text('selected-title',s.id);text('selected-status',status==='vacant'?'배정 가능':labels[status]);text('selected-zone',`${s.zone_id} 구역 · ${s.row_id} 행`);text('observe-target',s.id+'에 기록');
  $('selected-status').className='badge '+status;
  text('observed-status',observationLabel(s));
  text('selected-reason',!isFresh?'마지막 수신 상태입니다. 새 관측을 확인해야 합니다.':!s.zoneOpen?'운영자가 구역을 폐쇄했습니다.':s.available && s.availableUntilMillis<=serverNow()?'관측 유효 시간이 지났습니다.':reasons[s.reason]||'현장 확인이 필요합니다.');
  text('zone-toggle',zone.open?'구역 폐쇄':'구역 다시 열기');$('zone-toggle').disabled=!writable();$('assign').disabled=!assignable(s);
  const a=snapshot.assignments.find(a=>a.space_id===s.id && a.status==='active');$('assignment').hidden=!a;
  text('guide-status',a?' 안내 중':'안내 없음');
  if(a){text('assignment-label',a.arrival_id+' 안내 중');const remaining=Math.max(0,Math.ceil((a.expires-serverNow())/1000));text('assignment-status',`${remaining}초 후 안내 만료`+(a.attentionReason?' · 현장 재확인 필요':''));$('cancel').disabled=!writable();$('arrival').disabled=!writable() || s.occupancy!=='occupied' || s.occupancyUntilMillis<=serverNow() || remaining<=0;}
  const latest=s.reports.reduce((newest,r)=>!newest || r.observed>newest.observed?r:newest,null);
  text('latest-summary',latest?`${sourceLabels[latest.source_id]||latest.source_id} · ${labels[latest.state]} · ${Math.max(0,Math.floor((serverNow()-latest.observed)/1000))}초 전`:'관측 없음');
  text('observation-validity',!isFresh?'연결 복구 후 최신 관측을 확인하세요.':s.reason==='conflicting_reports'?'관측 상충 · 현장 재확인 필요':latest?`관측 ${time(latest.observed)} · ${latest.sharingActive && latest.fresh && latest.expiresAtMillis>serverNow()?`유효 시간 ${Math.max(0,Math.ceil((latest.expiresAtMillis-serverNow())/1000))}초 남음`:'유효 관측 재확인 필요'}`:'새 관측을 등록해 주세요.');
  const reports=$('reports');reports.replaceChildren();
  if(!s.reports.length){const p=document.createElement('p');p.className='support';p.textContent='관측 없음 · 빈자리로 간주하지 않습니다.';reports.append(p);}
  for(const r of s.reports){const box=document.createElement('div');box.className='report';const title=document.createElement('strong');title.textContent=(sourceLabels[r.source_id]||r.source_id)+' · '+labels[r.state];const detail=document.createElement('p');const valid=r.fresh && r.expiresAtMillis>serverNow() && isFresh;detail.textContent=`${time(r.observed)} · 신뢰도 ${Math.round(r.confidence*100)}% · ${!r.sharingActive?'공유 비활성':valid?'유효':'확인 필요'}`;box.append(title,detail);reports.append(box);}
  const src=snapshot.sources.find(x=>x.id===$('source').value);$('sharing-row').hidden=src.kind!=='vehicle';$('sharing').checked=pending?.path===`/v1/vacancy/sources/${src.id}/consent`?pending.body.enabled:Boolean(src.sharing);$('sharing').disabled=!writable();$('observe').disabled=!writable() || !src.sharing;
  text('source-hint',src.kind==='field'?'현장요원 입력은 수기 확인 기록입니다.':src.sharing?'가상 차량의 주변 관측 입력입니다. 실제 센서·차량 이동은 연결되지 않았습니다.':'공간 정보 공유를 허용한 뒤 새 관측을 등록해 주세요. 이전 동의 때의 관측은 다시 사용하지 않습니다.');
  text('last-update',`마지막 수신 ${time(snapshot.serverTimeMillis)} · 최근 ${snapshot.events.length}건`);
  if(lastHistory!==snapshot.sequence){lastHistory=snapshot.sequence;const body=$('history');body.replaceChildren();for(const e of snapshot.events){const row=document.createElement('tr');for(const value of [time(e.at),e.subject,eventLabels[e.event]||e.event,(e.details.sourceId?sourceLabels[e.details.sourceId]||e.details.sourceId:e.details.arrivalId)||'—']){const cell=document.createElement('td');cell.textContent=value;row.append(cell);}body.append(row);}if(!snapshot.events.length){const row=document.createElement('tr'),cell=document.createElement('td');cell.colSpan=4;cell.textContent='기록이 없습니다. 관측 등록부터 시작하세요.';row.append(cell);body.append(row);}}
}
$('plan').addEventListener('click',event=>{const button=event.target.closest('[data-space]');if(button){selected=button.dataset.space;render();if(matchMedia('(max-width:800px)').matches){$('selected-title').focus({preventScroll:true});document.querySelector('.inspector').scrollIntoView({block:'start'});}}});
$('zone-filter').addEventListener('change',()=>{const zone=$('zone-filter').value;if(snapshot && zone!=='all' && snapshot.spaces.find(s=>s.id===selected)?.zone_id!==zone)selected=snapshot.spaces.find(s=>s.zone_id===zone).id;render();});$('source').addEventListener('change',render);
$('back-to-plan').addEventListener('click',()=>{document.querySelector(`[data-space="${selected}"]`)?.focus({preventScroll:true});document.querySelector('.map-toolbar').scrollIntoView({block:'start'});});
function setView(list){
  document.querySelector('.plan-section').classList.toggle('list-mode',list);
  $('map-view').setAttribute('aria-pressed',String(!list));
  $('list-view').setAttribute('aria-pressed',String(list));
}
$('map-view').addEventListener('click',()=>setView(false));
$('list-view').addEventListener('click',()=>setView(true));
// Keep touch targets and status text readable on narrow devices.
setView(matchMedia('(max-width:1000px)').matches);
$('refresh').addEventListener('click',poll);$('retry').addEventListener('click',sendPending);
$('observe-form').addEventListener('submit',event=>{event.preventDefault();if(!writable())return;const source=snapshot.sources.find(s=>s.id===$('source').value);mutate('/v1/vacancy/observations',{sourceId:source.id,spaceId:selected,sequence:source.last_sequence+1,observedAtMillis:snapshot.serverTimeMillis-Number($('age').value)*1000,ttlMillis:Number($('ttl').value)*1000,confidence:Number($('confidence').value),state:$('observation-state').value,view:$('view').value});});
$('sharing').addEventListener('change',()=>{const src=snapshot.sources.find(s=>s.id===$('source').value);mutate(`/v1/vacancy/sources/${src.id}/consent`,{enabled:$('sharing').checked,revision:src.revision});});
$('zone-toggle').addEventListener('click',()=>{const space=snapshot.spaces.find(s=>s.id===selected),zone=snapshot.zones.find(z=>z.id===space.zone_id);mutate(`/v1/vacancy/zones/${zone.id}/status`,{open:!zone.open,revision:zone.revision});});
$('assign-form').addEventListener('submit',event=>{event.preventDefault();mutate('/v1/vacancy/assignments',{spaceId:selected,arrivalId:$('arrival-id').value.trim()});});
for(const [id,action] of [['cancel','cancel'],['arrival','arrival']])$(id).addEventListener('click',()=>{const a=snapshot.assignments.find(a=>a.space_id===selected && a.status==='active');if(a)mutate(`/v1/vacancy/assignments/${a.id}/${action}`,{});});
poll();setInterval(poll,1000);setInterval(render,250);
