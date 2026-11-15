const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const crypto=require('node:crypto');
const os=require('node:os'),path=require('node:path'),child=require('node:child_process');
const tmp=fs.mkdtempSync(path.join(os.tmpdir(),'swarm-lab-test-'));
const fixturePath=path.join(tmp,'states.json');
child.execFileSync('python3',['-m','coordination.execution_demo','--export-tests',fixturePath]);
const fixtures=JSON.parse(fs.readFileSync(fixturePath,'utf8'));
fs.rmSync(tmp,{recursive:true});
const code=fs.readFileSync('coordination/admin/execution-lab.js','utf8');
class Element{
 constructor(){this.textContent='';this.disabled=false;this.hidden=false;this.dataset={};this.children=[];this.listeners={};}
 setAttribute(k,v){this[k]=v;}append(...v){this.children.push(...v);}replaceChildren(...v){this.children=[...v];}addEventListener(k,v){this.listeners[k]=v;}
}
const settle=()=>new Promise(resolve=>setImmediate(resolve));
function harness(fetch,initialPending){
 const html=fs.readFileSync('coordination/admin/execution-lab.html','utf8'),nodes={};
 for(const match of html.matchAll(/id="([^"]+)"/g))nodes[match[1]]=new Element();
 const buttons=['next','risk','stale','clear','confirm-stop','reset'].map(action=>{const e=nodes[action]||new Element();e.dataset.action=action;return e;});
 const store=new Map();if(initialPending)store.set('swarm-execution-lab-pending',JSON.stringify(initialPending));
 const intervals=[];
 const context={document:{getElementById:id=>nodes[id],querySelectorAll:()=>buttons,createElement:()=>new Element(),createElementNS:()=>new Element()},localStorage:{getItem:k=>store.get(k)||null,setItem:(k,v)=>store.set(k,v),removeItem:k=>store.delete(k)},fetch,AbortController,setTimeout,clearTimeout,setInterval:fn=>intervals.push(fn),Date,crypto:{randomUUID:crypto.randomUUID},console};
 vm.runInNewContext(code,context);
 return {nodes,buttons,store,intervals,click:action=>buttons.find(b=>b.dataset.action===action).listeners.click(),retry:()=>nodes.retry.listeners.click()};
}
const response=(data,status=200)=>({ok:status===200,status,json:async()=>structuredClone(data)});

test('a clear observation cannot unlock a plan waiting for vehicle stop confirmation',async()=>{
 const queue=[fixtures.ready,fixtures.unconfirmed,fixtures.risk,fixtures.cleared,fixtures.stopped];
 const h=harness(async()=>response(queue.shift()));await settle();assert.equal(h.nodes.next.disabled,false);
 h.click('next');await settle();assert.match(h.nodes.knowledge.textContent,/미확인/);
 h.click('risk');await settle();assert.equal(h.nodes.next.disabled,true);assert.equal(h.nodes['confirm-stop'].disabled,false);
 h.click('clear');await settle();assert.match(h.nodes['traffic-status'].textContent,/위험 없음/);assert.equal(h.nodes.next.disabled,true);assert.equal(h.nodes['confirm-stop'].disabled,false);
 h.click('confirm-stop');await settle();assert.equal(h.nodes['confirm-stop'].disabled,true);assert.match(h.nodes.status.textContent,/정지 확인/);
});

test('an uncertain POST locks new actions and retries the identical request',async()=>{
 let attempts=[],lose=true;
 const h=harness(async(path,options)=>{
  if(!options.method)return response(fixtures.ready);
  attempts.push(JSON.parse(options.body));if(lose){lose=false;throw Error('response lost');}return response(fixtures.unconfirmed);
 });await settle();h.click('next');await settle();assert.equal(h.nodes.next.disabled,true);assert.equal(h.nodes.retry.hidden,false);
 const original=h.store.get('swarm-execution-lab-pending');h.retry();await settle();assert.deepEqual(attempts[0],attempts[1]);assert.ok(original);assert.equal(h.store.size,0);
});

test('restored pending request is not automatically sent or replaced',async()=>{
 const pending={action:'next',requestId:crypto.randomUUID(),serverEpoch:'old',demoId:'old',revision:0};let posts=0;
 const h=harness(async(path,options)=>{if(options.method){posts++;return response({error:'lab_instance_changed'},409);}return response(fixtures.ready);},pending);
 await settle();assert.equal(posts,0);assert.equal(h.nodes.next.disabled,true);h.retry();await settle();assert.equal(posts,1);assert.equal(h.store.size,0);
});

test('stale in-flight GET cannot redraw a completed POST',async()=>{
 let read=0,resolveOld;
 const h=harness(async(path,options)=>{if(options.method)return response(fixtures.unconfirmed);if(++read===1)return response(fixtures.ready);return new Promise(resolve=>resolveOld=resolve);});
 await settle();h.intervals[0]();h.click('next');await settle();assert.match(h.nodes.knowledge.textContent,/미확인/);
 resolveOld(response(fixtures.ready));await settle();assert.match(h.nodes.knowledge.textContent,/미확인/);
});

test('network failure and malformed successful JSON never enable controls',async()=>{
 for(const fetch of [async()=>{throw Error('offline');},async()=>response({simulationOnly:true,plan:{status:'running'}})]){
  const h=harness(fetch);await settle();assert.equal(h.nodes.next.disabled,true);assert.match(h.nodes.connection.textContent,/미확인/);assert.doesNotMatch(h.nodes.command.textContent,/모든 명령/);
 }
});

test('a late or conflicting command response needs another explicit user action',async()=>{
 let posts=0;
 const h=harness(async(path,options)=>{if(options.method){posts++;return response({error:'lab_view_changed'},409);}return response(fixtures.ready);});
 await settle();h.click('next');await settle();assert.equal(posts,1);h.intervals[0]();await settle();assert.equal(posts,1);assert.equal(h.store.size,0);
});
