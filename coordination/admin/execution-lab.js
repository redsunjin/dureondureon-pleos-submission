'use strict';
(()=>{
 const $=id=>document.getElementById(id),key='swarm-execution-lab-pending';
 let current=null,online=false,busy=false,polling=false,pending=null,generation=0,last=0,notice='';
 try{pending=JSON.parse(localStorage.getItem(key)||'null');}catch{}
 const states={awaiting_ready:'차량 준비 대기',ready:'준비 완료 · 시작 대기',running:'명령 응답 확인 중',stop_requested:'정지 요청 · 결과 미확인',stopped:'차량 정지 확인',cancelled:'명령 발행 전 취소',completed:'모든 차량 완료 확인'};
 const safety={missing:'관측 없음',disabled:'관측 검사 미사용',clearing:'위험 해소 확인 중',clear:'예측 구간 위험 없음',risk:'예측 위험 감지',stale:'관측 유효 시간 초과',unknown:'위험 판단 불가'};
 const reasons={vehicle_readiness_required:'각 차량의 준비 응답을 확인합니다.',awaiting_dispatch:'명시적인 명령 발행을 기다립니다.',command_response_required:'명령 발행은 이동 완료를 뜻하지 않습니다.',traffic_risk:'예측 위험으로 다음 이동을 막았습니다.',traffic_stale:'오래된 관측을 안전한 빈 구간으로 보지 않습니다.',traffic_unknown:'판단 불가 상태에서 이동하지 않습니다.',vehicle_stop_confirmed:'차량의 정지 보고와 자세를 확인했습니다.',all_vehicle_completions_confirmed:'모든 명령의 완료 보고를 확인했습니다.',execution_response_missing:'제한 시간 안에 명령 응답을 받지 못했습니다.',vehicle_connection_lost:'참여 차량의 응답이 끊겼습니다.'};
 const kinds={delegation:'사전 위임 등록',proposed:'계획 및 이동 한도 예약',vehicle_ready:'차량 준비 응답',dispatched:'이동 명령 발행',command_accepted:'차량 명령 수락',executing:'차량 실행 보고',completed:'차량 완료 보고',stopped:'차량 정지 보고',halt:'다음 이동 차단',traffic_observed:'이동체 관측 갱신',delegation_revoked:'위임 철회'};
 function validSnapshot(d){
  const p=d?.plan,g=p?.geometry;
  const finite=v=>typeof v==='number'&&Number.isFinite(v),pose=v=>v&&['x','y','yaw'].every(k=>finite(v[k]));
  if(d?.simulationOnly!==true||!p||!states[p.status]||!safety[p.trafficSafety?.safety]||!g||!finite(g.length)||g.length<=0||!finite(g.width)||g.width<=0||!g.bodies||!d.poses||!d.usage||!d.grants||!Array.isArray(d.events)||!Array.isArray(d.path)||!Array.isArray(p.commands)||!Number.isInteger(p.index)||p.index<0||p.index>p.commands.length||!Number.isInteger(d.revision)||d.revision<0||!finite(d.virtualTimeMillis)||typeof d.serverEpoch!=='string'||typeof d.demoId!=='string')return false;
  if(!Object.entries(d.poses).every(([v,q])=>pose(q)&&g.bodies[v]&&finite(g.bodies[v].length)&&g.bodies[v].length>0&&finite(g.bodies[v].width)&&g.bodies[v].width>0)||!d.path.every(pose))return false;
  if(!Object.entries(d.usage).every(([v,u])=>u&&finite(u.used)&&u.used>=0&&finite(u.reserved)&&u.reserved>=0&&d.grants[v]&&finite(d.grants[v].total)))return false;
  if(!p.commands.every(c=>c&&g.bodies[c.vehicle]&&finite(c.distance)))return false;
  if(p.traffic&&!p.traffic.tracks?.every(t=>t&&pose(t.pose)&&finite(t.vx)&&finite(t.vy)&&finite(t.observedAtMillis)))return false;
  return true;
 }
 async function request(path,body){const c=new AbortController(),timer=setTimeout(()=>c.abort(),2500);try{const r=await fetch(path,{cache:'no-store',signal:c.signal,...(body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{})});const data=await r.json();if(!r.ok)throw Object.assign(Error(data.error),{status:r.status});if(!validSnapshot(data))throw Error('invalid_lab_snapshot');return data;}finally{clearTimeout(timer);}}
 const svg=(tag,attrs,text)=>{const e=document.createElementNS('http://www.w3.org/2000/svg',tag);for(const [k,v] of Object.entries(attrs))e.setAttribute(k,String(v));if(text)e.textContent=text;return e;};
 function draw(){const root=$('scene');root.replaceChildren();if(!current)return;const p=current.plan,g=p.geometry,sx=850/g.length,sy=220/g.width;
  root.append(svg('rect',{x:25,y:20,width:850,height:220,fill:'#e8ede6',stroke:'#a8b9ac'}));
  root.append(svg('line',{x1:25,y1:130,x2:875,y2:130,stroke:'#c4cec3','stroke-dasharray':'7 8'}));
  if(current.path.length){root.append(svg('polyline',{points:current.path.map(q=>`${25+q.x*sx},${240-q.y*sy}`).join(' '),fill:'none',stroke:'#bf832c','stroke-width':3,'stroke-dasharray':'5 5'}));}
  for(const [id,pose] of Object.entries(current.poses)){const spec=g.bodies[id],x=25+pose.x*sx,y=240-pose.y*sy,group=svg('g',{transform:`translate(${x} ${y}) rotate(${-pose.yaw*180/Math.PI})`});
   group.append(svg('rect',{x:-spec.length*sx/2,y:-spec.width*sy/2,width:spec.length*sx,height:spec.width*sy,rx:5,fill:id==='NEW'?'#cfdfd0':'#fdfefb',stroke:p.motionKnowledge==='unknown'&&p.commands[p.index]?.vehicle===id?'#b27923':'#286452','stroke-width':2,'stroke-dasharray':p.motionKnowledge==='unknown'&&p.commands[p.index]?.vehicle===id?'6 4':'none'}));
   group.append(svg('text',{'text-anchor':'middle','dominant-baseline':'middle','font-size':14,fill:'#17383b'},id));root.append(group);}
  for(const t of p.traffic?.tracks||[]){const dt=Math.max(0,(current.virtualTimeMillis-t.observedAtMillis)/1000),x=25+(t.pose.x+t.vx*dt)*sx,y=240-(t.pose.y+t.vy*dt)*sy;
   root.append(svg('line',{x1:x,y1:y,x2:x+t.vx*12*sx,y2:y-t.vy*12*sy,stroke:'#7956a0','stroke-width':2,'stroke-dasharray':'4 4'}));root.append(svg('circle',{cx:x,cy:y,r:7,fill:'#7956a0'}));}
  root.append(svg('text',{x:28,y:270,'font-size':13,fill:'#607270'},'로컬 가상 좌표 · 시간 기반 위치 보간 없음'));
 }
 function render(){const p=current?.plan,s=p?.trafficSafety,c=p?.commands[p.index],locked=!online||Date.now()-last>3000||busy||!!pending||!p;
  $('connection').textContent=online?'시험실 연결됨':'연결 미확인 · 조작 잠김';$('status').textContent=states[p?.status]||'상태 확인 중';$('state-card').dataset.status=p?.status||'';
  $('reason').textContent=reasons[p?.reason]||p?.reason||'';
  $('knowledge').textContent=!p?'차량 상태를 아직 확인하지 못했습니다.':p.motionKnowledge==='unknown'?'마지막 보고 이후의 이동 결과는 미확인입니다.':p?.motionKnowledge==='reported_stationary'?'가상 차량의 완료·정지 보고가 확인됐습니다.':'아직 이동 명령을 보내지 않았습니다.';
  $('traffic-status').textContent=safety[s?.safety]||'확인 중';$('traffic-status').dataset.safety=s?.safety||'';
  $('traffic-detail').textContent=s?`관측 ${p.traffic?.revision||0}판 · 12초 예측 · ${s.safety==='clearing'?`해소 확인 ${s.clearRemainingMillis}ms 남음`:s.riskId?`위험 대상 ${s.riskId} / ${s.riskVehicle}`:'차량 응답과 별도로 검사'}`:'';
  $('command').textContent=!p?'최신 계획을 확인하고 있습니다.':['stopped','cancelled'].includes(p.status)?'계획이 종료됐습니다. 계속하려면 새 계획이 필요합니다.':c?`다음/현재 이동: ${c.vehicle} · ${Math.abs(c.distance).toFixed(2)}m · ${c.distance<0?'후진':c.distance>0?'전진':'정지 조향'} · ${p.index+1}/${p.commands.length}`:'모든 명령을 확인했습니다.';
  $('position-note').textContent=p?.motionKnowledge==='unknown'?'표시 위치는 마지막 보고입니다. 정지 요청을 정지 완료로 표시하지 않습니다.':'차량이 보고한 가상 자세를 표시합니다.';
  $('clock').textContent=current?`수동 가상 시각 ${(current.virtualTimeMillis/1000).toFixed(2)}초`:'';
  $('feedback').textContent=notice||(pending?'처리 결과 미확인 · 같은 요청으로 재확인하세요.':'각 버튼은 가상 시험 사건을 하나씩 진행합니다.');$('retry').hidden=!pending;$('retry').disabled=busy;
  for(const b of document.querySelectorAll('[data-action]'))b.disabled=locked;
  $('next').disabled=locked||!['ready','running'].includes(p?.status);$('next').textContent=c?.status==='executing'?'차량 완료 보고 받기':'다음 명령 발행';
  $('confirm-stop').disabled=locked||p?.status!=='stop_requested';
  $('budgets').replaceChildren();for(const [v,u] of Object.entries(current?.usage||{})){const tr=document.createElement('tr');for(const value of [v,current.grants[v].version,u.used.toFixed(2),u.reserved.toFixed(2),current.grants[v].total.toFixed(2)]){const td=document.createElement('td');td.textContent=value;tr.append(td);}$('budgets').append(tr);}
  $('events').replaceChildren();for(const e of (current?.events||[]).slice(-14).reverse()){const li=document.createElement('li');li.textContent=`${e.sequence}. ${kinds[e.kind]||e.kind}${e.vehicle?' · '+e.vehicle:''}${e.safety?' · '+(safety[e.safety]||e.safety):''}`;$('events').append(li);}
  $('identifiers').textContent=current?`시험 ${current.demoId} · 화면 ${current.revision}판 · 계획 ${p.id}`:'';draw();
 }
 async function poll(){if(polling||busy)return;polling=true;const epoch=generation;try{const data=await request('/api/state?poll='+Date.now());if(epoch===generation){current=data;online=true;last=Date.now();}}catch{if(epoch===generation)online=false;}finally{polling=false;render();}}
 async function send(){if(!pending||busy)return;generation++;busy=true;render();try{current=await request('/api/actions',pending);pending=null;localStorage.removeItem(key);online=true;last=Date.now();notice='가상 시험 결과를 확인했습니다.';}catch(e){online=false;if(e.status>=400&&e.status<500){pending=null;localStorage.removeItem(key);notice=e.status===409?'시험 상태가 바뀌었습니다. 최신 상태를 보고 다시 선택하세요.':'요청을 반영하지 못했습니다.';}else notice='처리 결과 확인 대기 · 추가 조작을 잠급니다.';}finally{busy=false;render();}}
 for(const b of document.querySelectorAll('[data-action]'))b.addEventListener('click',()=>{if(!current||pending||busy||!online)return;if(Date.now()-last>3000){online=false;notice='최신 상태를 확인한 뒤 다시 선택하세요.';render();poll();return;}const body={action:b.dataset.action,requestId:crypto.randomUUID(),serverEpoch:current.serverEpoch,demoId:current.demoId,revision:current.revision};try{localStorage.setItem(key,JSON.stringify(body));pending=body;notice='';send();}catch{notice='요청 보관에 실패해 전송하지 않았습니다.';render();}});
 $('retry').addEventListener('click',send);render();poll();setInterval(()=>{if(Date.now()-last>3000){online=false;render();}poll();},1000);
})();
