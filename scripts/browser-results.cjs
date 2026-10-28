const {chromium,expect}=require('@playwright/test');
const https=require('https');
(async()=>{
 const browser=await chromium.launch({headless:true,args:['--no-sandbox','--host-resolver-rules=MAP audioscraper.vroeg.app 188.114.96.0']});
 const page=await browser.newPage({viewport:{width:1440,height:1050}});
 const errors=[];page.on('pageerror',e=>errors.push(String(e)));
 const origin=process.env.AUDIOSCRAPER_ORIGIN||'http://127.0.0.1:8788';
 await page.goto(origin+'/#library');
 // De Whisper-wachtrij verschijnt alleen zolang er werk open staat.
 const queue=await(await page.request.get(origin+'/api/transcript-progress')).json();
 if(queue.remaining||queue.current){
  await page.getByRole('heading',{name:'Transcripties verwerken',exact:true}).waitFor();
  await expect(page.locator('#transcript-progress .queue-track')).toBeVisible();
 }else await expect(page.locator('#transcript-progress')).toBeHidden();
 const episodes=await(await page.request.get(origin+'/api/episodes')).json();
 const episode=episodes.find(e=>e.status==='completed'&&e.kind==='youtube'&&e.entity_count>0);
 if(!episode)throw Error('Test episode not yet complete');
 const data=await(await page.request.get(origin+'/api/episodes/'+episode.id)).json();
 const name=data.entities[0].name;
 const attribute=value=>value.replace(/"/g,'\\"');
 await page.locator('.episode-card').first().waitFor();
 await page.screenshot({path:'/app/test-results/dashboard-complete.png',fullPage:true});
 await page.goto(origin+'/#episode/'+episode.id);
 await page.getByRole('heading',{name:'Volledig transcript',exact:true}).waitFor();
 await expect(page.locator('.segment')).toHaveCount(data.segments.length);
 await expect(page.locator('.entity-chip')).toHaveCount(data.entities.length);
 if(data.published&&!await page.locator('.detail-meta').getByText('Gepubliceerd:',{exact:false}).count())throw Error('Publication date missing from detail');
 await page.screenshot({path:'/app/test-results/detail-complete.png',fullPage:false});
 const target=page.locator(`[data-highlight="${attribute(name)}"]`);
 await target.click();
 await expect(page.locator('mark').first()).toBeVisible();
 await expect(page.locator('.segment')).toHaveCount(data.segments.length);
 await page.locator('[data-clear-highlight]').click();
 await page.getByLabel('Zoek in dit transcript').fill(name);
 await expect(page.locator('mark').first()).toBeVisible();
 const timestampLink=page.locator('.timecode').nth(4);
 const timestampHref=await timestampLink.getAttribute('href');
 const videoId=new URL(data.url).searchParams.get('v');
 if(!timestampHref || !new RegExp('youtube\\.com/watch\\?v='+videoId+'&t=\\d+s').test(timestampHref)) throw Error('YouTube-tijdcode-link ontbreekt');
 if(await page.locator('.segment-link').count())throw Error('Transcripttekst mag geen link zijn');
 const download=await page.request.get(origin+'/api/episodes/'+episode.id+'/transcript.txt');
 // With speaker labels the export is timestamped per segment, so compare segments.
 const exported=await download.text();
 const missing=data.segments.filter(segment=>segment.text.trim()&&!exported.includes(segment.text.trim()));
 if(missing.length)throw Error('Export lacks '+missing.length+' of '+data.segments.length+' transcript segments');
 await page.goto(origin+'/#entities');
 await page.locator('.entity-tile').first().waitFor();
 await page.screenshot({path:'/app/test-results/entities-complete.png',fullPage:false});
 await page.fill('#search',name);
 await page.locator(`[data-entity-browse="${attribute(name)}"]`).first().click();
 await expect(page).toHaveURL(new RegExp('entity/'+encodeURIComponent(name).replace(/[.*+?^${}()|[\]\\]/g,'\\$&')+'\\?type='));
 await page.getByRole('heading',{name,exact:true}).waitFor();
 await page.locator('.entity-mention').first().click();
 await expect(page).toHaveURL(/episode\/[^?]+\?segment=\d+/);
 await expect(page.locator('.segment.target')).toBeVisible();
 await page.locator('[data-nav="entities"]').click();
 await page.getByRole('heading',{name:'Entiteiten',exact:true}).waitFor();
 await page.setViewportSize({width:390,height:844});
 await page.goto(origin+'/#episode/'+episode.id);
 await page.getByRole('heading',{name:'Volledig transcript',exact:true}).waitFor();
 if(await page.evaluate(()=>document.documentElement.scrollWidth>innerWidth))throw Error('Mobile overflow');
 await page.screenshot({path:'/app/test-results/detail-mobile-complete.png',fullPage:false});
 // The public origin must redirect to the existing Authelia login. Force IPv4
 // because this host has no usable outbound IPv6 route to Cloudflare.
 const response=await new Promise((resolve,reject)=>{
   const request=https.get({hostname:'audioscraper.vroeg.app',path:'/',family:4},resolve);
   request.on('error',reject);
 });
 if(![302,401].includes(response.statusCode)||(response.statusCode===302&&!response.headers.location?.startsWith('https://auth.vroeg.app/')))throw Error('Public login gate missing');
 response.resume();
 if(errors.length)throw Error(errors.join('\n'));
 console.log(JSON.stringify({result:'passed',segments:data.segments.length,entities:data.entities.length,checks:['transcript queue progress','full transcript','entity highlights from the episode itself','search','YouTube timestamp links','text export','entity navigation','mobile layout','public Authelia redirect']},null,2));
 await browser.close();
})().catch(e=>{console.error(e);process.exit(1)});
