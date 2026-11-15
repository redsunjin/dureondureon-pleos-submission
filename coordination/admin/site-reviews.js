'use strict';
(() => {
  const $=id=>document.getElementById(id),key='swarm-site-review-pending-v1';
  const statuses={needs_visit:'현장 확인 필요',evidence_added:'근거 기록',correction_needed:'수정 필요'};
  let site=null,selected=null,state=null,pending=null,posting=false,loading=false,epoch=0;
  try{pending=JSON.parse(localStorage.getItem(key)||'null');}catch{localStorage.removeItem(key);}
  const tell=(value)=>{$('site-review-feedback').textContent=value;};
  const matches=data=>site && data.siteId===site.siteId && data.mapVersion===site.mapVersion && data.geometryDigest===site.geometryDigest;
  function render(){
    const latest=state?.reviews.find(r=>r.featureId===selected);
    $('site-review-summary').textContent=latest?`${statuses[latest.status]} · ${latest.reviewer} · 기록 ${latest.revision}`:'이 배치에 검토 기록이 없습니다.';
    $('site-review-old').textContent=state?.previousLayoutNotes?`이전 배치 기록 ${state.previousLayoutNotes}건은 현재 지도에 적용하지 않습니다.`:'';
    $('site-review-submit').disabled=!site||!state||loading||posting||!!pending;
    $('site-review-retry').hidden=!pending;$('site-review-retry').disabled=posting;
    $('site-review-reload').disabled=loading||posting;
    $('site-review-target').textContent=site?.features.find(f=>f.id===selected)?.properties.label||'선택한 항목';
    $('site-review-history').replaceChildren();
    for(const r of (state?.history||[]).filter(r=>r.featureId===selected).slice(-5).reverse()){
      const entry=document.createElement('div');entry.className='site-review-entry';
      const title=document.createElement('strong');title.textContent=`${statuses[r.status]} · ${r.evidenceKind==='field_visit'?'현장 방문 기록':'자료 검토'} · ${r.reviewer}`;
      const note=document.createElement('p');note.textContent=r.note;
      const source=document.createElement('small');source.textContent=`확인 시점 ${new Date(r.observedAt).toLocaleString('ko-KR',{timeZone:'Asia/Seoul'})} · 근거 ${r.evidenceRef||'미등록'}`;
      entry.append(title,note,source);$('site-review-history').append(entry);
    }
  }
  async function request(options={}){
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    try{
      const r=await fetch('/v1/site-map/reviews',{cache:'no-store',...options,signal:controller.signal});
      const data=await r.json();if(!r.ok)throw Object.assign(new Error(data.error),{status:r.status,code:data.error});return data;
    }finally{clearTimeout(timer);}
  }
  async function load(){
    if(!site)return;const token=epoch;loading=true;render();
    try{const data=await request();if(token===epoch){if(!matches(data))throw new Error('map_changed');state=data;}}
    catch{if(token===epoch){state=null;tell('검토 기록을 받지 못했습니다. 다시 확인해 주세요.');}}
    finally{if(token===epoch){loading=false;render();}}
  }
  async function send(){
    if(!pending||posting)return;posting=true;const token=epoch;render();
    try{
      const data=await request({method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(pending)});
      pending=null;localStorage.removeItem(key);
      if(matches(data)){state=data;tell('검토 기록을 저장했습니다. 경계 확정이나 빈자리 배정 승인은 아닙니다.');}
    }catch(error){
      if(error.status>=400&&error.status<500){pending=null;localStorage.removeItem(key);if(token===epoch){tell(error.code==='site_review_map_changed'?'현장 배치가 변경됐습니다. 지도 설정을 다시 확인한 뒤 기록하세요.':error.code==='site_review_revision_changed'?'다른 검토 기록이 추가됐습니다. 최신 기록을 확인한 뒤 다시 저장하세요.':'기록을 저장하지 못했습니다. 확인 시점·근거·입력을 확인하세요.');await load();}}
      else if(token===epoch)tell('저장 결과 확인 대기 · 같은 요청으로 다시 확인합니다. 추가 저장은 잠깁니다.');
    }finally{posting=false;render();}
  }
  window.SwarmSiteReviews={
    setSite(next){epoch++;site=next;selected=next?.features[0]?.id;state=null;loading=false;tell('');render();if(site)load().then(()=>{if(pending&&site)send();});},
    select(id){if(selected!==id){$('site-review-note').value='';$('site-review-evidence').value='';$('site-review-time').value='';$('site-review-kind').value='document_review';$('site-review-result').value='needs_visit';}selected=id;render();},
    reset(){epoch++;site=null;state=null;loading=false;render();}
  };
  $('site-review-form').addEventListener('submit',event=>{
    event.preventDefault();if(!site||!state||posting||pending)return;
    const date=new Date($('site-review-time').value);if(!Number.isFinite(date.getTime()))return;
    const latest=state.reviews.find(r=>r.featureId===selected);
    const body={siteId:site.siteId,mapVersion:site.mapVersion,geometryDigest:site.geometryDigest,featureId:selected,
      requestId:crypto.randomUUID(),revision:latest?.revision||0,reviewer:$('site-reviewer').value.trim(),
      status:$('site-review-result').value,evidenceKind:$('site-review-kind').value,observedAt:date.toISOString(),
      evidenceRef:$('site-review-evidence').value.trim(),note:$('site-review-note').value.trim()};
    try{localStorage.setItem(key,JSON.stringify(body));pending=body;}catch{tell('요청을 기기에 보관하지 못해 전송하지 않았습니다.');return;}
    send();
  });
  $('site-review-retry').addEventListener('click',send);
  $('site-review-reload').addEventListener('click',load);
})();
