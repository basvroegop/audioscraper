const { chromium } = require('@playwright/test');
const BASE = process.env.BASE_URL || process.env.AUDIOSCRAPER_ORIGIN || 'http://127.0.0.1:8788';
const OUT = process.env.OUT_DIR || '/app/test-results';

(async () => {
 const browser = await chromium.launch({ headless: true, args: ['--no-sandbox'] });
 const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
 const errors = []; page.on('pageerror', e => errors.push(String(e)));
 const load = async () => (await page.request.get(BASE + '/api/projects')).json();
 // The library fills its counter after the first render, so never read it early.
 const counted = async () => {
  await page.waitForFunction(() => /\d+ afleveringe?n?/.test(document.querySelector('#result-count')?.textContent || ''));
  return Number((await page.locator('#result-count').textContent()).split(' ')[0]);
 };

 const projects = await load();
 if (projects.length < 2) throw Error('Deze controle verwacht minstens twee projecten.');
 const sorted = [...projects].sort((a, b) => b.episode_count - a.episode_count);
 const [biggest, smallest] = [sorted[0], sorted[sorted.length - 1]];
 if (!biggest.episode_count) throw Error('Geen enkel project heeft afleveringen.');

 // 1. The hub lists every project as its own environment.
 await page.goto(BASE);
 await page.getByRole('heading', { name: 'Je projecten' }).waitFor();
 const cards = page.locator('.project-card');
 if (await cards.count() !== projects.length) throw Error('Niet elk project staat op de hub.');
 await page.screenshot({ path: OUT + '/projects-hub-desktop.png', fullPage: true });

 // 2. The processing order is editable and the queue numbers follow it.
 const ranks = async () => (await page.locator('.project-card .project-rank').allTextContents()).join(',');
 if (await ranks() !== projects.map((_, index) => index + 1).join(','))
  throw Error('De wachtrijnummers lopen niet op.');
 const first = page.locator('.project-card').first();
 const firstName = (await first.locator('h3').textContent()).trim();
 if (!(await first.locator('[data-direction="up"]').isDisabled()))
  throw Error('Het bovenste project kan nog omhoog.');
 if (!(await page.locator('.project-card').last().locator('[data-direction="down"]').isDisabled()))
  throw Error('Het onderste project kan nog omlaag.');
 await first.locator('[data-direction="down"]').click();
 await page.locator('#toast', { hasText: 'Verwerkingsvolgorde' }).waitFor();
 await page.locator('.project-card').nth(1).locator('h3').filter({ hasText: firstName }).waitFor();
 const moved = await load();
 if (moved[1].display_name.trim() !== firstName) throw Error('De nieuwe volgorde is niet bewaard.');
 if (moved.map(p => p.priority).join(',') !== moved.map((_, index) => index).join(','))
  throw Error('De opgeslagen volgorde is niet aaneengesloten.');
 if (await ranks() !== moved.map((_, index) => index + 1).join(','))
  throw Error('De wachtrijnummers volgen de nieuwe volgorde niet.');
 await page.locator('.project-card', { hasText: firstName }).locator('[data-direction="up"]').click();
 await page.locator('#toast', { hasText: 'Verwerkingsvolgorde' }).waitFor();
 await page.locator('.project-card').first().locator('h3').filter({ hasText: firstName }).waitFor();
 if ((await load()).map(p => p.id).join(',') !== projects.map(p => p.id).join(','))
  throw Error('De oorspronkelijke volgorde is niet hersteld.');

 // 3. Opening a project enters its own environment.
 await page.locator(`.project-card a.project-open[href="#project/${biggest.id}"]`).click();
 await page.waitForURL('**#project/' + biggest.id);
 await page.getByRole('heading', { name: 'Overzicht', exact: true }).waitFor();
 if (!(await page.locator('#scope-label').textContent()).includes(biggest.display_name.toUpperCase()))
  throw Error('De zijbalk toont het actieve project niet.');
 await page.locator('.episode-card').first().waitFor();
 await page.screenshot({ path: OUT + '/project-overview-desktop.png', fullPage: true });

 // 4. The project library only holds this project's episodes.
 await page.locator('#scope-nav a[data-nav="library"]').click();
 await page.waitForURL('**#project/' + biggest.id + '/library');
 if (await counted() !== biggest.episode_count)
  throw Error(`Bibliotheek toont een ander aantal dan de ${biggest.episode_count} van dit project.`);
 if (await page.locator('.project-chip').count()) throw Error('Binnen een project hoort geen projectlabel op de kaarten.');
 await page.screenshot({ path: OUT + '/project-library-desktop.png', fullPage: false });

 // 5. Entities stay inside the project, with that project's numbers below them.
 await page.locator('#scope-nav a[data-nav="entities"]').click();
 await page.locator('.entity-grid').waitFor();
 await page.screenshot({ path: OUT + '/project-entities-desktop.png', fullPage: false });

 // 6. Each project is its own sidebar link and moves the whole dashboard along.
 await page.locator('#project-nav a[href="#project/' + smallest.id + '"]').click();
 await page.waitForURL('**#project/' + smallest.id);
 await page.getByRole('heading', { name: 'Overzicht', exact: true }).waitFor();
 await page.locator('#scope-nav a[data-nav="library"]').click();
 if (await counted() !== smallest.episode_count) throw Error('Het tweede project toont niet zijn eigen afleveringen.');

 // 7. Project settings rename the environment and colour it.
 await page.locator('#scope-nav a[data-nav="settings"]').click();
 await page.getByRole('heading', { name: 'Projectgegevens' }).waitFor();
 await page.fill('#project-form-name', 'Testnaam voor controle');
 await page.locator('.accent-option', { hasText: 'Pruim' }).click();
 await page.locator('#project-form button[type="submit"]').click();
 await page.locator('#scope-label', { hasText: 'TESTNAAM VOOR CONTROLE' }).waitFor();
 await page.screenshot({ path: OUT + '/project-settings-desktop.png', fullPage: true });
 await page.fill('#project-form-name', smallest.name || '');
 await page.locator('.accent-option', { hasText: 'Diepgroen' }).click();
 await page.locator('#project-form button[type="submit"]').click();
 await page.locator('#scope-label', { hasText: (smallest.name || smallest.title).toUpperCase() }).waitFor();

 // 8. The combined view labels which project each episode came from.
 await page.locator('.project-nav a[data-nav="projects"]').click();
 await page.getByRole('heading', { name: 'Je projecten' }).waitFor();
 await page.locator('#scope-nav a[data-nav="library"]').click();
 if (await counted() !== projects.reduce((n, p) => n + p.episode_count, 0))
  throw Error('Het gezamenlijke overzicht mist afleveringen.');
 if (!(await page.locator('.project-chip').count())) throw Error('Het gezamenlijke overzicht mist projectlabels.');
 await page.screenshot({ path: OUT + '/all-projects-library-desktop.png', fullPage: false });

 // 9. An episode opened from anywhere shows and restores its project.
 await page.locator('.episode-card').first().click();
 await page.getByRole('heading', { name: 'Volledig transcript', exact: true }).waitFor();
 if (!(await page.locator('.back-link').getAttribute('href')).startsWith('#project/'))
  throw Error('Het transcript verwijst niet terug naar zijn project.');
 if (!(await page.locator('#breadcrumb').textContent()).includes('Projecten')) throw Error('Kruimelpad mist het project.');
 await page.screenshot({ path: OUT + '/episode-in-project-desktop.png', fullPage: false });

 // 10. Adding a project still validates the source.
 await page.goto(BASE + '/#projects');
 await page.getByRole('heading', { name: 'Je projecten' }).waitFor();
 await page.getByRole('button', { name: 'Nieuw project', exact: false }).first().click();
 await page.getByLabel('Bron-URL').fill('http://127.0.0.1/feed');
 await page.getByRole('button', { name: 'Project aanmaken', exact: false }).click();
 await page.locator('#form-error').filter({ hasText: 'openbare' }).waitFor();
 await page.getByRole('button', { name: 'Sluiten', exact: true }).click();

 // 11. Mobile.
 await page.setViewportSize({ width: 390, height: 844 });
 await page.goto(BASE + '/#projects');
 await page.getByRole('heading', { name: 'Je projecten' }).waitFor();
 await page.screenshot({ path: OUT + '/projects-hub-mobile.png', fullPage: true });
 await page.goto(BASE + '/#project/' + biggest.id);
 await page.locator('.episode-card').first().waitFor();
 await page.screenshot({ path: OUT + '/project-overview-mobile.png', fullPage: true });
 if (await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)) throw Error('Mobiele horizontale overflow');

 if (errors.length) throw Error(errors.join('\n'));
 console.log('Projectcontroles geslaagd: hub, wachtrijvolgorde, projectomgeving, wisselen, instellingen, gezamenlijk overzicht, transcript, mobiel; geen JS-fouten.');
 await browser.close();
})().catch(e => { console.error(e); process.exit(1); });
