'use strict';
// Optional provider. No SDK request is made until an operator opens the review map.
(() => {
  let loading;
  function sdk(key) {
    if (window.kakao?.maps?.Map) return Promise.resolve(window.kakao.maps);
    if (loading) return loading;
    loading = new Promise((resolve,reject) => {
      const script=document.createElement('script');
      let finished=false;
      const finish=(error)=>{
        if(finished)return;finished=true;clearTimeout(timeout);
        script.onload=null;script.onerror=null;
        if(error){script.remove();reject(error);}else resolve(window.kakao.maps);
      };
      const timeout=setTimeout(()=>finish(new Error('sdk_timeout')),12000);
      script.src='https://dapi.kakao.com/v2/maps/sdk.js?autoload=false&appkey='+encodeURIComponent(key);
      script.async=true;
      script.onerror=()=>finish(new Error('sdk_load_failed'));
      script.onload=()=>{if(window.kakao?.maps?.load)window.kakao.maps.load(()=>finish());else finish(new Error('sdk_unavailable'));};
      document.head.append(script);
    }).catch(error=>{loading=null;throw error;});
    return loading;
  }
  window.SwarmKakaoMap = {async create(container,config,onSelect,onStatus,isCurrent=()=>true) {
    const K=await sdk(config.publicKey), site=config.site;
    if(!isCurrent())throw new Error('review_closed');
    const [west,south,east,north]=site.bbox;
    const map=new K.Map(container,{center:new K.LatLng((south+north)/2,(west+east)/2),level:3});
    const overlays=new Map(),listeners=[];
    const point=([lng,lat])=>new K.LatLng(lat,lng);
    let timer,disposed=false;
    const bounds=new K.LatLngBounds(point([west,south]),point([east,north]));
    const event=(target,name,fn)=>{K.event.addListener(target,name,fn);listeners.push([target,name,fn]);};
    const waiting=()=>{if(disposed)return;onStatus('loading');clearTimeout(timer);timer=setTimeout(()=>{if(!disposed)onStatus('error');},12000);};
    event(map,'tilesloaded',()=>{clearTimeout(timer);if(!disposed)onStatus('ready');});
    event(map,'maptypeid_changed',waiting);
    for(const feature of site.features){
      const g=feature.geometry,p=feature.properties;
      const color=p.kind==='closure'?'#a13f37':p.verification==='verified'?'#436574':'#9a6616';
      const options={map,strokeWeight:2,strokeColor:color,strokeOpacity:1,strokeStyle:p.verification==='verified'?'solid':'dash',fillColor:color,fillOpacity:.12};
      let shape;
      if(g.type==='Polygon')shape=new K.Polygon({...options,path:g.coordinates[0].map(point)});
      else if(g.type==='LineString')shape=new K.Polyline({...options,path:g.coordinates.map(point)});
      else shape=new K.Circle({...options,center:point(g.coordinates),radius:6});
      overlays.set(feature.id,{shape,color});
      event(shape,'click',()=>onSelect(feature.id));
    }
    map.addControl(new K.ZoomControl(),K.ControlPosition.RIGHT);
    waiting();map.relayout();map.setBounds(bounds,40,40,40,40);
    return {
      select(id){for(const [key,item] of overlays)item.shape.setOptions({strokeWeight:key===id?4:2,strokeColor:key===id?'#17323d':item.color});},
      focus(id){const g=site.features.find(f=>f.id===id)?.geometry;if(!g)return;if(g.type==='Point'){map.panTo(point(g.coordinates));return;}const b=new K.LatLngBounds();for(const p of (g.type==='Polygon'?g.coordinates[0]:g.coordinates))b.extend(point(p));map.setBounds(b,40,40,40,40);},
      setBase(value){waiting();map.setMapTypeId(value==='satellite'?K.MapTypeId.HYBRID:K.MapTypeId.ROADMAP);},
      fit(){map.relayout();map.setBounds(bounds,40,40,40,40);},
      destroy(){disposed=true;clearTimeout(timer);for(const [target,name,fn] of listeners)K.event.removeListener(target,name,fn);for(const {shape} of overlays.values())shape.setMap(null);container.replaceChildren();}
    };
  }};
})();
