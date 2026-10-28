const {chromium,expect}=require('@playwright/test');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  const origin=process.env.AUDIOSCRAPER_ORIGIN||'http://127.0.0.1:8788';
  const original=await(await page.request.get(origin+'/api/processing')).json();
  try{
    await page.goto(origin+'/#library');
    const toggle=page.locator('#processing-toggle');
    await expect(toggle).toBeEnabled();
    if(!original.paused){
      await expect(toggle).toHaveText('⏸ Pauzeer alles');
      await toggle.click();
      await expect(toggle).toHaveText('▶ Hervat alles');
      assertPaused(await(await page.request.get(origin+'/api/processing')).json());
      await page.reload();
      await expect(toggle).toHaveText('▶ Hervat alles');
      await toggle.click();
      await expect(toggle).toHaveText('⏸ Pauzeer alles');
    }
    // De analysewachtrij staat er alleen zolang er nog werk open staat.
    const analysis=await(await page.request.get(origin+'/api/entity-progress')).json();
    if(analysis.remaining||analysis.current)await expect(page.locator('#entity-progress .queue-track')).toBeVisible();
    else await expect(page.locator('#entity-progress')).toBeHidden();
    await page.screenshot({path:'/app/test-results/processing-control-desktop.png'});
    await page.setViewportSize({width:390,height:844});
    await page.reload();
    await expect(toggle).toBeVisible();
    if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile horizontal overflow');
    await page.screenshot({path:'/app/test-results/processing-control-mobile.png'});
    if(errors.length)throw Error(errors.join('\n'));
    console.log(JSON.stringify({result:'passed',checks:['pause','persistent state after page reload','resume','queue panel matches open work','mobile button','no JS errors']}));
  }finally{
    await page.request.put(origin+'/api/processing',{data:{paused:original.paused}});
    await browser.close();
  }
})().catch(error=>{console.error(error);process.exit(1)});
function assertPaused(status){if(!status.paused)throw Error('Pause was not saved')}
