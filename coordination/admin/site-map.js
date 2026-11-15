'use strict';
(() => {
  const $=id=>document.getElementById(id),dialog=$('site-dialog');
  const kindLabels={access:'도로 접속 후보',reference:'위치 참고점',zone:'구역',row:'주차열',space:'주차면',aisle:'통로',entrance:'입구',exit:'출구',closure:'폐쇄 영역'};
  let config=null,adapter=null,generation=0,selected=null,configController=null,base='road';
  const say=(value)=>{$('site-map-status').textContent=value;};
  function mapStatus(state){
    $('site-map-state').textContent={loading:'지도 불러오는 중',ready:'배경 지도 연결됨',error:'지도 연결 오류 · 목록으로 확인',degraded:'일부 지도 미수신 · 목록으로 확인'}[state];
    $('site-map-state').dataset.state=state;
    $('site-map-fallback').hidden=state==='ready';
    $('site-map-fallback').textContent=state==='loading'?'배경 지도를 불러오고 있습니다.':state==='ready'?'':'배경 지도를 받지 못했습니다. 오른쪽 목록과 출처를 확인하세요.';
    $('site-fit').disabled=state!=='ready';$('site-focus').disabled=state!=='ready';
    $('site-base').disabled=state!=='ready'||config?.provider!=='kakao';
    $('site-load').disabled=state==='loading';
    if(state==='ready')$('site-load').textContent='지도 새로 불러오기';
    if(state==='error'||state==='degraded')$('site-load').textContent='지도 다시 불러오기';
  }
  function detail(id){
    const f=config?.site?.features.find(f=>f.id===id);if(!f)return;
    selected=id;const p=f.properties;
    $('site-selected').textContent=p.label;
    $('site-kind').textContent=kindLabels[p.kind];
    $('site-verification').textContent=p.verification==='verified'?'경계 확인 기록 있음 · 점유 미확인':'현장 확인 필요';
    $('site-direction').textContent=p.kind==='aisle'?({forward:'좌표 순방향 단방향 표기',backward:'좌표 역방향 단방향 표기',both:'양방향 표기',unknown:'방향 미등록'}[p.direction||'unknown']+' · 현장 미확인'):'입출구·통행 방향 미확인';
    window.SwarmSiteReviews?.select(id);
    $('site-accuracy').textContent=p.accuracyMeters===null?'미측정':p.accuracyMeters+'m';
    $('site-feature-source').textContent=p.source;
    $('site-source-updated').textContent=p.sourceUpdatedAt?p.sourceUpdatedAt.slice(0,10):'기록 없음';
    $('site-feature-link').href=p.sourceUrl;
    $('site-feature-version').textContent=String(p.mapVersion);
    $('site-coordinates').textContent=f.geometry.type==='Point'?`위도 ${f.geometry.coordinates[1].toFixed(6)} · 경도 ${f.geometry.coordinates[0].toFixed(6)}`:f.geometry.type==='Polygon'?`다각형 · ${f.geometry.coordinates[0].length-1}개 꼭짓점`:`통로 · ${f.geometry.coordinates.length}개 점`;
    for(const button of $('site-features').querySelectorAll('button'))button.setAttribute('aria-pressed',String(button.dataset.feature===id));
    adapter?.select(id);
  }
  function showConfig(){
    const site=config.site;
    $('site-survey-export').hidden=!site;
    if(site)$('site-survey-export').href='/v1/site-map/survey?layout='+encodeURIComponent(site.geometryDigest);
    base='road';$('site-base').value=base;
    $('site-name').textContent=site?.label||'현장 위치 미설정';
    $('site-address').textContent=site?.address||'검토할 장소와 구역 자료를 먼저 등록해 주세요.';
    $('site-meta').textContent=site?`현장 배치 ${site.mapVersion}판 · ${site.features.length}개 항목 · 조회 ${site.updatedAt.slice(0,10)}`:'현장 자료 없음';
    $('site-provider').textContent=config.provider==='kakao'?'카카오 지도':'OpenStreetMap';
    $('site-base').disabled=true;
    $('site-base').querySelector('option[value=satellite]').disabled=config.provider!=='kakao';
    $('site-base-note').textContent=config.provider==='kakao'?'위성사진은 실시간 관측이 아닙니다.':'위성사진 미제공 · 일반 지도 배경';
    $('site-license').textContent=site?.license||'배경·구역 자료 미설정';
    $('site-data-source').hidden=!site;
    if(site)$('site-data-source').href=site.sourceUrl;
    $('site-features').replaceChildren();
    $('site-feature-detail').hidden=!site;
    $('site-kind-filter').value='all';window.SwarmSiteReviews?.setSite(site);
    for(const f of site?.features||[]){
      const button=document.createElement('button');button.type='button';button.dataset.feature=f.id;
      const title=document.createElement('strong');title.textContent=f.properties.label;
      const info=document.createElement('span');info.textContent=`${kindLabels[f.properties.kind]} · ${f.properties.verification==='verified'?'경계 확인 기록':'현장 미확인'}`;
      button.append(title,info);button.addEventListener('click',()=>detail(f.id));$('site-features').append(button);
    }
    filterFeatures();
    $('site-load').disabled=config.state!=='ready';
    $('site-load').textContent='배경 지도 불러오기';
    say(config.state==='missing_site'?'현장 구역 자료가 없습니다. 가상 행사장 데이터와 연결하지 않습니다.':config.state==='missing_key'?'카카오 지도 JavaScript 키가 설정되지 않았습니다. 구역 목록과 출처는 확인할 수 있습니다.':'구역은 위치 검토용입니다. 배경을 열어도 빈자리 관측이나 안내 권한은 생기지 않습니다.');
    $('site-map-state').textContent='배경 지도 대기';
    $('site-map-fallback').hidden=false;$('site-map-fallback').textContent=config.state==='missing_site'?'실제 위치를 등록하면 지도를 확인할 수 있습니다.':config.state==='missing_key'?'지도 키 설정이 필요합니다.':'배경 지도를 열어 구역 위치를 확인하세요.';
  }
  async function loadConfig(){
    configController?.abort();
    $('site-retry').disabled=true;
    $('site-load').disabled=true;$('site-fit').disabled=true;$('site-base').disabled=true;
    $('site-features').replaceChildren();$('site-feature-detail').hidden=true;
    $('site-name').textContent='현장 지도 설정 확인 중';window.SwarmSiteReviews?.reset();
    say('현장 지도 설정을 확인합니다.');
    const token=++generation;
    const controller=new AbortController(),timer=setTimeout(()=>controller.abort(),5000);
    configController=controller;
    try{
      const response=await fetch('/v1/site-map',{cache:'no-store',signal:controller.signal});
      if(!response.ok)throw new Error('config_unavailable');
      const next=await response.json();
      if(next.previewOnly!==true||!['osm','kakao'].includes(next.provider))throw new Error('config_invalid');
      if(token!==generation||!dialog.open)return;
      config=next;showConfig();
    }catch{
      if(token===generation&&dialog.open){say('현장 지도 설정을 받지 못했습니다. 다시 확인해 주세요.');$('site-load').disabled=true;$('site-map-state').textContent='설정 수신 실패';}
    }finally{clearTimeout(timer);if(configController===controller){configController=null;$('site-retry').disabled=false;}}
  }
  async function loadMap(){
    if(config?.state!=='ready')return;
    const token=++generation;adapter?.destroy();adapter=null;$('site-map-canvas').replaceChildren();
    mapStatus('loading');
    const status=value=>{if(token===generation&&dialog.open)mapStatus(value);};
    try{
      const factory=config.provider==='osm'?window.SwarmOSMMap.create:window.SwarmKakaoMap.create;
      const next=await factory($('site-map-canvas'),config,id=>{if(token===generation&&dialog.open)detail(id);},status,()=>token===generation&&dialog.open);
      if(token!==generation||!dialog.open){next.destroy();return;}
      adapter=next;adapter.select(selected);if(base==='satellite')adapter.setBase?.(base);
    }catch{if(token===generation&&dialog.open)mapStatus('error');}
  }
  function filterFeatures(){
    const kind=$('site-kind-filter').value;
    const visible=(config?.site?.features||[]).filter(f=>kind==='all'||f.properties.kind===kind);
    for(const b of $('site-features').querySelectorAll('button'))b.hidden=!visible.some(f=>f.id===b.dataset.feature);
    if(!visible.some(f=>f.id===selected)&&visible.length)detail(visible[0].id);
    else if(visible.length)detail(selected);
  }
  $('site-kind-filter').addEventListener('change',filterFeatures);
  $('site-focus').addEventListener('click',()=>adapter?.focus?.(selected));
  $('site-open').addEventListener('click',()=>{dialog.showModal();loadConfig();});
  $('site-close').addEventListener('click',()=>dialog.close());
  dialog.addEventListener('close',()=>{window.SwarmSiteReviews?.reset();generation++;configController?.abort();adapter?.destroy();adapter=null;$('site-map-canvas').replaceChildren();$('site-fit').disabled=true;$('site-base').disabled=true;base='road';$('site-base').value=base;});
  $('site-retry').addEventListener('click',()=>{generation++;adapter?.destroy();adapter=null;$('site-map-canvas').replaceChildren();loadConfig();});
  $('site-load').addEventListener('click',loadMap);
  $('site-fit').addEventListener('click',()=>adapter?.fit());
  $('site-base').addEventListener('change',()=>{base=$('site-base').value;adapter?.setBase?.(base);});
})();
