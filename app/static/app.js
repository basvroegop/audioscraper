const $ = (s, root=document) => root.querySelector(s);
const esc = v => String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const labels={person:'Personen',organization:'Organisaties & instanties',location:'Locaties',product:'Producten & werken',event:'Evenementen',other:'Overige namen'};
const statuses={queued:'In wachtrij',downloading:'Downloaden',transcribing:'Transcriberen',transcribed:'Transcript klaar',analyzing:'Entiteiten herkennen',completed:'Voltooid',failed:'Mislukt'};
const sourceTypes={youtube:'Heel kanaal','youtube-videos':'Alleen video’s','youtube-streams':'Alleen live-uitzendingen','youtube-shorts':'Alleen Shorts','youtube-playlist':'Afspeellijst',rss:'Podcast-RSS',video:'Losse video',companywebcast:'Raadsvergaderingen',notubiz:'Raadsvergaderingen',gemeenteoplossingen:'Raadsvergaderingen'};
const episodeTags={youtube:'▶ YOUTUBE',rss:'◉ PODCAST',meeting:'⌘ VERGADERING'};
const accents={moss:'Mos',teal:'Diepgroen',clay:'Klei',indigo:'Indigo',amber:'Amber',plum:'Pruim',rust:'Roest',slate:'Leisteen'};
/* Line icons on a 24x24 grid; they take their colour from the link around them. */
const glyph=paths=>`<svg class="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>`;
const icons={
 projects:glyph('<rect x="3.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.6"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.6"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.6"/>'),
 overview:glyph('<rect x="3.5" y="3.5" width="7" height="9" rx="1.4"/><rect x="13.5" y="3.5" width="7" height="5" rx="1.4"/><rect x="3.5" y="15.5" width="7" height="5" rx="1.4"/><rect x="13.5" y="11.5" width="7" height="9" rx="1.4"/>'),
 library:glyph('<path d="M12 6.6S9.9 4.6 3.8 4.6v12.6c6.1 0 8.2 2.2 8.2 2.2s2.1-2.2 8.2-2.2V4.6C14.1 4.6 12 6.6 12 6.6Z"/><path d="M12 6.6v13"/>'),
 entities:glyph('<circle cx="12" cy="8.2" r="3.7"/><path d="M5.3 20a6.7 6.7 0 0 1 13.4 0"/>'),
 speakers:glyph('<rect x="9.2" y="2.8" width="5.6" height="11.2" rx="2.8"/><path d="M5.9 11.5a6.1 6.1 0 0 0 12.2 0"/><path d="M12 17.6v3.6"/><path d="M8.7 21.2h6.6"/>'),
 settings:glyph('<path d="M4 7.5h8.5M17.5 7.5h2.5M4 16.5h2.5M11.5 16.5h8.5"/><circle cx="15" cy="7.5" r="2.5"/><circle cx="9" cy="16.5" r="2.5"/>'),
};
const projectNav=[['overview','Overzicht',''],['library','Bibliotheek','nav-count'],['entities','Entiteiten',''],['speakers','Stemmen',''],['settings','Instellingen','']];
const globalNav=[['library','Bibliotheek','nav-count'],['entities','Entiteiten',''],['speakers','Stemmen','']];
const sectionNames={overview:'Overzicht',library:'Bibliotheek',entities:'Entiteiten',speakers:'Stemmen',settings:'Instellingen',projects:'Alle projecten',detail:'Transcript','entity-detail':'Vermeldingen'};
let view='', selectedEntity='', selectedSpeaker='', detail=null, refreshTimer, searchTimer, toastTimer, routeVersion=0;
let projects=[], projectMap=new Map();
const state={q:'',status:'',entity:'',kind:'',phoneRegion:'',phoneStatus:'',locationRegion:'',project:''};
let phoneRegionDataPromise;
function loadPhoneRegionData(){return phoneRegionDataPromise ||= fetch('/static/phone-regions.json?v=20260917-14').then(r=>{if(!r.ok)throw Error('Regiogegevens laden mislukt');return r.json()}).catch(error=>{phoneRegionDataPromise=null;throw error})}
async function api(path,opts={}){const r=await fetch('/api'+path,{...opts,headers:{'Content-Type':'application/json',...opts.headers}});if(!r.ok){let data;try{data=await r.json()}catch{}throw Error(typeof data?.detail==='string'?data.detail:'Aanvraag mislukt ('+r.status+').')}return r.json()}

/* Every read stays inside the active project unless the whole archive is shown. */
function scoped(extra={}){const params=new URLSearchParams(extra);if(state.project)params.set('project',state.project);const query=params.toString();return query?'?'+query:''}
function sectionHash(section,params=''){const path=state.project?`project/${state.project}/${section}`:section;return '#'+path+(params?'?'+params:'')}
function activeProject(){return state.project?projectMap.get(state.project):null}
function projectName(project){return project?(project.display_name||project.title):'Alle projecten'}
function plural(count,one,many){return count+' '+(count===1?one:many)}
function kindIcon(kind){return String(kind||'').startsWith('youtube')||kind==='video'?'▶':kind==='companywebcast'?'⌘':'◉'}
function accentClass(accent){return 'accent-'+(accents[accent]?accent:'moss')}

let processingState=null,changingProcessing=false;
function showProcessingState(status){
 processingState=status;
 const button=$('#processing-toggle'),label=$('#processing-state');
 button.textContent=status.paused?'▶ Hervat alles':'⏸ Pauzeer alles';
 button.classList.toggle('paused',status.paused);
 button.disabled=changingProcessing;
 label.textContent=status.paused?(status.finishing?'Pauzeren na huidige stap':'Alles gepauzeerd'):'Verwerking actief';
 label.classList.toggle('paused',status.paused);
}
async function refreshProcessingState(){if(changingProcessing)return;try{showProcessingState(await api('/processing'))}catch(error){$('#processing-state').textContent='Status niet beschikbaar';$('#processing-toggle').disabled=true}}
$('#processing-toggle').onclick=async()=>{
 if(!processingState||changingProcessing)return;
 changingProcessing=true;$('#processing-toggle').disabled=true;
 try{const status=await api('/processing',{method:'PUT',body:JSON.stringify({paused:!processingState.paused})});showProcessingState(status)}
 catch(error){toast(error.message)}
 finally{changingProcessing=false;await refreshProcessingState()}
};
function toast(message){$('#toast').textContent=message;$('#toast').style.display='block';clearTimeout(toastTimer);toastTimer=setTimeout(()=>$('#toast').style.display='none',5000)}
function timecode(seconds){const n=Math.floor(seconds||0);return (n>=3600?Math.floor(n/3600)+':':'')+String(Math.floor(n/60)%60).padStart(2,'0')+':'+String(n%60).padStart(2,'0')}
function duration(n){return n?Math.round(n/60)+' min':'—'}
function date(value){if(!value)return 'Nog niet gecontroleerd';const d=new Date(typeof value==='number'?value*1000:value);return isNaN(d)?value:d.toLocaleDateString('nl-NL',{day:'numeric',month:'short',hour:'2-digit',minute:'2-digit'})}
function publishedDate(value){if(!value)return '';const compact=String(value).match(/^([12]\d{3})(\d{2})(\d{2})$/);const d=compact?new Date(Number(compact[1]),Number(compact[2])-1,Number(compact[3])):new Date(value);return isNaN(d)?String(value):d.toLocaleDateString('nl-NL',{day:'numeric',month:'long',year:'numeric'})}
function badge(status){return `<span class="badge ${status==='failed'?'failed':status==='completed'?'':'working'}">${status==='completed'?'✓':'◌'} ${esc(statuses[status]||status)}</span>`}
function empty(title,text,button=false){return `<div class="empty"><strong>${title}</strong><p>${text}</p>${button?'<button class="primary" data-add>Maak je eerste project <span>+</span></button>':''}</div>`}
function addButton(label='Nieuw project'){return `<button class="primary" data-add><span>+</span> ${label}</button>`}
function safeUrl(url){try{const u=new URL(url);return ['https:','http:'].includes(u.protocol)?esc(u.href):'#'}catch{return '#'}}
function youtubeTimestampUrl(url,seconds){try{const target=new URL(url);const host=target.hostname.toLowerCase();let videoId='';if(host==='youtu.be'||host==='www.youtu.be')videoId=target.pathname.split('/').filter(Boolean)[0]||'';else if(['youtube.com','www.youtube.com','m.youtube.com','music.youtube.com'].includes(host)){videoId=target.searchParams.get('v')||'';if(!videoId&&target.pathname.startsWith('/shorts/'))videoId=target.pathname.split('/')[2]||''}if(!videoId)return '';return 'https://www.youtube.com/watch?v='+encodeURIComponent(videoId)+'&t='+Math.max(0,Math.floor(seconds||0))+'s'}catch{return ''}}
function heading(eyebrow,title,description,action=addButton()){return `<div class="page-heading"><div><span class="eyebrow">${eyebrow}</span><h1>${title}</h1><p class="subtitle">${description}</p></div>${action}</div>`}
function projectHeading(section,description){
 const project=activeProject();
 if(!project)return heading('ALLE PROJECTEN',esc(sectionNames[section]||'Bibliotheek'),description+' Dit overzicht combineert alle projecten.');
 return `<div class="page-heading project-heading ${accentClass(project.accent)}"><div><span class="eyebrow"><a href="#projects">PROJECTEN</a> / ${esc(projectName(project)).toUpperCase()}</span><h1>${esc(sectionNames[section]||'Overzicht')}</h1><p class="subtitle">${description}</p></div><div class="heading-actions">${section==='settings'?`<a class="text-link" href="#project/${project.id}">Terug naar overzicht ↗</a>`:`<a class="text-link" href="#project/${project.id}/settings">Projectinstellingen ↗</a>`}${addButton()}</div></div>`;
}
function stat(label,value,note,icon){return `<div class="stat"><div><div class="stat-label">${label}</div><div class="stat-number">${value}</div><div class="stat-note">${note}</div></div><div class="stat-icon">${icon}</div></div>`}
function searchBox(placeholder,value,id='search'){return `<div class="search"><input id="${id}" type="search" aria-label="${placeholder}" placeholder="${placeholder}" value="${esc(value)}"></div>`}
/* The dashboard runs under style-src 'self', which blocks inline style
   attributes. Bar widths are therefore applied from the CSSOM after rendering. */
