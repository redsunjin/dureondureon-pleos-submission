'use strict';
(() => {
  async function create(container,cfg,onSelect,onStatus){
    const L=window.L;if(!L)throw new Error('library_unavailable');
    const map=L.map(container,{zoomControl:true,scrollWheelZoom:false,preferCanvas:true});
    map.attributionControl.setPrefix('<a href="https://leafletjs.com" target="_blank" rel="noopener noreferrer">Leaflet</a>');
    const layers=new Map();let timer,disposed=false,errors=0;
    const color=f=>f.properties.kind==='closure'?'#a13f37':f.properties.verification==='verified'?'#436574':'#9a6616';
    const waiting=()=>{if(disposed)return;errors=0;onStatus('loading');clearTimeout(timer);timer=setTimeout(()=>{if(!disposed)onStatus('error');},12000);};
    const tiles=L.tileLayer(cfg.tileTemplate,{maxZoom:19,keepBuffer:0,updateWhenIdle:true,updateWhenZooming:false,
      attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener noreferrer">OpenStreetMap contributors</a>',
      referrerPolicy:'strict-origin-when-cross-origin'});
    tiles.on('loading',waiting);
    tiles.on('tileerror',()=>{errors++;if(!disposed)onStatus('degraded');});
    tiles.on('load',()=>{clearTimeout(timer);if(!disposed)onStatus(errors?'degraded':'ready');});
    const geo=L.geoJSON(cfg.site,{style:f=>({color:color(f),weight:2,dashArray:f.properties.verification==='verified'?null:'7 5',fillOpacity:.1}),
      pointToLayer:(f,latlng)=>L.circleMarker(latlng,{radius:7,color:color(f),fillOpacity:.2,weight:2}),
      onEachFeature:(f,layer)=>{layers.set(f.id,{layer,feature:f});layer.on('click',()=>onSelect(f.id));const label=document.createElement('span');label.textContent=f.properties.label;layer.bindTooltip(label);}
    }).addTo(map);
    const fit=()=>{map.invalidateSize();map.fitBounds(geo.getBounds(),{padding:[35,35],maxZoom:17,animate:false});};
    fit();waiting();tiles.addTo(map);
    return {
      select(id){for(const [key,{layer,feature}] of layers)layer.setStyle({weight:key===id?4:2,color:key===id?'#17323d':color(feature)});},
      focus(id){const layer=layers.get(id)?.layer;if(!layer)return;if(layer.getBounds)map.fitBounds(layer.getBounds(),{padding:[40,40],maxZoom:19,animate:false});else map.panTo(layer.getLatLng());},
      fit,
      destroy(){disposed=true;clearTimeout(timer);map.remove();}
    };
  }
  window.SwarmOSMMap={create};
})();
