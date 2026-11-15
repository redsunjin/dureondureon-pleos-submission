async (page) => {
  const checks=[],errors=[];function check(name,condition){if(!condition)throw new Error(name);checks.push({check:name,passed:true});}
  page.on('pageerror',e=>errors.push(e.message));
  await page.reload();await page.waitForFunction(()=>document.getElementById('connection').textContent==='관제 연결됨');
  await page.locator('#observation-editor summary').click();
  const arrival=page.getByRole('textbox',{name:'차량 표식',exact:true});
  await arrival.fill('INVALID SPACE');check('Browser rejects invalid arrival identifiers',await arrival.evaluate(el=>!el.checkValidity()));
  await arrival.fill('DEMO-99');check('Browser accepts valid arrival identifiers without pattern errors',await arrival.evaluate(el=>el.checkValidity()));
  const source=page.getByRole('combobox',{name:'관측 출처',exact:true});await source.selectOption('STAFF-1');
  await page.getByRole('spinbutton',{name:'유효 시간(초)',exact:true}).fill('120');
  for(const [id,state] of [['A-02','vacant'],['C-01','vacant'],['C-02','vacant'],['C-03','occupied'],['C-04','blocked']]){
    await page.getByRole('button',{name:new RegExp('^'+id+' · ')}).click();
    await page.getByRole('combobox',{name:'관측 결과',exact:true}).selectOption(state);
    const response=page.waitForResponse(r=>r.url().endsWith('/observations')&&r.request().method()==='POST');
    await page.getByRole('button',{name:'관측 등록',exact:true}).click();if(!(await response).ok())throw new Error('fixture observation failed');
    await page.waitForFunction(()=>document.getElementById('retry').hidden);
  }
  await page.getByRole('button',{name:/^B-01 · /}).click();
  if(await page.getByRole('button',{name:'구역 폐쇄',exact:true}).isVisible()){
    const response=page.waitForResponse(r=>r.url().endsWith('/zones/B/status'));
    await page.getByRole('button',{name:'구역 폐쇄',exact:true}).click();await response;await page.waitForFunction(()=>document.getElementById('retry').hidden);
  }
  await page.getByRole('button',{name:/^C-01 · /}).click();
  await page.getByRole('heading',{name:'가을 축제',exact:true}).scrollIntoViewIfNeeded();
  await page.screenshot({path:'output/playwright/vacancy-manager-final.png',fullPage:true});
  await page.setViewportSize({width:390,height:844});await page.getByRole('heading',{name:'가을 축제',exact:true}).scrollIntoViewIfNeeded();
  check('Final compact layout remains within small viewport',await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  await page.screenshot({path:'output/playwright/vacancy-manager-mobile-final.png',fullPage:true});await page.setViewportSize({width:1440,height:1000});
  const result=page.waitForResponse(r=>r.url().endsWith('/assignments')&&r.request().method()==='POST');
  await page.getByRole('button',{name:'선택 공간으로 안내',exact:true}).click();const response=await result;const data=await response.json();check('Final valid guide form reaches the actual server',response.ok());
  check('No uncaught JavaScript errors in final page',errors.length===0);
  return {passed:checks.length,assignment:data.actionResult,checks};
}
