'use strict';
(()=>{
 const $=id=>document.getElementById(id),token=location.hash.slice(1),personal=!!token;
 const storageKey='curb-response-'+(token||'operator');
 let current=null,online=false,busy=false,pending=null,polling=false,generation=0;
 const statuses={awaiting:'동의 대기',ready:'전원 동의 · 시작 대기',running:'가상 이동 진행',stopped:'중단',expired:'만료',rejected:'거부로 종료',completed:'완료'};
 const reasons={awaiting_consent:'차량별 동의 대기',awaiting_reconsent:'새 계획에 재동의 필요',all_approved:'전원 동의 완료',running:'가상 이동 진행',consent_withdrawn:'차량 동의 철회',vehicle_rejected:'차량 참여 거부',participant_connection_lost:'참여 차량 응답 끊김',connection_lost:'관제 응답 끊김',plan_expired:'계획 유효 시간 만료',server_restarted:'서버 재시작',user_stopped:'관제에서 중단',completed:'완료',traffic_risk:'이동체 위험',traffic_stale:'이동체 관측 만료',environment_changed:'주변 배치 변경',traffic_cleared:'위험 해소 · 재계획 필요'};
 const decisions={pending:'응답 대기',approved:'동의',withdrawn:'철회',rejected:'거부'};
 const stages={create_space:'공간 확보',settle_spacing:'간격 정렬',approach:'접근',reverse:'후진',park:'주차',steering_at_rest:'정지 중 조향',reverse_in:'후진 진입',reverse_align:'후진 정렬',final_position:'최종 위치 조정'};
 try{pending=JSON.parse(sessionStorage.getItem(storageKey)||'null');}catch{}
 $('operator').hidden=personal;$('responses').hidden=!personal;
 const tell=t=>$('feedback').textContent=t;
 async function api(path,data){const c=new AbortController(),timer=setTimeout(()=>c.abort(),4500);try{const r=await fetch(path,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data),signal:c.signal,cache:'no-store'});const value=await r.json();if(!r.ok)throw Object.assign(new Error(value.error),{status:r.status});return value;}finally{clearTimeout(timer);}}
 function render(){
  const s=current?.session,p=current?.participant;
  if(!personal&&$('invitations').dataset.session!==s?.sessionId)$('invitations').replaceChildren();
  $('summary').textContent=s?`${statuses[s.status]||s.status} · ${reasons[s.reason]||s.reason} · 계획 ${s.sessionId} · 응답 버전 ${s.revision}`:'현재 계획 없음';
  $('title').textContent=personal?(p?`${p.vehicleId} · ${p.role}`:'차량 요청 확인'):'참여 차량 현황';
  $('plan').textContent=p?`예정 총 이동 ${p.totalTravel.toFixed(2)}m · 요청 ${p.moveRequestId}`:'';
  $('steps').replaceChildren();
  for(const step of p?.steps||[]){const row=document.createElement('p');row.textContent=`${stages[step.stage]||step.stage} · ${step.distance>=0?'전진':'후진'} ${Math.abs(step.distance).toFixed(2)}m · 가상 시간 ${step.start.toFixed(1)}–${step.end.toFixed(1)}초`;$('steps').append(row);}
  $('decision').textContent=p?`내 응답: ${decisions[p.decision]} · 전원 ${current.approvedCount}/${current.participantCount} 동의 · 내 진행 ${p.progressPercent.toFixed(0)}%`:s?s.participants.map(p=>`${p.vehicleId}: ${decisions[p.decision]}`).join(' / '):'';
  const locked=!online||busy||!!pending||!s;
  $('approve').disabled=$('reject').disabled=locked||s?.status!=='awaiting'||p?.decision!=='pending';
  $('withdraw').disabled=locked||!['awaiting','ready','running'].includes(s?.status)||p?.decision!=='approved';
  $('start').disabled=locked||s?.status!=='ready';$('stop').disabled=locked||!['awaiting','ready','running'].includes(s?.status);
  $('replan').disabled=locked||!['stopped','rejected','expired'].includes(s?.status);
  $('retry').hidden=!pending;$('retry').disabled=busy;
  $('create').disabled=$('restore').disabled=busy||!!pending;
  $('owner').disabled=busy||!!pending;
 }
 function invitations(data){if(!data.invitations)return;$('invitations').dataset.session=data.session.sessionId;$('invitations').replaceChildren();for(const inv of data.invitations){const a=document.createElement('a');a.className='participant-link';a.href='/participate/#'+inv.token;a.target='_blank';a.rel='noopener noreferrer';a.textContent=`${inv.vehicleId} 참여 화면 열기`;$('invitations').append(a);}}
 async function poll(){
  if(busy||polling)return;polling=true;const epoch=generation,owner=$('owner').value;
  try{let result;if(personal)result=await api('/v1/curb/participant/poll',{token});else{const r=await fetch('/v1/curb/state?ownerVehicleId='+encodeURIComponent(owner)+'&poll='+Date.now(),{cache:'no-store'});if(!r.ok)throw Error();result=await r.json();}
   if(epoch===generation&&owner===$('owner').value){current=result;online=true;if(!pending)tell('관제 연결됨 · 새 계획은 새 참여 링크에서 확인하세요.');}
  }catch{if(epoch===generation){online=false;tell('연결 확인 실패 · 응답 버튼 잠김 · 동의 차량의 응답 공백은 관제에서 중단 처리됩니다.');}}
  finally{polling=false;render();}
 }
 async function send(){if(!pending||busy)return;generation++;busy=true;render();try{const result=await api(pending.path,pending.body);current=result;invitations(result);pending=null;sessionStorage.removeItem(storageKey);online=true;tell('응답 결과를 확인했습니다.');}catch(e){online=false;if(e.status>=400&&e.status<500){pending=null;sessionStorage.removeItem(storageKey);tell(`요청이 반영되지 않았습니다 (${e.message}). 최신 요청을 확인한 뒤 다시 응답하세요.`);}else tell('결과 확인 대기 · 추가 응답은 잠깁니다. 같은 요청으로 결과를 다시 확인하세요.');}finally{busy=false;render();}}
 function queue(path,body){if(busy||pending)return;pending={path,body};try{sessionStorage.setItem(storageKey,JSON.stringify(pending));}catch{pending=null;tell('요청 보관 실패 · 전송하지 않았습니다.');return;}send();}
 for(const action of ['approve','reject','withdraw'])$(action).addEventListener('click',()=>{if(!current?.participant)return;queue('/v1/curb/participant/respond',{token,requestId:crypto.randomUUID(),revision:current.session.revision,moveRequestId:current.participant.moveRequestId,action});});
 $('retry').addEventListener('click',send);
 $('create').addEventListener('click',()=>queue('/v1/curb/sessions',{ownerVehicleId:$('owner').value,requestId:crypto.randomUUID()}));
 $('restore').addEventListener('click',async()=>{await poll();if(current?.session&&['awaiting','ready','running'].includes(current.session.status))queue('/v1/curb/participation',{sessionId:current.session.sessionId,ownerVehicleId:$('owner').value});});
 for(const action of ['start','stop'])$(action).addEventListener('click',()=>queue(`/v1/curb/sessions/${current.session.sessionId}/actions`,{ownerVehicleId:$('owner').value,requestId:crypto.randomUUID(),revision:current.session.revision,action}));
 $('replan').addEventListener('click',()=>queue(`/v1/curb/sessions/${current.session.sessionId}/replan`,{ownerVehicleId:$('owner').value,requestId:crypto.randomUUID(),revision:current.session.revision}));
 // Invitation setup follows create/replan only; it never casts participant votes.
 async function cycle(){await poll();if(!personal&&online&&!pending&&!busy&&current?.session?.status==='awaiting'&&!$('invitations').childElementCount){queue('/v1/curb/participation',{sessionId:current.session.sessionId,ownerVehicleId:$('owner').value});}}
 $('owner').addEventListener('change',()=>{generation++;$('invitations').replaceChildren();current=null;online=false;render();});
 render();cycle();setInterval(cycle,800);
})();
