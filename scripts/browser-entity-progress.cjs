const {chromium, expect}=require('@playwright/test');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  const origin=process.env.AUDIOSCRAPER_ORIGIN||'http://127.0.0.1:8788';
  await page.goto(origin+'/#library');
  const progress=await(await page.request.get(origin+'/api/entity-progress')).json();
  if(progress.total<1||progress.done>progress.total)throw Error('Invalid entity queue totals');
  // Een wachtrij zonder openstaand werk hoort niet in beeld te staan.
  const queued=progress.remaining>0||progress.current;
  if(!queued){
    await expect(page.locator('#entity-progress')).toBeHidden();
    const transcripts=await(await page.request.get(origin+'/api/transcript-progress')).json();
    if(!transcripts.remaining&&!transcripts.current)await expect(page.locator('#transcript-progress')).toBeHidden();
    if(errors.length)throw Error(errors.join('\n'));
    console.log(JSON.stringify({result:'passed',done:progress.done,total:progress.total,idle:true}));
    await browser.close();return;
  }
  await expect(page.locator('#entity-progress')).toBeVisible();
  await expect(page.locator('#entity-progress .queue-track')).toBeVisible();
  await expect(page.locator('#entity-progress .transcript-progress-head strong'))
    .toHaveText(`${progress.done} / ${progress.total}`,{timeout:10000});
  const bar=page.locator('#entity-progress .queue-track');
  await expect(bar).toHaveAttribute('aria-valuenow',String(progress.done));
  if(progress.current){
    const current=page.locator('#entity-progress .current-track');
    await expect(current).toBeVisible();
    const done=Number(await current.getAttribute('aria-valuenow'));
    const total=Number(await current.getAttribute('aria-valuemax'));
    if(done<0||done>total||!total)throw Error('Invalid current entity progress');
    await expect(page.locator('#entity-progress .current-transcript small')).toContainText('tekstdelen klaar');
  }
  await page.screenshot({path:'/app/test-results/entity-progress-desktop.png',fullPage:false});
  await page.setViewportSize({width:390,height:844});
  await page.reload();
  await expect(page.locator('#entity-progress .queue-track')).toBeVisible();
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile horizontal overflow');
  await page.screenshot({path:'/app/test-results/entity-progress-mobile.png',fullPage:false});
  if(errors.length)throw Error(errors.join('\n'));
  console.log(JSON.stringify({result:'passed',done:progress.done,total:progress.total,current:progress.current?.title||null}));
  await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
