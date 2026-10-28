const {chromium,expect}=require('@playwright/test');
(async()=>{
  const browser=await chromium.launch({headless:true,args:['--no-sandbox']});
  const page=await browser.newPage({viewport:{width:1440,height:1000}});
  const errors=[];page.on('pageerror',error=>errors.push(String(error)));
  await page.goto((process.env.AUDIOSCRAPER_ORIGIN||'http://127.0.0.1:8788')+'/#entities');
  const region=page.locator('#location-region-filter');
  await expect(region).toBeVisible();
  const selectRegion=async value=>Promise.all([
    page.waitForResponse(response=>response.url().includes('/api/entities?')&&new URL(response.url()).searchParams.get('region')===value),
    region.selectOption(value),
  ]);
  await selectRegion('north');
  await expect(page.locator('#type-filter')).toHaveValue('location');
  const north=page.locator('.entity-tile');
  await expect(north.first().locator('.entity-type')).toHaveText('Locaties');
  if(await north.count()===0)throw Error('Geen Noord-Nederlandse locaties gevonden');
  for(const tile of await north.all()){
    await expect(tile.locator('.entity-type')).toHaveText('Locaties');
    await expect(tile.locator('.entity-geography')).toContainText(/Groningen|Friesland|Drenthe/);
  }
  const northCount=await north.count();
  await selectRegion('nl');
  if(await page.locator('.entity-tile').count()<northCount)throw Error('Nederlandfilter bevat minder locaties dan Noord-Nederland');
  await selectRegion('europe');
  if(await page.locator('.entity-tile').count()<northCount)throw Error('Europafilter bevat minder locaties dan Noord-Nederland');
  await page.setViewportSize({width:390,height:844});
  await page.reload();
  await expect(region).toBeVisible();
  if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobiele pagina is horizontaal breder dan het scherm');
  if(errors.length)throw Error(errors.join('\n'));
  console.log(JSON.stringify({result:'passed',northCount,checks:['north','netherlands','europe','mobile','no JS errors']}));
  await browser.close();
})().catch(error=>{console.error(error);process.exit(1)});
