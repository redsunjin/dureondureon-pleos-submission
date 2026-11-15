async (page) => {
  const checks=[];function check(name,condition){if(!condition)throw new Error(name);checks.push({check:name,passed:true});}
  const button=name=>page.getByRole('button',{name,exact:true});
  const cell=id=>page.getByRole('button',{name:new RegExp('^'+id+' · ')});
  const role=(type,name)=>page.getByRole(type,{name,exact:true});
  const state=async()=>await(await page.request.get('http://127.0.0.1:8770/v1/vacancy/state?eventId=festival-demo')).json();
  if(!await page.locator('#observation-editor').evaluate(el=>el.open)) await page.locator('#observation-editor summary').click();
  await cell('B-02').click();await role('combobox','관측 출처').selectOption('STAFF-1');await role('combobox','관측 결과').selectOption('vacant');await role('combobox','관측 시점').selectOption('0');await role('spinbutton','신뢰도').fill('0.95');await role('spinbutton','유효 시간(초)').fill('120');
  let dropped=false,releaseRetry=false,accepted=null;const sent=[];
  await page.route('**/v1/vacancy/observations',async route=>{
    sent.push(route.request().postDataJSON());
    if(!dropped){dropped=true;const r=await route.fetch();accepted=await r.json();await route.abort('failed');}
    else if(!releaseRetry)await route.abort('failed');else await route.continue();
  });
  await button('관측 등록').click();
  await page.waitForFunction(()=>!document.getElementById('retry').hidden && document.getElementById('feedback').textContent.includes('요청 결과 확인 대기'));
  const pending=await page.evaluate(()=>JSON.parse(localStorage.getItem('swarm-vacancy-pending-v1')));
  check('Lost reply preserves request UUID locally',Boolean(pending?.body.requestId));
  check('Unknown write result locks further mutations',await button('관측 등록').isDisabled()&&await button('선택 공간으로 안내').isDisabled());
  await page.screenshot({path:'output/playwright/vacancy-pending-reply.png',fullPage:true});
  releaseRetry=true;const replay=page.waitForResponse(r=>r.url().endsWith('/observations')&&r.request().method()==='POST'&&r.status()===200);
  await page.reload();await replay;
  await page.waitForFunction(()=>document.getElementById('retry').hidden && document.getElementById('feedback').textContent.includes('관측을 저장'));
  check('Reload recovers the exact original UUID rather than creating a new write',sent.length>=2 && sent.every(p=>p.requestId===pending.body.requestId));
  check('Recovered mutation is a single accepted observation',(await state()).events.filter(e=>e.event==='observation'&&e.subject==='B-02').length===1 && Boolean(accepted.actionResult.id));
  check('Confirmed receipt clears local pending record',await page.evaluate(()=>localStorage.getItem('swarm-vacancy-pending-v1'))===null);
  await page.unroute('**/v1/vacancy/observations');
  await page.locator('#observation-editor summary').click();
  await cell('B-02').click();await page.waitForFunction(()=>!document.getElementById('assign').disabled);
  await page.context().setOffline(true);
  await page.waitForFunction(()=>document.getElementById('connection').textContent.includes('끊김'));
  check('Offline page stops authorizing an otherwise vacant space',await button('선택 공간으로 안내').isDisabled()&&await button('관측 등록').isDisabled());
  check('Offline counts are not presented as live vacancy',await page.locator('#count-available').textContent()==='—');
  await page.screenshot({path:'output/playwright/vacancy-offline.png',fullPage:true});
  await page.context().setOffline(false);await button('다시 확인').click();
  await page.waitForFunction(()=>document.getElementById('connection').textContent==='관제 연결됨'&&!document.getElementById('assign').disabled);
  check('Reconnection restores authority only from a fresh server response',await button('선택 공간으로 안내').isEnabled());
  await page.setViewportSize({width:390,height:844});
  await page.getByRole('heading',{name:'가을 축제',exact:true}).scrollIntoViewIfNeeded();
  check('Small screen has no horizontal overflow',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await cell('B-03').click();check('Small screen selection reaches the same inspector',await page.locator('#selected-title').textContent()==='B-03');
  await button('관측 등록').scrollIntoViewIfNeeded();check('Small screen observation controls remain reachable',await button('관측 등록').isVisible());
  await page.screenshot({path:'output/playwright/vacancy-mobile.png',fullPage:true});
  await page.setViewportSize({width:1440,height:1000});
  return {passed:checks.length,recoveredRequestId:pending.body.requestId,observationId:accepted.actionResult.id,checks};
}
