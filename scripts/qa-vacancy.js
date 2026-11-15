async (page) => {
  const checks=[];
  function check(name,condition){if(!condition)throw new Error(name);checks.push({check:name,passed:true});}
  const button=name=>page.getByRole('button',{name,exact:true});
  const field=name=>page.getByRole(['관측 출처','관측 결과','관측 방향','관측 시점','표시 구역'].includes(name)?'combobox':name==='차량 표식'?'textbox':'spinbutton',{name,exact:true});
  const cell=id=>page.getByRole('button',{name:new RegExp('^'+id+' · ')});
  const state=async()=>await(await page.request.get('http://127.0.0.1:8770/v1/vacancy/state?eventId=festival-demo')).json();
  async function settled(){await page.waitForFunction(()=>document.getElementById('retry').hidden && !document.getElementById('observe').disabled);}
  async function post(buttonName,path){const response=page.waitForResponse(r=>r.url().endsWith(path)&&r.request().method()==='POST');await button(buttonName).click();const r=await response;if(!r.ok())throw new Error(await r.text());await page.waitForFunction(()=>document.getElementById('retry').hidden);return await r.json();}
  async function observe(){return await post('관측 등록','/v1/vacancy/observations');}
  async function status(id,value){await page.waitForFunction(({id,value})=>document.querySelector(`[data-space="${id}"]`).getAttribute('aria-label')===`${id} · ${value}`,{id,value});}
  async function source(value){await field('관측 출처').selectOption(value);await page.locator('body').ariaSnapshot();}
  async function share(enabled){const cb=page.getByRole('checkbox',{name:/이 가상 차량의 공간 정보 공유 허용/});if(await cb.isChecked()!==enabled){const response=page.waitForResponse(r=>r.url().includes('/consent')&&r.request().method()==='POST');await cb.click();if(!(await response).ok())throw new Error('consent failed');await page.waitForFunction(()=>document.getElementById('retry').hidden);} }

  await page.waitForFunction(()=>document.getElementById('connection').textContent==='관제 연결됨');
  if(!await page.locator('#observation-editor').evaluate(el=>el.open)) await page.locator('#observation-editor summary').click();
  check('Unknown initial spaces cannot be dispatched',await button('선택 공간으로 안내').isDisabled());
  await cell('A-01').click();await source('STAFF-1');await field('관측 결과').selectOption('vacant');await field('유효 시간(초)').fill('120');await observe();await status('A-01','빈 공간');
  check('Fresh manual observation enables only its mapped space',(await state()).counts.available===1);
  await post('선택 공간으로 안내','/v1/vacancy/assignments');await status('A-01','안내 중');
  check('Dispatch does not pretend physical occupancy',(await state()).spaces.find(s=>s.id==='A-01').occupancy==='vacant');
  check('Repeated dispatch and unconfirmed arrival are disabled',await button('선택 공간으로 안내').isDisabled()&&await button('도착 확인').isDisabled());
  await field('관측 결과').selectOption('occupied');await observe();await status('A-01','점유');
  const arrival=page.waitForResponse(r=>/\/arrival$/.test(r.url()));await button('도착 확인').click();check('Arrival succeeds only after occupied evidence',(await arrival).ok());
  await page.waitForFunction(()=>document.getElementById('assignment').hidden);
  check('Arrival releases guide lease but keeps occupied state',(await state()).counts.assigned===0&&(await state()).spaces.find(s=>s.id==='A-01').occupancy==='occupied');

  await cell('A-02').click();await field('관측 결과').selectOption('vacant');await observe();await status('A-02','빈 공간');await field('차량 표식').fill('ARRIVAL-02');await post('선택 공간으로 안내','/v1/vacancy/assignments');
  const cancel=page.waitForResponse(r=>/\/cancel$/.test(r.url()));await button('안내 취소').click();await cancel;await status('A-02','빈 공간');check('Cancel releases the lease without changing observation',await button('선택 공간으로 안내').isEnabled());

  await cell('A-03').click();await source('CAR-1');check('Vehicle information sharing starts disabled',await button('관측 등록').isDisabled());await share(true);await settled();await field('관측 방향').selectOption('left');await observe();await status('A-03','빈 공간');
  await source('CAR-2');await share(true);await settled();await field('관측 방향').selectOption('right');await observe();
  check('Two car views count one canonical space',(await state()).counts.available===2&&(await state()).spaces.find(s=>s.id==='A-03').freshSourceCount===2);
  await field('관측 결과').selectOption('occupied');await observe();await status('A-03','확인 필요');check('Conflicting observations prevent guidance',await button('선택 공간으로 안내').isDisabled()&&(await page.locator('#selected-reason').textContent()).includes('충돌'));
  await share(false);await source('CAR-1');await share(false);await status('A-03','확인 필요');
  await share(true);await settled();check('Re-enabling consent does not reactivate old reports',await button('선택 공간으로 안내').isDisabled());
  await field('관측 결과').selectOption('vacant');await observe();await status('A-03','빈 공간');check('New observation under renewed sharing can restore availability',await button('선택 공간으로 안내').isEnabled());
  await field('관측 결과').selectOption('unknown');await observe();await status('A-03','확인 필요');check('Occlusion replaces previous empty report without fallback',await button('선택 공간으로 안내').isDisabled());

  await cell('A-04').click();await source('STAFF-1');await field('관측 결과').selectOption('vacant');await field('신뢰도').fill('0.5');await observe();await status('A-04','확인 필요');check('Low confidence never authorizes dispatch',await button('선택 공간으로 안내').isDisabled());
  await field('신뢰도').fill('0.95');await field('유효 시간(초)').fill('2');await observe();await status('A-04','빈 공간');await status('A-04','확인 필요');check('Real TTL expiry removes green vacancy and disables dispatch',await button('선택 공간으로 안내').isDisabled());
  await page.screenshot({path:'output/playwright/vacancy-expired.png',fullPage:true});
  await field('유효 시간(초)').fill('120');await cell('A-02').click();await post('구역 폐쇄','/v1/vacancy/zones/A/status');await status('A-02','구역 폐쇄');await observe();check('New vacant report cannot override operator closure',await button('선택 공간으로 안내').isDisabled());
  await post('구역 다시 열기','/v1/vacancy/zones/A/status');await status('A-02','빈 공간');check('Explicit reopening restores only fresh evidence',await button('선택 공간으로 안내').isEnabled());
  await field('표시 구역').selectOption('B');check('Zone filter shows B and hides A',await cell('B-01').isVisible()&&!await cell('A-01').isVisible());await field('표시 구역').selectOption('all');
  await cell('A-05').click();await source('CAR-3');await share(true);await settled();await field('관측 시점').selectOption('90');await field('유효 시간(초)').fill('60');await observe();await status('A-05','확인 필요');check('Delayed first report is stored but never treated as current vacancy',(await state()).spaces.find(s=>s.id==='A-05').reason==='stale');
  await field('관측 시점').selectOption('0');await field('차량 표식').fill('FORM-STAYS');await page.waitForTimeout(1200);check('Live polling preserves form input',await field('차량 표식').inputValue()==='FORM-STAYS');
  await page.screenshot({path:'output/playwright/vacancy-ledger.png',fullPage:true});
  return {passed:checks.length,checks};
}