function bar(percent){return `<i data-width="${Math.max(0,Math.min(100,Math.round(percent||0)))}"></i>`}
function applyWidths(root=document){root.querySelectorAll('i[data-width]').forEach(element=>{element.style.width=element.dataset.width+'%'})}
function projectChip(episode){return episode.project_id&&!state.project?`<a class="project-chip ${accentClass(episode.project_accent)}" href="#project/${episode.project_id}/library" title="Open dit project">${esc(episode.project_name||episode.source_title||'Project')}</a>`:''}

/* ---------------------------------------------------------------- sidebar */
async function loadProjects(){
 projects=await api('/projects');
 projectMap=new Map(projects.map(p=>[p.id,p]));
 renderProjectNav();
 return projects;
}
/* Every project is its own link under “Alle projecten”, so the whole archive
   is one click away without a dropdown in between. */
function renderProjectNav(){
 $('#project-nav').innerHTML=`<a href="#projects" data-nav="projects"><span>${icons.projects}</span> Alle projecten <b>${projects.length}</b></a>`
  +projects.map(project=>`<a class="project-link ${accentClass(project.accent)}${project.id===state.project?' active':''}" href="#project/${project.id}" title="${esc(projectName(project))}"><span class="project-dot"></span><span class="project-link-name">${esc(projectName(project))}</span><b>${project.episode_count}</b></a>`).join('');
}
function renderNav(section){
 renderProjectNav();
 const project=activeProject();
 $('#scope-label').textContent=project?projectName(project).toUpperCase():'ALLE PROJECTEN';
 const items=project?projectNav:globalNav;
 $('#scope-nav').innerHTML=items.map(([key,label,counter])=>
  `<a href="${project?`#project/${project.id}/${key}`:'#'+key}" data-nav="${key}"><span>${icons[key]||''}</span> ${label}${counter?` <b id="${counter}">0</b>`:''}</a>`).join('');
 document.querySelectorAll('[data-nav]').forEach(link=>link.classList.toggle('active',link.dataset.nav===section));
}
function showBreadcrumb(section){
 const project=activeProject();
 $('#breadcrumb').innerHTML=project
  ?`<a href="#projects">Projecten</a> <span>/</span> <a href="#project/${project.id}">${esc(projectName(project))}</a> <span>/</span> ${esc(sectionNames[section]||'Overzicht')}`
  :section==='projects'?'Projecten'
  :`<a href="#projects">Alle projecten</a> <span>/</span> ${esc(sectionNames[section]||'Bibliotheek')}`;
}
/* Opening an episode from anywhere puts the sidebar in its project. */
function enterProjectContext(project,section){
 if(!project||!projectMap.has(project.id))return;
 state.project=project.id;
 renderNav(section);
 showBreadcrumb(section);
}
function setCounts(stats){
 if($('#nav-count'))$('#nav-count').textContent=stats.total;
 if($('#phone-count'))$('#phone-count').textContent=stats.phone_numbers||0;
}
const countsFromStats=new Set(['projects','overview','library']);

/* --------------------------------------------------------------- progress */
async function renderTranscriptProgress(){const progress=await api('/transcript-progress'+scoped());const target=$('#transcript-progress');if(!target)return;const current=progress.current;
 // Nothing running and nothing waiting: the panel stays away entirely.
 if(!current&&!progress.remaining){target.innerHTML='';return}const hasWork=progress.total>0;const currentTitle=current?esc(current.title):'';const worker=current?.worker_progress;const phases={waiting_for_gpu:'Wacht op gedeelde GPU',starting:'Whisper starten',transcribing:'Whisper transcribeert',diarizing:'Sprekers herkennen'};const exact=worker&&Number.isInteger(worker.percent);const currentDetail=current?`${exact?phases[worker.phase]||'Whisper transcribeert':esc(current.stage||statuses[current.status]||'Wordt verwerkt')}${exact?` · ${worker.percent}% · ${timecode(worker.processed_seconds)} / ${timecode(worker.duration)}`:current.duration?` · ${duration(current.duration)}`:''}`:'';const currentBar=exact?`<div class="current-track exact" role="progressbar" aria-label="Huidige transcriptie" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${worker.percent}">${bar(worker.percent)}</div>`:`<div class="current-track" role="progressbar" aria-label="Huidige transcriptie wordt verwerkt"><i></i></div>`;const activity=current?`<div class="current-transcript"><div><span class="current-label">NU BEZIG</span><strong>${currentTitle}</strong><small>${currentDetail}</small></div>${currentBar}</div>`:progress.remaining?`<p class="progress-waiting">${progress.waiting} ${progress.waiting===1?'opname wacht':'opnames wachten'} op Whisper.</p>`:`<p class="progress-waiting">${hasWork?'Alle transcripties zijn klaar.':'Nog geen transcripties in de wachtrij.'}</p>`;target.innerHTML=`<div class="transcript-progress-head"><div><span class="eyebrow">WHISPER-WACHTRIJ</span><h2>Transcripties verwerken</h2></div><strong>${progress.done} / ${progress.total}</strong></div><div class="queue-track" role="progressbar" aria-label="Transcripties klaar" aria-valuemin="0" aria-valuemax="${progress.total}" aria-valuenow="${progress.done}">${bar(progress.percent)}</div><div class="queue-summary"><span>${progress.percent}% klaar</span><span>${progress.remaining} ${progress.remaining===1?'transcriptie resteert':'transcripties resten'}${progress.running?` · ${progress.running} loopt nu`:''}</span></div>${activity}`;applyWidths(target)}
async function renderEntityProgress(){
 const progress=await api('/entity-progress'+scoped());const target=$('#entity-progress');if(!target)return;
 const current=progress.current;
 if(!current&&!progress.remaining){target.innerHTML='';return}
 const activity=current?`<div class="current-transcript"><div><span class="current-label">NU BEZIG</span><strong>${esc(current.title)}</strong><small>${current.parts_done} van ${current.parts_total} tekstdelen klaar · ${current.percent}%</small></div><div class="current-track exact" role="progressbar" aria-label="Huidige entiteitsdetectie" aria-valuemin="0" aria-valuemax="${current.parts_total}" aria-valuenow="${current.parts_done}">${bar(current.percent)}</div></div>`:
 progress.remaining===0?`<p class="progress-waiting">${progress.total?'Alle entiteiten zijn verwerkt.':'Nog geen transcripties om te analyseren.'}</p>`:
 progress.awaiting_transcript?`<p class="progress-waiting">Entiteitsdetectie gaat verder zodra de resterende transcripties klaar zijn.</p>`:
 `<p class="progress-waiting">${progress.waiting} ${progress.waiting===1?'aflevering wacht':'afleveringen wachten'} op entiteitsdetectie.</p>`;
 target.innerHTML=`<div class="transcript-progress-head"><div><span class="eyebrow">ANALYSE-WACHTRIJ</span><h2>Entiteiten herkennen</h2></div><strong>${progress.done} / ${progress.total}</strong></div><div class="queue-track" role="progressbar" aria-label="Entiteitsdetectie klaar" aria-valuemin="0" aria-valuemax="${progress.total}" aria-valuenow="${progress.done}">${bar(progress.percent)}</div><div class="queue-summary"><span>${progress.percent}% klaar</span><span>${progress.remaining} ${progress.remaining===1?'aflevering resteert':'afleveringen resten'}${progress.running?` · ${progress.running} loopt nu`:''}</span></div>${activity}`;
 applyWidths(target);
}

/* ----------------------------------------------------------------- router */
function parseHash(){
 const hash=location.hash.slice(1)||'projects';
 const path=hash.split('?')[0],params=new URLSearchParams(hash.split('?')[1]||'');
 const parts=path.split('/').filter(Boolean);
 if(parts[0]==='project')return {project:parts[1]||'',section:parts[2]||'overview',params,rest:[]};
 if(parts[0]==='episode')return {project:'',section:'detail',params,rest:[parts[1]||'']};
 if(parts[0]==='entity')return {project:params.get('project')||'',section:'entity-detail',params,rest:[decodeURIComponent(parts.slice(1).join('/'))]};
 if(parts[0]==='sources')return {project:'',section:'projects',params,rest:[]};
 return {project:'',section:parts[0]||'projects',params,rest:[]};
}
async function route(){
 const version=++routeVersion;clearInterval(refreshTimer);
 const {project,section:requested,params,rest}=parseHash();
 // De losse nummerpagina is opgegaan in Entiteiten; oude links blijven werken.
 const section=requested==='phones'?'entities':requested;
 state.project=project;
 selectedEntity='';selectedSpeaker='';detail=null;
 state.q='';state.status='';state.kind='';state.locationRegion='';state.entity=params.get('entity')||'';
 if(section==='entities'){
  state.phoneRegion=['north','Groningen','Drenthe','Friesland'].includes(params.get('region'))?params.get('region'):'';
  state.phoneStatus=['mobile','review','confirmed','unknown','rejected'].includes(params.get('status'))?params.get('status'):'';
 }
 $('#content').innerHTML='<div class="loading">Archief laden…</div>';
 try{
  await loadProjects();
  if(state.project&&!projectMap.has(state.project)){toast('Dit project bestaat niet meer.');location.hash='projects';return}
 }catch(error){$('#content').innerHTML=empty('Projecten laden lukt even niet',esc(error.message));return}
 view=section;
 renderNav(section==='detail'?'library':section==='entity-detail'?'entities':section);
 const project_=activeProject();
 showBreadcrumb(section);
 try{
  if(section==='projects')await renderProjects();
  else if(section==='overview')await renderOverview();
  else if(section==='settings')await renderSettings();
  else if(section==='detail'){
   await renderDetail(rest[0]);
   const segment=Number(params.get('segment'));
   if(Number.isInteger(segment)&&segment>=0)requestAnimationFrame(()=>{const target=$('#segment-'+segment);target?.classList.add('target');target?.scrollIntoView({behavior:'smooth',block:'center'})});
  }
  else if(section==='entity-detail')await renderEntityDetail(rest[0],params.get('type')||'');
  else if(section==='entities')await renderEntities();
  else if(section==='speakers')await renderSpeakers();
  else await renderLibrary();
  // Pages that do not need the totals themselves still show them in the sidebar.
  if(!countsFromStats.has(section))api('/stats'+scoped()).then(stats=>{if(version===routeVersion)setCounts(stats)}).catch(()=>{});
  if(version===routeVersion)refreshTimer=setInterval(refresh,6000);
 }catch(e){$('#content').innerHTML=empty('Laden lukt even niet',esc(e.message))}
 window.scrollTo(0,0);
}

/* ------------------------------------------------------------ project hub */
function projectGrid(){return projects.map((project,index)=>projectCard(project,index,projects.length)).join('')}
function projectCard(project,index=0,total=1){
 const percent=project.episode_count?Math.round(project.transcripts*100/project.episode_count):0;
 const state_=project.syncing?['working','Bron controleren…']:!project.enabled?['paused','Gepauzeerd']:project.error?['failed','Bron gaf een fout']:project.active?['working',project.active+' in verwerking']:['ready','Bijgewerkt'];
 return `<article class="project-card ${accentClass(project.accent)}">
 <div class="project-card-head"><span class="project-avatar">${kindIcon(project.kind)}<b class="project-rank" title="Plaats ${index+1} in de verwerkingswachtrij">${index+1}</b></span><div><a class="project-open" href="#project/${project.id}"><h3>${esc(projectName(project))}</h3></a><span class="project-kind">${esc(sourceTypes[project.kind]||project.kind)}</span></div><span class="project-state ${state_[0]}">${esc(state_[1])}</span></div>
 ${project.description?`<p class="project-description">${esc(project.description)}</p>`:`<a class="project-source" href="${safeUrl(project.url)}" target="_blank" rel="noopener">${esc(project.url)} ↗</a>`}
 <div class="project-track" role="progressbar" aria-label="Transcripties in ${esc(projectName(project))}" aria-valuemin="0" aria-valuemax="${project.episode_count}" aria-valuenow="${project.transcripts}">${bar(percent)}</div>
 <div class="project-numbers"><span><b>${project.episode_count}</b>afleveringen</span><span><b>${project.transcripts}</b>transcripties</span><span><b>${project.entities}</b>entiteiten</span><span><b>${project.phone_numbers}</b>nummers</span></div>
 ${project.error?`<p class="error">${esc(project.error)}</p>`:''}
 <div class="project-foot"><span>${project.updated?'Laatste werk: '+esc(date(project.updated)):'Nog geen verwerking'}</span><div class="project-actions"><span class="project-move"><button data-move-project="${project.id}" data-direction="up" ${index===0?'disabled':''} title="Eerder verwerken" aria-label="Verwerk ${esc(projectName(project))} eerder">↑</button><button data-move-project="${project.id}" data-direction="down" ${index>=total-1?'disabled':''} title="Later verwerken" aria-label="Verwerk ${esc(projectName(project))} later">↓</button></span>${project.pollable?`<button data-sync="${project.id}" ${!project.enabled||project.syncing?'disabled':''}>↻ Controleer</button><button data-toggle="${project.id}" data-enabled="${!project.enabled}">${project.enabled?'Pauzeren':'Hervatten'}</button>`:''}<a class="text-link" href="#project/${project.id}">Open ↗</a></div></div>
 </article>`;
}
async function renderProjects(){
 const stats=await api('/stats');
 $('#content').innerHTML=heading('IEDERE BRON EEN EIGEN OMGEVING','Je projecten','Kies een project om binnen die omgeving te werken: eigen bibliotheek, transcripties, entiteiten, stemmen en nummers.')
 +`<section class="transcript-progress" id="transcript-progress" aria-live="polite"></section><section class="transcript-progress entity-progress" id="entity-progress" aria-live="polite"></section>`
 +`<div class="section-head"><h2>Alle projecten <small>${projects.length}</small></h2><a href="#library" class="text-link">Bekijk alles samen ↗</a></div>`
 +`<p class="panel-help queue-order-help">De wachtrij werkt de projecten van boven naar beneden af: een project wordt eerst helemaal getranscribeerd en geanalyseerd voordat het volgende aan de beurt is. Verschuif een project met ↑ en ↓.</p>`
 +`<div class="project-grid">${projects.length?projectGrid():empty('Nog geen projecten','Voeg een YouTube-kanaal, afspeellijst, losse video of podcast toe. Elke bron krijgt een eigen omgeving.',true)}</div>`;
 applyWidths();
 setCounts(stats);
 await Promise.all([renderTranscriptProgress(),renderEntityProgress()]);
}

/* ------------------------------------------------------- project overview */
async function renderOverview(){
 const project=activeProject();
 const [stats,episodes,entities]=await Promise.all([api('/stats'+scoped()),api('/episodes'+scoped()),api('/entities'+scoped({limit:24}))]);
 const recent=episodes.slice(0,6);
 $('#content').innerHTML=projectHeading('overview',project.description?esc(project.description):`Alles wat Audioscraper uit <a href="${safeUrl(project.url)}" target="_blank" rel="noopener">deze bron</a> heeft gehaald.`)
 +`<div class="stats">${stat('In dit project',stats.total,stats.completed+' volledig verwerkt','▤')}${stat('In verwerking',stats.active,stats.failed?stats.failed+' hebben aandacht nodig':'Automatisch op de achtergrond','≋')}${stat('Entiteiten',stats.entities,stats.phone_numbers+' telefoonnummers','◎')}</div>`
 +`<div class="pipeline"><strong>${kindIcon(project.kind)} &nbsp; ${esc(sourceTypes[project.kind]||project.kind)}</strong><span class="arrow">→</span><span>Audio</span><span class="arrow">→</span><span>Transcript</span><span class="arrow">→</span><strong>Entiteiten</strong><span class="auto">${project.enabled?(project.pollable?'Ieder uur gecontroleerd':'Eenmalige import'):'Project gepauzeerd'}</span></div>`
 +`<section class="transcript-progress" id="transcript-progress" aria-live="polite"></section><section class="transcript-progress entity-progress" id="entity-progress" aria-live="polite"></section>`
 +`<div class="section-head"><h2>Laatste afleveringen</h2><a href="#project/${project.id}/library" class="text-link">Hele bibliotheek ↗</a></div>`
 +`<div class="cards">${recent.length?recent.map(episodeCard).join(''):empty('Nog geen afleveringen','Zodra de bron is gecontroleerd verschijnen hier de afleveringen van dit project.')}</div>`
 +`<div class="section-head"><h2>Meest genoemd</h2><a href="#project/${project.id}/entities" class="text-link">Alle entiteiten ↗</a></div>`
 +`<div class="entity-chips overview-chips">${entities.entities.length?entities.entities.map(e=>`<a class="entity-chip" href="#entity/${encodeURIComponent(e.name)}?type=${encodeURIComponent(e.type)}&project=${project.id}">${esc(e.name)}<b>${e.count}</b></a>`).join(''):'<p class="count-hint">De entiteiten van dit project verschijnen na de analyse.</p>'}</div>`;
 setCounts(stats);
 await Promise.all([renderTranscriptProgress(),renderEntityProgress()]);
}

/* -------------------------------------------------------------- bibliotheek */
function waves(){return '<div class="waves">'+[12,21,35,22,44,59,31,47,24,56,38,20,49,31,57,22,42,17,31,12,26,17,36,22,14].map((h,i)=>`<i class="wave-${i}"></i>`).join('')+'</div>'}
function episodeCard(e){return `<a class="episode-card" href="#episode/${e.id}"><div class="card-art ${e.kind}"><span class="source-tag">${episodeTags[e.kind]||'◉ PODCAST'}</span>${e.thumbnail_url||e.kind==='youtube'?`<img class="episode-artwork" src="/api/episodes/${e.id}/thumbnail" alt="" loading="lazy">`:waves()}<span class="duration">${duration(e.duration)}</span></div><div class="card-body">${projectChip(e)}<h3>${esc(e.title)}</h3><p class="byline">${esc(e.author||e.project_name||e.source_title||'Bron wordt opgehaald')}${e.published?` · ${esc(publishedDate(e.published))}`:''}</p><div class="entity-preview">${e.top_entities.map(x=>`<span class="mini-tag">${esc(x.name)}</span>`).join('')}${e.entity_count>4?`<span class="mini-tag">+${e.entity_count-4}</span>`:''}</div>${e.status!=='completed'?`<p class="stage-text">${esc(e.stage)}</p>`:''}<div class="card-bottom">${badge(e.status)}<span>${e.entity_count?e.entity_count+' entiteiten':'Bekijk details'} ↗</span></div></div></a>`}
async function renderLibrary(){
 const stats=await api('/stats'+scoped());
 $('#content').innerHTML=projectHeading('library','Elke aflevering met transcript, tijdcodes en genoemde namen.')
 +`<div class="stats" id="stats">${stat('In de bibliotheek',stats.total,stats.completed+' volledig verwerkt','▤')}${stat('In verwerking',stats.active,stats.failed?stats.failed+' hebben aandacht nodig':'Automatisch op de achtergrond','≋')}${stat('Gevonden entiteiten',stats.entities,'Personen, organisaties en meer','◎')}</div>`
 +`<section class="transcript-progress" id="transcript-progress" aria-live="polite"></section><section class="transcript-progress entity-progress" id="entity-progress" aria-live="polite"></section>`
 +`<div class="section-head"><h2>Afleveringen <small id="result-count"></small></h2>${state.project?`<a href="#project/${state.project}/settings" class="text-link">Projectinstellingen ↗</a>`:'<a href="#projects" class="text-link">Beheer projecten ↗</a>'}</div>`
 +`<div class="toolbar">${searchBox('Zoek in titels en transcripties…',state.q)}<select id="status-filter" aria-label="Filter op status"><option value="">Alle statussen</option>${Object.entries(statuses).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select></div><div id="entity-filter"></div><div class="cards" id="episode-list"></div>`;
 setCounts(stats);
 $('#search').oninput=e=>{state.q=e.target.value;debounce(loadEpisodes)};
 $('#status-filter').onchange=e=>{state.status=e.target.value;loadEpisodes()};
 await Promise.all([loadEpisodes(),renderTranscriptProgress(),renderEntityProgress()]);
}
async function loadEpisodes(){
 const items=await api('/episodes'+scoped({q:state.q,status:state.status,entity:state.entity}));
 if(!$('#episode-list'))return;
 $('#result-count').textContent=items.length+' '+(items.length===1?'aflevering':'afleveringen');
 $('#entity-filter').innerHTML=state.entity?`<div class="selection-note">Vermeldingen van ${esc(state.entity)} <button data-clear-filter>Wis filter ×</button></div>`:'';
 const filtered=state.q||state.status||state.entity;
 $('#episode-list').innerHTML=items.length?items.map(episodeCard).join(''):empty(
  filtered?'Geen afleveringen gevonden':state.project?'Dit project is nog leeg':'Je archief begint hier',
  filtered?'Probeer een andere zoekterm of filter.':state.project?'Zodra de bron is gecontroleerd verschijnen de afleveringen hier.':'Voeg een YouTube-kanaal, video of podcast toe. Elke bron krijgt een eigen project.',
  !filtered&&!state.project);
}

/* --------------------------------------------------------------- entiteiten */
const ENTITY_PAGE=150;
let entityOffset=0,entityTotal=0;
async function renderEntities(){
 const regions=[['nl','Nederland'],['north','Noord-Nederland'],['Groningen','Provincie Groningen'],['Friesland','Provincie Friesland'],['Drenthe','Provincie Drenthe'],['europe','Europa'],['outside-europe','Buiten Europa'],['continent:AF','Afrika'],['continent:AS','Azië'],['continent:NA','Noord-Amerika'],['continent:SA','Zuid-Amerika'],['continent:OC','Oceanië'],['unknown','Niet ingedeeld']];
 $('#content').innerHTML=projectHeading('entities','Personen, instanties, plaatsen en andere namen uit de transcripties.')
 +`<div class="toolbar entity-toolbar">${searchBox('Zoek een persoon, organisatie of plaats…',state.q)}<select id="type-filter" aria-label="Filter op entiteitstype"><option value="">Alle categorieën</option>${Object.entries(labels).map(([k,v])=>`<option value="${k}">${v}</option>`).join('')}</select><select id="location-region-filter" aria-label="Filter locaties op gebied"><option value="">Alle gebieden</option>${regions.map(([value,label])=>`<option value="${value}">${label}</option>`).join('')}</select></div>`
 +`<p class="panel-help">Het gebiedsfilter toont alleen locaties. Plaatsen worden lokaal ingedeeld; onduidelijke of verkeerd herkende namen staan onder ‘Niet ingedeeld’.</p><div class="entity-grid" id="entities-list"></div><div class="entity-more" id="entity-more"></div>`
 +`<section id="phone-panel" aria-live="polite"></section>`;
 phoneSignature='';
 $('#search').oninput=e=>{state.q=e.target.value;debounce(loadEntities)};
 $('#type-filter').onchange=e=>{state.kind=e.target.value;if(state.kind!=='location'){state.locationRegion='';$('#location-region-filter').value=''}loadEntities()};
 $('#location-region-filter').onchange=e=>{state.locationRegion=e.target.value;if(state.locationRegion){state.kind='location';$('#type-filter').value='location'}loadEntities()};
 await loadEntities();
 await renderPhoneSection();
}
function geographyLabel(geography){if(!geography)return '';return [geography.province,geography.country||geography.continent].filter(Boolean).join(' · ')}
function entityTile(e){return `<button class="entity-tile ${e.type}" data-entity-browse="${esc(e.name)}" data-entity-type="${esc(e.type)}"><span class="entity-type">${labels[e.type]||'Overig'}</span><span class="entity-name">${esc(e.name)}</span>${e.geography?`<span class="entity-geography">${esc(geographyLabel(e.geography))}</span>`:''}<span class="entity-meta">${e.count} vermeldingen · ${plural(e.episodes,'aflevering','afleveringen')} ↗</span></button>`}
function drawEntities(items,append){
 const list=$('#entities-list');if(!list)return;
 if(append)list.insertAdjacentHTML('beforeend',items.map(entityTile).join(''));
 else list.innerHTML=items.length?items.map(entityTile).join(''):empty(state.locationRegion?'Geen locaties in dit gebied':'Hier komen de genoemde namen',state.locationRegion?'Kies een ander gebied of toon alle locaties.':'Zodra een transcript is geanalyseerd, vind je de entiteiten hier terug.');
 const more=$('#entity-more'),shown=list.querySelectorAll('.entity-tile').length;
 if(more)more.innerHTML=shown<entityTotal?`<button data-entity-more>Toon meer namen</button><span class="count-hint">${shown} van ${entityTotal}</span>`:entityTotal>ENTITY_PAGE?`<span class="count-hint">Alle ${entityTotal} namen staan hierboven.</span>`:'';
}
/* A big project holds tens of thousands of names, so they arrive a page at a
   time: the first screen stays quick and the rest follows on request. */
async function loadEntities(append){
 if(!append)entityOffset=0;
 const data=await api('/entities'+scoped({q:state.q,kind:state.kind,region:state.locationRegion,limit:ENTITY_PAGE,offset:entityOffset}));
 entityTotal=data.total;
 entityOffset+=data.entities.length;
 drawEntities(data.entities,append);
}
function highlightEntityText(text,name){const low=text.toLocaleLowerCase(),needle=name.toLocaleLowerCase();let result='',position=0,index=low.indexOf(needle);while(index!==-1){result+=esc(text.slice(position,index))+'<mark>'+esc(text.slice(index,index+name.length))+'</mark>';position=index+name.length;index=low.indexOf(needle,position)}return result+esc(text.slice(position))}
async function renderEntityDetail(name,kind){
 const data=await api('/entity-mentions'+scoped({name,kind}));
 const category=labels[data.type]||'Entiteit';
 const back=state.project?`#project/${state.project}/entities`:'#entities';
 $('#content').innerHTML=`<a href="${back}" class="back-link">← Terug naar entiteiten</a><div class="detail-head"><span class="eyebrow">${esc(category).toUpperCase()} / VERMELDINGEN${state.project?' IN '+esc(projectName(activeProject())).toUpperCase():''}</span><h1>${esc(data.name)}</h1><p class="subtitle">${data.mentions.length} ${data.mentions.length===1?'alinea':'alinea’s'} met vermeldingen${state.project?' in dit project':' in je hele archief'}.</p></div><section class="panel"><div class="panel-heading"><h2>Alle vermeldingen</h2><span class="count-hint">Klik op een alinea om het transcript op deze plek te openen.</span></div><div class="mention-list">${data.mentions.length?data.mentions.map((mention,index)=>`<a class="entity-mention" href="#episode/${mention.episode_id}?segment=${mention.segment}"><div><span class="mention-number">${index+1}</span><strong>${esc(mention.title)}</strong><span class="mention-date">${mention.published?esc(publishedDate(mention.published))+' · ':''}</span><span class="mention-time">${timecode(mention.start)} ↗${mention.count>1?` · ${mention.count}× genoemd`:''}</span></div><p>${highlightEntityText(mention.text,data.name)}</p></a>`).join(''):empty('Geen vermeldingen gevonden','Deze entiteit heeft nog geen gekoppelde transcriptfragmenten.')}</div></section>`;
}

/* ------------------------------------------------------------------ stemmen */
async function renderSpeakers(){
 const items=await api('/speakers'+scoped());
 const named=items.filter(s=>s.name).length;
 $('#content').innerHTML=projectHeading('speakers','Geef een stem één keer een naam. Audioscraper gebruikt die naam daarna in alle transcripties.')
 +`<div class="speaker-stats"><strong>${items.length}</strong> herkende ${items.length===1?'stem':'stemmen'}${state.project?' in dit project':''} · <strong>${named}</strong> met een naam <button data-backfill-speakers>Herken in bestaande transcripties</button></div><div class="speaker-grid">${items.length?items.map(s=>`<article class="speaker-card"><div class="speaker-card-head"><div class="speaker-avatar">${esc((s.name||s.display_name||'?').slice(0,1).toUpperCase())}</div><div><span class="eyebrow">STEMPROFIEL</span><h2>${esc(s.name||s.display_name)}</h2><p>${s.episodes} ${s.episodes===1?'bestand':'bestanden'} · ${s.segments} fragmenten · ${duration(s.seconds)}</p>${s.analysis_pending?`<span class="speaker-analysis-pending">Entiteitsdetectie in wachtrij</span>`:''}</div></div>${s.sample_available?`<audio class="speaker-audio" controls preload="none" src="/api/speakers/${s.profile_id}/sample"></audio>`:`<p class="count-hint">Geen luisterfragment beschikbaar.</p>`}<form class="speaker-name-form" data-profile-id="${s.profile_id}"><label for="speaker-${s.profile_id}">Naam van deze stem</label><div><input id="speaker-${s.profile_id}" name="name" maxlength="80" required value="${esc(s.name)}" placeholder="${esc(s.display_name)}"><button type="submit">Opslaan</button></div>${!s.name&&s.suggested_name?`<button type="button" class="speaker-suggestion" data-speaker-suggestion="${esc(s.suggested_name)}">Gebruik suggestie: ${esc(s.suggested_name)}</button>`:''}</form><div class="speaker-entities"><h3>Entiteiten in uitspraken</h3>${s.entities.length?`<div class="entity-chips">${s.entities.map(entity=>`<a class="entity-chip" href="#entity/${encodeURIComponent(entity.name)}?type=${encodeURIComponent(entity.type)}${state.project?'&project='+state.project:''}">${esc(entity.name)}<b>${entity.count}</b></a>`).join('')}</div>`:s.analysis_pending?'<p class="count-hint">Entiteiten worden gekoppeld zodra de lopende entiteitsdetectie klaar is.</p>':'<p class="count-hint">Geen entiteiten gekoppeld aan de uitspraken van deze stem.</p>'}</div></article>`).join(''):empty('Nog geen stemmen herkend','Nieuwe transcripties krijgen automatisch stemprofielen. Gebruik de knop hierboven om bestaande transcripties opnieuw te verwerken.')}</div><p class="panel-help speaker-page-help">Stemprofielen worden gedeeld met de rest van de worker: een naam die je hier geeft geldt ook in andere projecten waarin dezelfde stem voorkomt. Bestaande transcripties moeten opnieuw worden beluisterd omdat stemkenmerken niet uit alleen de tekst kunnen worden afgeleid.</p>`;
}

/* ------------------------------------------------------------------ nummers */
function phoneEpisodes(phone){const groups=new Map();for(const mention of phone.mentions){let episode=groups.get(mention.episode_id);if(!episode){episode={id:mention.episode_id,title:mention.title,url:mention.url,kind:mention.kind,published:mention.published,mentions:[]};groups.set(mention.episode_id,episode)}episode.mentions.push(mention)}return [...groups.values()]}
function phoneLookupStatus(phone){
 const labels={pending:'In wachtrij voor automatisch zoeken',searching:'Zoekt openbare bedrijfsgegevens…',not_found:'Nog geen bevestigde match · wordt opnieuw gezocht',error:'Zoeken tijdelijk niet beschikbaar · herhaalpoging volgt'};
 return `<span class="phone-lookup-status">${esc(labels[phone.lookup_status]||labels.pending)}</span>`;
}
const phoneSubjectLabels={person:'Persoon',organization:'Organisatie',service:'Dienst',private_contact:'Privécontact',own_number:'Eigen nummer van beller',invalid:'Ongeldig of transcriptiefout',unknown:'Onbekend'};
const phoneRelationLabels={called_target:'Gebelde partij',submitted_target:'Ingestuurd doelwit',caller_own_number:'Door beller als eigen nummer gegeven',third_party_reference:'Alleen genoemd als derde',invalid_number:'Ongeldig nummer',unclear:'Relatie onduidelijk'};
function phoneContextStatus(phone){const labels={none:'Wacht op eerste openbare zoekactie',pending:'Contextanalyse in wachtrij',searching:'Analyseert omliggend gesprek…',error:'Contextanalyse tijdelijk niet beschikbaar',completed:'Contextanalyse voltooid'};return labels[phone.context_status]||labels.none}
function phoneMatchesStatus(phone,status){const confirmed=phone.company||phone.attribution?.state==='confirmed';const useful=phone.best_candidate&&phone.best_candidate.subject_type!=='unknown'&&phone.best_candidate.confidence>=60;if(status==='mobile')return phone.category==='Mobiel nummer'&&!phone.company;if(status==='review')return phone.category==='Mobiel nummer'&&!confirmed&&phone.attribution?.state!=='rejected'&&useful;if(status==='confirmed')return Boolean(confirmed);if(status==='rejected')return phone.attribution?.state==='rejected';if(status==='unknown')return !confirmed&&phone.attribution?.state!=='rejected'&&!useful;return true}
/* A number filter only redraws the numbers: the entity grid above it can run
   into thousands of tiles and does not depend on the choice. The URL still
   carries it, so refreshing or sharing the page keeps the same view. */
function applyPhoneFilter(){history.replaceState(null,'',phoneFilterHash());renderPhoneSection().catch(error=>toast(error.message))}
function phoneFilterHash(){const params=new URLSearchParams();if(state.phoneRegion)params.set('region',state.phoneRegion);if(state.phoneStatus)params.set('status',state.phoneStatus);return sectionHash('entities',params.toString())}
function phoneAttributionEditor(phone){
 if(phone.company)return '';
 if(phone.category!=='Mobiel nummer')return `<span class="phone-context-state">Contextanalyse wordt alleen voor ongekoppelde mobiele nummers uitgevoerd.</span>`;
 const reviewed=phone.attribution?.state==='confirmed'?phone.attribution:null;
 const rejected=phone.attribution?.state==='rejected';
 const candidate=reviewed||(phone.best_candidate?.subject_type!=='unknown'?phone.best_candidate:null);
 if(rejected)return `<div class="phone-attribution rejected"><strong>Suggestie handmatig afgewezen</strong><span>Automatische kandidaten blijven verborgen totdat de beoordeling wordt gewist.</span><button type="button" data-clear-phone="${esc(phone.number)}">Beoordeling wissen</button></div>`;
 const type=candidate?.subject_type&&candidate.subject_type!=='unknown'?candidate.subject_type:'private_contact';
 const relation=candidate?.relation||'unclear';
 const title=reviewed?'Handmatig bevestigd':candidate&&candidate.subject_type!=='unknown'?(candidate.confidence>=60?'Waarschijnlijke context':'Mogelijke context'):'Nog geen bruikbare context';
 const evidence=candidate?.evidence?`<a class="phone-evidence" href="#episode/${candidate.episode_id}?segment=${candidate.segment}">“${esc(candidate.evidence)}” ↗</a>`:'';
 return `<form class="phone-attribution phone-attribution-form ${reviewed?'confirmed':''}" data-phone="${esc(phone.number)}" data-episode-id="${esc(candidate?.episode_id||'')}" data-segment="${candidate?.segment??''}"><strong>${title}${candidate?.confidence!=null&&!reviewed?` · ${candidate.confidence}%`:''}</strong><span class="phone-context-state">${esc(phoneContextStatus(phone))}</span>${evidence}<label>Type<select name="subject_type">${Object.entries(phoneSubjectLabels).filter(([key])=>key!=='unknown').map(([key,label])=>`<option value="${key}" ${type===key?'selected':''}>${label}</option>`).join('')}</select></label><label>Relatie<select name="relation">${Object.entries(phoneRelationLabels).map(([key,label])=>`<option value="${key}" ${relation===key?'selected':''}>${label}</option>`).join('')}</select></label><label>Naam<input name="label" maxlength="160" value="${esc(candidate?.label||'')}" placeholder="Alleen als de naam duidelijk blijkt"></label><label>Omschrijving<input name="description" maxlength="300" value="${esc(candidate?.description||'')}" placeholder="Bijvoorbeeld: klasgenoot van de inzender"></label><div class="phone-review-actions"><button type="submit">${reviewed?'Wijziging opslaan':'Bevestigen'}</button>${reviewed?`<button type="button" data-clear-phone="${esc(phone.number)}">Bevestiging wissen</button>`:`<button type="button" data-reject-phone="${esc(phone.number)}">Afwijzen</button>`}</div></form>`;
}
/* Numbers are part of what a transcript yielded, so they sit under the
   entities of the same scope instead of on a page of their own. */
let phoneSignature='';
async function renderPhoneSection(){
 if(!$('#phone-panel'))return;
 const selectedRegion=state.phoneRegion,selectedStatus=state.phoneStatus,selectedProject=state.project;
 const [allPhones,regionData]=await Promise.all([api('/phone-numbers'+scoped()),loadPhoneRegionData()]);
 const target=$('#phone-panel');
 if(!target||view!=='entities'||state.project!==selectedProject||state.phoneRegion!==selectedRegion||state.phoneStatus!==selectedStatus)return;
 // The six-second refresh must not rebuild a list somebody is working in, so
 // redraw only when the numbers or the chosen filters actually differ.
 const signature=JSON.stringify([selectedProject,selectedRegion,selectedStatus,allPhones]);
 if(signature===phoneSignature&&target.innerHTML)return;
 phoneSignature=signature;
 if(!allPhones.length){target.innerHTML='';return}
 const phones=allPhones.filter(phone=>matchesPhoneRegion(phone,selectedRegion,regionData)&&phoneMatchesStatus(phone,selectedStatus));
 const regions=[['','Alle regio’s'],['north','Groningen, Drenthe en Friesland'],['Groningen','Groningen'],['Drenthe','Drenthe'],['Friesland','Friesland']];
 const states=[['','Alle nummers'],['mobile','Ongekoppelde 06-nummers'],['review','Te beoordelen suggesties'],['confirmed','Bevestigde koppelingen'],['unknown','Nog onbekend'],['rejected','Afgewezen']];
 target.innerHTML=`<div class="section-head"><h2>Telefoonnummers <small>${allPhones.length}</small></h2></div>`
 +`<div class="toolbar phone-region-toolbar"><label for="phone-region-filter">Regio</label><select id="phone-region-filter" aria-describedby="phone-region-help">${regions.map(([value,label])=>`<option value="${value}" ${selectedRegion===value?'selected':''}>${label}</option>`).join('')}</select><label for="phone-status-filter">Beoordeling</label><select id="phone-status-filter">${states.map(([value,label])=>`<option value="${value}" ${selectedStatus===value?'selected':''}>${label}</option>`).join('')}</select></div><p class="panel-help phone-region-help" id="phone-region-help">Een openbare bedrijfspagina bewijst een bedrijfskoppeling. Transcriptanalyse geeft alleen aan wie of wat waarschijnlijk met het nummer werd bedoeld en vraagt om handmatige bevestiging.</p><div class="panel"><div class="panel-heading"><h2>${phones.length}${selectedRegion||selectedStatus?` van ${allPhones.length}`:''} unieke ${phones.length===1?'nummer':'nummers'}</h2><span class="count-hint">${allPhones.filter(p=>p.company||p.attribution?.state==='confirmed').length} bevestigd · ${allPhones.filter(p=>p.best_candidate&&p.best_candidate.subject_type!=='unknown'&&p.attribution?.state!=='confirmed').length} suggesties</span></div><div class="phone-list">${phones.length?phones.map(phone=>`<article class="phone-row" data-phone="${esc(phone.number)}"><div><strong class="phone-number">${esc(phone.display)}</strong><span class="phone-category">${esc(phone.category)} · ${phone.mentions.length} ${phone.mentions.length===1?'vermelding':'vermeldingen'} in ${phoneEpisodes(phone).length} ${phoneEpisodes(phone).length===1?'bestand':'bestanden'}</span></div><div class="phone-company">${phone.company?`<strong>${esc(phone.company)}</strong>${phone.location?`<span class="phone-location">Locatie: ${esc(phone.location)}</span>`:''}<span>Openbaar bevestigd · <a href="${safeUrl(phone.source_url)}" target="_blank" rel="noopener noreferrer">${esc(phone.source_label)} ↗</a>${phone.verified?` · Gecontroleerd ${esc(new Date(phone.verified*1000).toLocaleDateString('nl-NL'))}`:''}${phone.kvk_number?` · <a href="https://www.kvk.nl/zoeken/?q=${encodeURIComponent(phone.kvk_number)}" target="_blank" rel="noopener noreferrer">KvK ${esc(phone.kvk_number)} ↗</a>`:''}</span>`:phone.area?`<strong>Netnummergebied</strong><span>${esc(phone.area[0])} · ${esc(phone.area[1])}</span>`:'<span class="count-hint">Nog geen betrouwbare bedrijfskoppeling</span>'}${!phone.company?phoneLookupStatus(phone):''}${selectedRegion?`<span class="phone-region-match">${esc(phoneRegion(phone,regionData).label)}</span>`:''}${phoneAttributionEditor(phone)}</div><div class="phone-mentions">${phoneEpisodes(phone).map(episode=>`<section class="phone-episode"><a href="#episode/${episode.id}?segment=${episode.mentions[0].segment}" title="Open transcript"><strong>${esc(episode.title)}</strong>${episode.published?` · ${esc(publishedDate(episode.published))}`:''} <small>${episode.mentions.length}×</small></a>${episode.mentions.map(m=>`<a class="phone-context" href="#episode/${m.episode_id}?segment=${m.segment}"><b>${timecode(m.start)}</b><span>“${esc(m.context)}”</span></a>`).join('')}</section>`).join('')}</div></article>`).join(''):empty('Geen nummers voor deze filters','Kies een andere regio of beoordeling.')}</div></div>`;
 $('#phone-region-filter').onchange=e=>{state.phoneRegion=e.target.value;applyPhoneFilter()};
 $('#phone-status-filter').onchange=e=>{state.phoneStatus=e.target.value;applyPhoneFilter()};
}

/* ------------------------------------------------------- projectinstellingen */
async function renderSettings(){
 const project=await api('/projects/'+state.project);
 projectMap.set(project.id,project);
 const limits=[[1,'Nieuwste aflevering'],[3,'Nieuwste 3 afleveringen'],[10,'Nieuwste 10 afleveringen'],[25,'Nieuwste 25 afleveringen'],[50,'Nieuwste 50 afleveringen'],[0,'Alle beschikbare afleveringen']];
 $('#content').innerHTML=projectHeading('settings','Naam, kleur en bronbeheer van dit project.')
 +`<section class="panel"><div class="panel-heading"><h2>Projectgegevens</h2><span class="count-hint">Alleen zichtbaar in dit dashboard</span></div>
 <form class="project-form" id="project-form"><label for="project-form-name">Projectnaam</label><input id="project-form-name" name="name" maxlength="120" value="${esc(project.name||'')}" placeholder="${esc(project.title)}">
 <label for="project-form-description">Omschrijving</label><input id="project-form-description" name="description" maxlength="400" value="${esc(project.description||'')}" placeholder="Waar gaat dit project over?">
 <fieldset class="accent-picker"><legend>Kleur</legend>${Object.entries(accents).map(([key,label])=>`<label class="accent-option ${accentClass(key)} ${project.accent===key?'selected':''}"><input type="radio" name="accent" value="${key}" ${project.accent===key?'checked':''}><span></span>${label}</label>`).join('')}</fieldset>
 <button type="submit" class="primary">Opslaan</button></form></section>
 <section class="panel"><div class="panel-heading"><h2>Bron</h2><span class="count-hint">${esc(sourceTypes[project.kind]||project.kind)}</span></div>
 <div class="settings-rows"><div class="settings-row"><span>Bron-URL</span><a href="${safeUrl(project.url)}" target="_blank" rel="noopener">${esc(project.url)} ↗</a></div>
 <div class="settings-row"><span>Eerste import</span><b>${esc((limits.find(([value])=>value===project.initial_limit)||[0,project.initial_limit+' afleveringen'])[1])}</b></div>
 <div class="settings-row"><span>Laatste controle</span><b>${project.syncing?'Wordt nu gecontroleerd…':esc(date(project.checked))}</b></div>
 <div class="settings-row"><span>Aangemaakt</span><b>${esc(date(project.created))}</b></div>
 <div class="settings-row"><span>Verwerkingsvolgorde</span><b>${project.priority+1}e van ${projects.length} · <a href="#projects">volgorde aanpassen ↗</a></b></div>
 <div class="settings-row"><span>Inhoud</span><b>${plural(project.episode_count,'aflevering','afleveringen')} · ${plural(project.transcripts,'transcriptie','transcripties')} · ${plural(project.entities,'entiteit','entiteiten')} · ${plural(project.phone_numbers,'nummer','nummers')}</b></div></div>
 ${project.error?`<p class="error">${esc(project.error)}</p>`:''}
 <div class="detail-actions">${project.pollable?`<button data-sync="${project.id}" ${!project.enabled||project.syncing?'disabled':''}>↻ Controleer nu</button><button data-toggle="${project.id}" data-enabled="${!project.enabled}">${project.enabled?'Project pauzeren':'Project hervatten'}</button>`:'<p class="count-hint">Een losse video heeft geen bron om opnieuw te controleren.</p>'}</div>
 <p class="panel-help">Pauzeren stopt nieuwe ontdekkingen in dit project. Werk dat al in de wachtrij staat loopt door; gebruik <strong>Pauzeer alles</strong> bovenaan om alle verwerking stil te leggen.</p></section>
 <section class="panel danger"><div class="panel-heading"><h2>Project verwijderen</h2><span class="count-hint">Kan niet ongedaan worden gemaakt</span></div>
 <p class="panel-help">Hiermee ${project.episode_count===1?'verdwijnt de aflevering':'verdwijnen de '+project.episode_count+' afleveringen'} van dit project met hun transcripties, entiteiten, stemtoewijzingen en nummervermeldingen. Handmatig bevestigde nummerkoppelingen blijven staan omdat ze voor alle projecten gelden.</p>
 <button class="danger-button" data-delete-project="${project.id}" data-name="${esc(projectName(project))}">Verwijder dit project</button></section>`;
 $('#project-form').onsubmit=async event=>{
  event.preventDefault();
  const version=routeVersion;
  const button=event.target.querySelector('[type="submit"]');button.disabled=true;
  try{
   const data=new FormData(event.target);
   await api('/projects/'+project.id,{method:'PATCH',body:JSON.stringify({name:String(data.get('name')||''),description:String(data.get('description')||''),accent:String(data.get('accent')||project.accent)})});
   await loadProjects();
   if(version!==routeVersion)return;  // the user already navigated away
   toast('Project bijgewerkt.');await renderSettings();renderNav('settings');showBreadcrumb('settings');
  }catch(error){toast(error.message);button.disabled=false}
 };
}

/* ----------------------------------------------------------------- transcript */
async function renderDetail(id){
 const e=await api('/episodes/'+id);detail=e;
 enterProjectContext(e.project,'library');
 const back=e.project?`#project/${e.project.id}/library`:'#library';
 const transcriptHelp=e.kind==='youtube'?'Klik op een tijdcode om YouTube in een nieuw tabblad te openen op precies dat moment. De transcripttekst kun je gewoon selecteren en kopiëren.':'Klik op een tijdcode om het betreffende fragment te beluisteren.';
 $('#content').innerHTML=`<a href="${back}" class="back-link">← Terug naar ${e.project?esc(e.project.display_name):'bibliotheek'}</a><div class="detail-head"><span class="eyebrow">${e.project?`<a href="#project/${e.project.id}">${esc(e.project.display_name).toUpperCase()}</a> / `:''}${(episodeTags[e.kind]||'◉ PODCAST').split(' ')[1]} / TRANSCRIPT</span><h1>${esc(e.title)}</h1><div class="detail-meta"><span>${esc(e.author||'')}</span>${e.published?`<span>Gepubliceerd: ${esc(publishedDate(e.published))}</span>`:''}<span>${duration(e.duration)}</span>${e.language?`<span>Taal: ${esc(e.language.toUpperCase())}</span>`:''}${badge(e.status)}<a href="${safeUrl(e.url)}" target="_blank" rel="noopener">Open originele bron ↗</a></div></div>${e.status!=='completed'?`<div class="progress-box"><strong>${esc(e.stage)}</strong><p>Dit scherm wordt automatisch bijgewerkt. Verwerking gaat ook door wanneer je het sluit.</p>${e.error?`<p class="error">${esc(e.error)}</p>`:''}${e.status==='failed'?`<button data-retry="${e.id}">Opnieuw proberen</button>`:''}</div>`:''}${e.audio_available?`<div class="audio-panel"><span class="audio-label">◉ &nbsp; Luister terug</span><audio id="player" controls preload="none" src="/api/episodes/${e.id}/audio"></audio></div>`:''}<section class="panel"><div class="panel-heading"><h2>Genoemde entiteiten</h2><span class="count-hint">${e.entities.length} unieke namen</span></div><p class="panel-help">Klik op een naam om de vermeldingen in het transcript te markeren. Automatische herkenning kan fouten bevatten of namen missen.</p>${e.entities.length?Object.entries(labels).map(([type,label])=>{const group=e.entities.filter(x=>x.type===type);return group.length?`<div class="entity-group"><h3>${label} · ${group.length}</h3><div class="entity-chips">${group.map(x=>`<button class="entity-chip" data-highlight="${esc(x.name)}">${esc(x.name)}<b>${x.count}</b></button>`).join('')}</div></div>`:''}).join(''):`<p class="count-hint">${e.status==='completed'?'Geen benoemde entiteiten gevonden.':'De entiteiten verschijnen na de analyse.'}</p>`}</section><section class="panel" id="transcript-panel"><div class="panel-heading"><h2>Volledig transcript</h2><span class="count-hint">${e.segments.length} fragmenten</span></div><p class="panel-help">${transcriptHelp}</p>${e.speaker_error?`<p class="speaker-warning">Stemherkenning lukte bij deze opname niet: ${esc(e.speaker_error)}</p>`:''}${e.speakers?.length?`<div class="speaker-filter" aria-label="Filter transcript op spreker"><button class="selected" data-speaker-filter="">Alle sprekers</button>${e.speakers.map(s=>`<button data-speaker-filter="${esc(s.name)}">${s.id?'●':'◌'} ${esc(s.name)} <small>${s.segments}</small></button>`).join('')}</div>`:''}${e.transcript?`<div class="transcript-toolbar"><input id="transcript-search" type="search" placeholder="Zoek in dit transcript…" aria-label="Zoek in dit transcript"><a href="/api/episodes/${e.id}/transcript.txt">↓ Download tekst</a></div><div id="selection-note"></div><div id="transcript"></div>`:'<p class="count-hint">Het volledige transcript verschijnt hier zodra Whisper klaar is.</p>'}</section>${e.status==='completed'?`<div class="detail-actions"><button data-recognize-speakers="${e.id}">${e.speakers?.length?'Stemmen opnieuw herkennen':'Stemmen herkennen'}</button><button data-retry="${e.id}">Entiteiten opnieuw analyseren</button></div>`:''}`;
 if(e.transcript){drawTranscript();$('#transcript-search').oninput=event=>{selectedEntity='';debounce(()=>drawTranscript(event.target.value))}}
}
function drawTranscript(query=selectedEntity){if(!detail||!$('#transcript'))return;const entity=detail.entities.find(e=>e.name===selectedEntity);const selected=entity?new Map():null;if(entity)for(const occurrence of entity.occurrences)for(const o of occurrence.spans||[occurrence]){if(!selected.has(o.segment))selected.set(o.segment,[]);selected.get(o.segment).push(o)}let hits=0;$('#transcript').innerHTML=detail.segments.map((s,i)=>{let ranges=[];if(selected)ranges=(selected.get(i)||[]).map(o=>[o.offset,o.end_offset]);else if(query){let p=0;const low=s.text.toLocaleLowerCase(),needle=query.toLocaleLowerCase();while((p=low.indexOf(needle,p))!==-1){ranges.push([p,p+query.length]);p+=query.length}}ranges.sort((a,b)=>a[0]-b[0]);let text='',last=0;for(const [a,b] of ranges){if(a<last)continue;text+=esc(s.text.slice(last,a))+'<mark>'+esc(s.text.slice(a,b))+'</mark>';last=b;hits++}text+=esc(s.text.slice(last));const timestampUrl=detail.kind==='youtube'?youtubeTimestampUrl(detail.url,s.start):'';const timecodeMarkup=timestampUrl?`<a class="timecode" href="${esc(timestampUrl)}" target="_blank" rel="noopener noreferrer" title="Open YouTube vanaf ${timecode(s.start)}">${timecode(s.start)}</a>`:`<button class="timecode" data-seek="${s.start}" title="Beluister vanaf ${timecode(s.start)}">${timecode(s.start)}</button>`;const hidden=selectedSpeaker&&s.speaker!==selectedSpeaker;return `<div class="segment ${ranges.length?'hit':''} ${hidden?'speaker-hidden':''}" id="segment-${i}">${timecodeMarkup}<div class="segment-copy">${s.speaker?`<button class="speaker-label" data-speaker-filter="${esc(s.speaker)}">${s.speaker_id?'●':'◌'} ${esc(s.speaker)}</button>`:''}<p>${text}</p></div></div>`}).join('');$('#selection-note').innerHTML=query?`<div class="selection-note">${hits} vermeldingen van “${esc(query)}” <button data-next-hit>Volgende ↓</button><button data-clear-highlight>Wis ×</button></div>`:'';document.querySelectorAll('[data-highlight]').forEach(b=>b.classList.toggle('selected',b.dataset.highlight===selectedEntity));document.querySelectorAll('[data-speaker-filter]').forEach(b=>b.classList.toggle('selected',b.dataset.speakerFilter===selectedSpeaker))}
function debounce(fn){clearTimeout(searchTimer);searchTimer=setTimeout(()=>Promise.resolve(fn()).catch(e=>toast(e.message)),250)}

/* ------------------------------------------------------------------ refresh */
async function refresh(){
 try{
  if(view==='projects'){const previous=JSON.stringify(projects.map(p=>[p.id,p.episode_count,p.transcripts,p.entities,p.phone_numbers,p.syncing,p.enabled,p.error]));await loadProjects();if(JSON.stringify(projects.map(p=>[p.id,p.episode_count,p.transcripts,p.entities,p.phone_numbers,p.syncing,p.enabled,p.error]))!==previous&&$('.project-grid')){$('.project-grid').innerHTML=projectGrid();applyWidths()}await Promise.all([renderTranscriptProgress(),renderEntityProgress()])}
  else if(view==='library'){await loadEpisodes();const s=await api('/stats'+scoped());setCounts(s);if($('#stats'))$('#stats').innerHTML=stat('In de bibliotheek',s.total,s.completed+' volledig verwerkt','▤')+stat('In verwerking',s.active,s.failed?s.failed+' hebben aandacht nodig':'Automatisch op de achtergrond','≋')+stat('Gevonden entiteiten',s.entities,'Personen, organisaties en meer','◎');await Promise.all([renderTranscriptProgress(),renderEntityProgress()])}
  else if(view==='overview')await Promise.all([renderTranscriptProgress(),renderEntityProgress()]);
  else if(view==='entities'&&!document.activeElement?.closest('.phone-attribution-form')&&!['phone-region-filter','phone-status-filter'].includes(document.activeElement?.id)&&!window.getSelection()?.toString()){const scroll=window.scrollY;await renderPhoneSection();window.scrollTo(0,scroll)}
  else if(view==='detail'&&detail&&detail.status!=='completed'){const e=await api('/episodes/'+detail.id);if(e.updated!==detail.updated){const scroll=window.scrollY;const player=$('#player');const playing=player&&!player.paused;const position=player?.currentTime||0;await renderDetail(detail.id);if($('#player')){$('#player').currentTime=position;if(playing)$('#player').play().catch(()=>{})}window.scrollTo(0,scroll)}}
 }catch(e){console.warn(e)}
}
async function worker(){try{const s=await api('/worker');$('#worker').innerHTML=`<span class="dot ${s.online?'green':'orange'}"></span><div>${s.online?'Whisper-worker online':'Worker niet bereikbaar'}<small>${s.online?'large-v3 · lokaal op GPU':'Wachtrij wordt bewaard'}</small></div>`}catch{}}

/* ----------------------------------------------------------------- handlers */
document.addEventListener('click',async e=>{
 const b=e.target.closest('button');if(!b)return;
 try{
  if(b.hasAttribute('data-add')){$('#add-dialog').showModal();$('#source-url').focus()}
  if(b.hasAttribute('data-entity-more')){b.disabled=true;await loadEntities(true)}
  if(b.dataset.sync){b.disabled=true;await api('/projects/'+b.dataset.sync+'/sync',{method:'POST'});toast('Bron wordt gecontroleerd.');await loadProjects();await route()}
  if(b.dataset.toggle){await api('/projects/'+b.dataset.toggle,{method:'PATCH',body:JSON.stringify({enabled:b.dataset.enabled==='true'})});await loadProjects();await route()}
  if(b.dataset.moveProject){
   const index=projects.findIndex(project=>project.id===b.dataset.moveProject);
   const target=index+(b.dataset.direction==='up'?-1:1);
   if(index<0||target<0||target>=projects.length)return;
   const ids=projects.map(project=>project.id);
   ids.splice(target,0,...ids.splice(index,1));
   await api('/projects/order',{method:'PUT',body:JSON.stringify({ids})});
   await loadProjects();
   if($('.project-grid')){$('.project-grid').innerHTML=projectGrid();applyWidths()}else await route();
   toast('Verwerkingsvolgorde bijgewerkt.');
  }
  if(b.dataset.deleteProject){
   const name=b.dataset.name;
   const answer=prompt(`Dit verwijdert het project “${name}” met alle transcripties en entiteiten.\n\nTyp de projectnaam om te bevestigen:`);
   if(answer===null)return;
   if(answer.trim()!==name){toast('De naam kwam niet overeen; er is niets verwijderd.');return}
   const result=await api('/projects/'+b.dataset.deleteProject,{method:'DELETE'});
   toast(`Project verwijderd · ${plural(result.episodes_removed,'aflevering','afleveringen')} opgeruimd.`);
   location.hash='projects';
  }
  if(b.hasAttribute('data-backfill-speakers')){if(confirm('Alle bestaande transcripties zonder stemprofielen opnieuw verwerken? Dit kan geruime tijd duren.')){const result=await api('/speakers/backfill',{method:'POST'});toast(result.queued?result.queued+' transcripties staan in de wachtrij.':'Alle transcripties hebben al stemprofielen.')}}
  if(b.dataset.speakerSuggestion){const form=b.closest('form');form.elements.name.value=b.dataset.speakerSuggestion;form.elements.name.focus()}
  if(b.dataset.recognizeSpeakers){await api('/episodes/'+b.dataset.recognizeSpeakers+'/speakers',{method:'POST'});toast('Stemherkenning staat in de wachtrij.');await renderDetail(b.dataset.recognizeSpeakers)}
  if(b.dataset.retry){await api('/episodes/'+b.dataset.retry+'/retry',{method:'POST'});toast('Verwerking staat in de wachtrij.');await renderDetail(b.dataset.retry)}
  if(b.dataset.rejectPhone){await api('/phone-numbers/'+b.dataset.rejectPhone+'/attribution',{method:'PATCH',body:JSON.stringify({state:'rejected'})});toast('Suggestie afgewezen.');await renderPhoneSection()}
  if(b.dataset.clearPhone){await api('/phone-numbers/'+b.dataset.clearPhone+'/attribution',{method:'PATCH',body:JSON.stringify({state:'clear'})});toast('Handmatige beoordeling gewist.');await renderPhoneSection()}
  if(b.hasAttribute('data-speaker-filter')){selectedSpeaker=b.dataset.speakerFilter;drawTranscript($('#transcript-search')?.value||selectedEntity)}
  if(b.dataset.highlight){selectedEntity=selectedEntity===b.dataset.highlight?'':b.dataset.highlight;$('#transcript-search').value='';drawTranscript();$('.segment.hit')?.scrollIntoView({behavior:'smooth',block:'center'})}
  if(b.hasAttribute('data-clear-highlight')){selectedEntity='';$('#transcript-search').value='';drawTranscript()}
  if(b.hasAttribute('data-next-hit')){const hits=[...document.querySelectorAll('.segment.hit')];const next=hits.find(x=>x.getBoundingClientRect().top>150)||hits[0];next?.scrollIntoView({behavior:'smooth',block:'center'})}
  if(b.dataset.seek&&$('#player')){$('#player').currentTime=Number(b.dataset.seek);$('#player').play().catch(()=>toast('Start het afspelen met de audiospeler.'))}
  if(b.dataset.entityBrowse){location.hash='entity/'+encodeURIComponent(b.dataset.entityBrowse)+'?type='+encodeURIComponent(b.dataset.entityType)+(state.project?'&project='+state.project:'')}
  if(b.hasAttribute('data-clear-filter')){location.hash=sectionHash('library')}
 }catch(error){toast(error.message)}
});
document.addEventListener('submit',async event=>{const phoneForm=event.target.closest('.phone-attribution-form');if(phoneForm){event.preventDefault();const button=phoneForm.querySelector('[type="submit"]');button.disabled=true;try{const data=new FormData(phoneForm);const payload={state:'confirmed',subject_type:data.get('subject_type'),relation:data.get('relation'),label:String(data.get('label')||'').trim(),description:String(data.get('description')||'').trim()};if(phoneForm.dataset.episodeId&&phoneForm.dataset.segment!==''){payload.episode_id=phoneForm.dataset.episodeId;payload.segment=Number(phoneForm.dataset.segment)}await api('/phone-numbers/'+phoneForm.dataset.phone+'/attribution',{method:'PATCH',body:JSON.stringify(payload)});toast('Nummercontext handmatig bevestigd.');await renderPhoneSection()}catch(error){toast(error.message);button.disabled=false}return}const form=event.target.closest('.speaker-name-form');if(!form)return;event.preventDefault();const button=form.querySelector('[type="submit"]');button.disabled=true;try{const profileId=form.dataset.profileId;const name=form.elements.name.value.trim();await api('/speakers/'+profileId,{method:'PATCH',body:JSON.stringify({name})});toast('Naam opgeslagen en in alle transcripties bijgewerkt.');await renderSpeakers()}catch(error){toast(error.message);button.disabled=false}});
$('#close-dialog').onclick=()=>$('#add-dialog').close();$('#add-dialog').onclick=e=>{if(e.target===$('#add-dialog')){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)e.target.close()}};
function sourceHelp(){const kind=$('#source-kind').value;const help={auto:'De URL bepaalt automatisch of je een kanaal, kanaaltab, afspeellijst, video of RSS-feed toevoegt.',youtube:'De volledige kanaalpagina wordt gevolgd: video’s, afgeronde live-uitzendingen en Shorts.',"youtube-videos":'Alleen de gewone video-tab van dit kanaal wordt gevolgd.',"youtube-streams":'Alleen afgeronde live-uitzendingen worden gevolgd; lopende en ingeplande streams worden overgeslagen.',"youtube-shorts":'Alleen de Shorts-tab van dit kanaal wordt gevolgd.',"youtube-playlist":'Gebruik een YouTube-afspeellijstlink met `list=`. Nieuwe items in die lijst worden gesynchroniseerd.',rss:'Gebruik de openbare RSS-feed van een podcast.',meetings:'Gebruik het vergaderarchief van een gemeente, bijvoorbeeld https://hoogeveen.raadsinformatie.nl/, https://gemeenteraad.groningen.nl/ of https://channel.royalcast.com/gemeenteopsterland/#!/archived. Ondersteunde platforms: Notubiz, GemeenteOplossingen en Company Webcast.'};$('#source-help').textContent=help[kind]};
$('#source-kind').onchange=sourceHelp;
$('#add-form').onsubmit=async e=>{
 e.preventDefault();const button=$('#submit-source');button.disabled=true;$('#form-error').textContent='';
 try{
  const result=await api('/projects',{method:'POST',body:JSON.stringify({url:$('#source-url').value,kind:$('#source-kind').value,initial_limit:Number($('#source-limit').value),name:$('#project-name').value,description:$('#project-description').value})});
  $('#add-dialog').close();
  for(const id of ['source-url','project-name','project-description'])$('#'+id).value='';
  $('#source-kind').value='auto';$('#source-limit').value='3';sourceHelp();
  toast(result.kind==='video'?'Video toegevoegd als eigen project.':'Project aangemaakt. Afleveringen worden opgehaald.');
  await loadProjects();
  const target=result.project_id||result.id;
  if(target)location.hash='project/'+target;else location.hash='projects';
  if(location.hash==='#project/'+target)await route();
 }catch(error){$('#form-error').textContent=error.message}
 finally{button.disabled=false}
};
window.addEventListener('hashchange',route);route();worker();refreshProcessingState();setInterval(worker,30000);setInterval(refreshProcessingState,6000);
