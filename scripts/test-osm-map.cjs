// Adapter contract checks; no network traffic, browser, or live-map claims.
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const config={site:JSON.parse(fs.readFileSync('coordination/data/sejong-arboretum.geojson','utf8')),tileTemplate:'https://tile.openstreetmap.org/{z}/{x}/{y}.png'};
function harness(){
  const events={},layers=[],statuses=[],timers=new Map();let timerId=0,removed=false,tileOptions,tileUrl,fitCount=0,selection;
  const shape=()=>({events:{},style:{},on(name,fn){this.events[name]=fn;},setStyle(s){this.style={...this.style,...s};},bindTooltip(label){this.label=label.textContent;}});
  const map={attributionControl:{setPrefix(){}},invalidateSize(){},fitBounds(){fitCount++;},remove(){removed=true;}};
  const tiles={on(name,fn){events[name]=fn;return this;},addTo(){events.loading();return this;}};
  const L={map:()=>map,circleMarker:()=>shape(),tileLayer:(url,options)=>{tileUrl=url;tileOptions=options;return tiles;},geoJSON:(site,options)=>{
    for(const f of site.features){const layer=shape();layer.style=options.style(f);layers.push(layer);options.onEachFeature(f,layer);}
    return {addTo(){return this;},getBounds(){return config.site.bbox;}};
  }};
  const context={window:{L},document:{createElement:()=>({textContent:''})},setTimeout:fn=>{const id=++timerId;timers.set(id,fn);return id;},clearTimeout:id=>timers.delete(id),Map};
  vm.runInNewContext(fs.readFileSync('coordination/admin/osm-map.js','utf8'),context);
  return {create:()=>context.window.SwarmOSMMap.create({},config,id=>selection=id,s=>statuses.push(s)),events,layers,statuses,timers,
    get removed(){return removed;},get tileOptions(){return tileOptions;},get tileUrl(){return tileUrl;},get fitCount(){return fitCount;},get selection(){return selection;}};
}
test('visible OSM attribution, exact tile endpoint, and no prefetch buffer',async()=>{
 const h=harness();await h.create();assert.equal(h.tileUrl,config.tileTemplate);assert.match(h.tileOptions.attribution,/OpenStreetMap contributors/);assert.equal(h.tileOptions.keepBuffer,0);assert.equal(h.tileOptions.referrerPolicy,'strict-origin-when-cross-origin');
});
test('tile failure remains degraded even after the batch completes',async()=>{
 const h=harness();await h.create();h.events.tileerror();h.events.load();assert.equal(h.statuses.at(-1),'degraded');assert.equal(h.timers.size,0);
});
test('a later successful tile batch can restore ready state',async()=>{
 const h=harness();await h.create();h.events.tileerror();h.events.load();h.events.loading();h.events.load();assert.equal(h.statuses.at(-1),'ready');
});
test('no tile completion produces a bounded timeout',async()=>{
 const h=harness();await h.create();assert.equal(h.timers.size,1);[...h.timers.values()][0]();assert.equal(h.statuses.at(-1),'error');
});
test('selection highlights geometry without inventing occupancy, and map click shares selection',async()=>{
 const h=harness(),adapter=await h.create();adapter.select(config.site.features[0].id);assert.equal(h.layers[0].style.weight,4);assert.equal(h.layers[1].style.weight,2);h.layers[1].events.click();assert.equal(h.selection,config.site.features[1].id);assert.equal(h.layers[0].label,config.site.features[0].properties.label);
});
test('close tears down map/timers and suppresses late tile events',async()=>{
 const h=harness(),adapter=await h.create();adapter.destroy();const before=h.statuses.length;h.events.load();h.events.tileerror();assert.equal(h.statuses.length,before);assert.equal(h.timers.size,0);assert.equal(h.removed,true);
});
