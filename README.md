# Audioscraper

Een lokaal luisterarchief voor YouTube-kanalen, losse YouTube-video's en podcast-RSS-feeds. Downloadt audio, maakt een volledig transcript via de bestaande Whisper-worker en herkent genoemde personen, organisaties/instanties, locaties, producten/werken, evenementen en andere namen.

**Dashboard:** https://audioscraper.vroeg.app — Authelia, uitsluitend de gebruikers `bas` en `roos`.

## Projecten

Iedere bron is één project met een eigen omgeving: de afleveringen, transcripties, gevonden entiteiten, herkende stemmen en telefoonnummers van die bron staan alleen daar. Een losse YouTube-video is ook een project. Het dashboard opent op **Alle projecten**: per project een kaart met de status van de bron, het aantal afleveringen, transcripties, entiteiten en nummers, en een voortgangsbalk.

- **Wissel van project** door het in de zijbalk aan te klikken: onder *Alle projecten* staat elk project met zijn eigen kleur en aantal afleveringen. De rest van de zijbalk — Overzicht, Bibliotheek, Entiteiten, Stemmen, Instellingen — blijft daarna binnen dat project.
- Kies **Alle projecten** om alles samen te zien. In dat gezamenlijke overzicht draagt elke aflevering een label met het project waar ze vandaan komt.
- Open je een transcript vanuit een zoekresultaat of telefoonnummer, dan zet het dashboard de zijbalk meteen in het bijbehorende project.
- Bij **Instellingen** geef je een project een eigen naam, omschrijving en kleur, zie je zijn plaats in de verwerkingswachtrij, controleer of pauzeer je de bron, en kun je het hele project verwijderen. Verwijderen vraagt om de projectnaam en wist de afleveringen, transcripties, entiteiten en nummervermeldingen van dat project. Handmatig bevestigde nummerkoppelingen blijven staan; die gelden voor alle projecten.
- Stemprofielen zijn gedeeld met de worker: een naam die je aan een stem geeft, geldt ook in andere projecten waarin dezelfde stem voorkomt.
- Achtergrondverwerking gebruikt één wachtrij en één GPU, maar werkt de projecten af in de volgorde die je zelf bepaalt. **Pauzeer alles** bovenaan legt alles stil; een gepauzeerd project stopt alleen nieuwe ontdekkingen in dat project, niet het werk dat al in de wachtrij staat.

### Verwerkingsvolgorde

Op **Alle projecten** staan de projecten in de volgorde waarin de wachtrij ze afwerkt; het nummer op de projectkaart is de plaats in die rij. Verschuif een project met ↑ en ↓, bijvoorbeeld om raadsvergaderingen vóór een kanaal met een grote achterstand te zetten.

- Een project wordt **helemaal afgemaakt** voordat het volgende aan de beurt is: eerst al zijn transcripties, daarna zijn entiteitsdetectie.
- Binnen een project gaat de **nieuwste aflevering eerst**, op publicatiedatum en niet op het moment van toevoegen. De bibliotheek toont dezelfde volgorde. Omdat de drie bronsoorten hun datum ieder anders noteren, bewaart `episodes.published_at` een sorteerbaar moment; een bestaande database vult die kolom bij het starten alsnog.
- Binnen een project gaat een duurzaam transcript altijd vóór entiteitsdetectie. Zolang een project nog een transcript schuldig is — ook wanneer dat alleen op een herhaalpoging wacht — begint zijn entiteitsdetectie niet, zodat Ollama de gedeelde GPU niet bezet houdt op het moment dat Whisper weer aan de beurt is.
- Een project dat op een herhaalpoging wacht wordt overgeslagen, zodat de wachtrij doorwerkt in plaats van stil te liggen.
- Een nieuw project komt vooraan te staan: je hebt het net toegevoegd, dus daar wacht je op. Verschuif het daarna als dat niet klopt.
- Pauzeren van een project is iets anders dan volgorde: pauzeren stopt alleen het ontdekken van nieuwe afleveringen.

## Gebruik

1. Klik op **Nieuw project** en plak een kanaal, kanaaltab, afspeellijst, video, podcast-RSS-URL of het vergaderarchief van een gemeente. Geef het project eventueel meteen een eigen naam en omschrijving.
2. Kies wat je wilt volgen: het hele YouTube-kanaal, alleen gewone video’s, alleen afgeronde live-uitzendingen, alleen Shorts, één afspeellijst, een podcast-RSS-feed of raadsvergaderingen. Met **Automatisch uit de URL** herkent een kanaal-URL met `/videos`, `/streams` of `/shorts` de juiste tab zelf, en wordt een vergaderarchief herkend aan het platform.
3. Kies bij een kanaal, kanaaltab, afspeellijst of feed hoeveel recente afleveringen direct moeten worden geïmporteerd. **Alle beschikbare afleveringen** importeert de hele publiek zichtbare lijst. Live-uitzendingen die nog bezig of ingepland zijn worden altijd overgeslagen.
4. Bronnen worden elk uur gecontroleerd. Via **Instellingen** van een project kun je de bron direct controleren of pauzeren. Pauzeren stopt nieuwe ontdekkingen; de bestaande wachtrij loopt door.
5. Open een aflevering. Bovenaan staan de genoemde entiteiten, daaronder het **volledige transcript**. Klik een naam om de vindplaatsen te markeren of een tijdcode om audio af te spelen. Transcripties zijn doorzoekbaar en te downloaden als tekst.
6. Via **Entiteiten** vind je namen terug binnen het project, of over alle projecten heen wanneer je in **Alle projecten** werkt. Klik een naam voor de bijbehorende alinea’s. Onder de namen staan de telefoonnummers uit dezelfde transcripties.

Een groot project levert tienduizenden namen, dus `/api/entities` stuurt met `limit` en `offset` alleen het gevraagde stuk en daarbij het totaal. De pagina toont de eerste 150 en laadt de rest met **Toon meer namen**; zoeken en filteren beginnen weer bovenaan. Het antwoord telt per naam de afleveringen in plaats van ze op te sommen — het dashboard laat er toch alleen het aantal van zien. Voor het project met 22.360 namen scheelde dat samen 5,0 MB aan eerste lading tegenover 14 kB.

## Raadsvergaderingen

Gemeenten publiceren hun raadsvergaderingen ieder via een ander platform, dus `app/meetings.py` houdt per platform een adapter met dezelfde drie taken: een geplakte URL herkennen, het archief uitlezen en bij één vergadering de opname opzoeken. Een nieuw platform toevoegen betekent een adapter toevoegen; de rest van de wachtrij verandert niet.

### Company Webcast

Het platform achter onder meer `channel.royalcast.com` en `channel.companywebcast.com`. Plak de kanaalpagina van één gemeente, bijvoorbeeld `https://channel.royalcast.com/gemeenteopsterland/#!/archived`.

- Het archief komt uit `portal/api/1.0/<gemeente>/portalwebcasts/` met `status=indexed`; lopende en geplande vergaderingen blijven daarmee buiten de wachtrij.
- Er wordt steeds een even groot stuk van de catalogus opgehaald (minimaal 100 vergaderingen), net als bij YouTube. Een wisselende omvang zou al geziene vergaderingen bij een volgende controle opnieuw in de wachtrij zetten. **Alle beschikbare afleveringen** loopt het hele archief af; bij Opsterland gaat dat terug tot 2012.
- De titel van de vergadering komt uit het archief, het vergadertype (`Raadsvergadering`, `Oriënterende Raadsbijeenkomst`, …) staat als auteur bij de aflevering, en de datum komt uit de geplande starttijd.
- De downloadlink wordt per poging opnieuw opgevraagd: het platform ondertekent hem met een korte geldigheidsduur. Audioscraper neemt de `audio/mpeg`-bijlage en valt terug op `video/mp4` als die er niet is. De duur wordt afgeleid uit de werkelijke begin- en eindtijd.

### Notubiz

Het platform achter de raadsinformatieportalen op `<gemeente>.raadsinformatie.nl`, bijvoorbeeld `https://hoogeveen.raadsinformatie.nl/`. Plak het portaal zelf of een willekeurige pagina erbinnen: een portaal is één gemeente en dus één project. In het portaal klik je in de agenda een datum in het verleden aan om de video-opname te vinden; Audioscraper loopt diezelfde agenda af.

- Het organisatienummer van de gemeente staat in de paginabron van het portaal. Daarmee leest `api.notubiz.nl/events` de agenda per tijdvak uit. Alleen openbare vergaderingen die zijn uitgezonden komen in de wachtrij: een presidium of inloopspreekuur wordt niet opgenomen en wordt dus overgeslagen. Omdat de agenda een uitzending vooraf aankondigt, komt een vergadering pas twaalf uur na de geplande starttijd in beeld; anders zou een lopende avond mislukken omdat de opname er nog niet staat.
- Een raadsavond is bij Notubiz een omslag (`assembly`) om de zalen waarin die avond tegelijk wordt vergaderd. Elke zaal heeft een eigen opname en wordt daarom een eigen aflevering. De naam van de zaal staat pas op de vergaderpagina, dus tot het downloaden zijn ze genummerd (`Raadsavond parallel · deel 1`).
- De eerste import loopt jaar voor jaar terug tot de gekozen omvang is gehaald, maximaal twintig jaar; bij Hoogeveen gaan de opnames terug tot 2017. Een volgende controle kijkt alleen de laatste 400 dagen na. Dat venster valt binnen dat van de eerste import, dus al geziene vergaderingen kunnen niet alsnog als nieuw in de wachtrij komen.
- Het vergadertype (`Raadsavond`, `Auditcommissie`, …) komt uit het gremium en staat als auteur bij de aflevering; de datum komt uit de geplande starttijd.
- De opname staat als HLS-stream in de paginabron van de vergadering, opgebouwd zoals de speler van het portaal dat doet. Notubiz biedt daarnaast een mp4-download aan, maar die negeert `Range`-verzoeken en is regelmatig groter dan 2 GiB. FFmpeg leest daarom de stream en schrijft meteen de mono-mp3, zodat de video nooit op schijf staat. De invoer is daarbij vastgezet op HLS over HTTPS zonder omleidingen en moet op `*.notubiz.nl` staan, zodat een portaal FFmpeg nergens anders heen kan sturen.

### GemeenteOplossingen

Dit platform draait op het eigen domein van de gemeente, bijvoorbeeld `https://gemeenteraad.groningen.nl/`. Er is dus geen achtervoegsel om het aan te herkennen; Audioscraper vraagt het portaal zelf of het een open-data-API op `/api/v2/` heeft. Plak het portaal of een willekeurige pagina erbinnen.

- De agenda komt uit `/api/v2/meetings`, de open-data-API die de gemeente hiervoor publiceert. Die is vrij toegankelijk, gesorteerd op datum en filterbaar op periode, dus het ontdekken van vergaderingen gaat volledig langs de voordeur.
- De eerste import loopt terug tot de gekozen omvang is gehaald; een volgende controle kijkt alleen de laatste 400 dagen na. Net als bij Notubiz komt een vergadering pas twaalf uur na de starttijd in beeld.
- De titel is het eigen label van de vergadering en anders de naam van het orgaan (`gemeenteraad`, `Politiek Vragenuur`, `Politieke woensdag`), dat ook als auteur bij de aflevering staat.
- De opname staat als één mp3 per vergadering op `go-va-web.gemeenteoplossingen.nl`. Die mediaserver is vrij toegankelijk; alleen de bestandsnaam staat op de vergaderpagina.

**De vergaderpagina zit achter een poort.** Groningen zet [Anubis](https://anubis.techaro.lol/) voor alles behalve `/api/v2/` en `/robots.txt`. Die poort vraagt elke bezoeker eerst een stukje rekenwerk — een SHA-256 met een aantal nullen voorop — en laat je daarna door met een cookie. Een browser doet dat onzichtbaar; Audioscraper doet het expliciet in `anubis_nonce()`. Dat is bewust gekozen en het is goed om te weten wat het betekent:

- Het is geen inlog en geen geheim: iedereen krijgt dezelfde opgave en de inhoud erachter is openbare overheidsinformatie. Maar de poort staat er wel om geautomatiseerde clients tegen te houden, en die wordt hiermee gepasseerd.
- De `robots.txt` van het portaal staat `/Vergaderingen/` juist wél toe, met `Crawl-delay: 2`. Audioscraper houdt zich aan die wachttijd en blijft weg bij de paden die er wel in staan uitgesloten.
- Audioscraper noemt zichzelf gewoon `Audioscraper/1.0` en doet zich niet voor als browser, zodat de gemeente ons bewust kan toelaten of blokkeren.
- Er gaat zo min mogelijk langs de poort: het ontdekken gebruikt de open API, het mp3-bestand komt van de open mediaserver, en alleen het opzoeken van de bestandsnaam kost één pagina per vergadering — eenmalig, want de verkregen cookie wordt per portaal hergebruikt.

Niet iedere vergadering wordt uitgezonden — een agendacommissie meestal niet — en de agenda verraadt vooraf niet welke. Zo'n vergadering komt in de wachtrij en mislukt daar met `Deze vergadering biedt geen downloadbare opname aan.`

## Architectuur en opslag

- FastAPI + SQLite, één blijvende Docker-service met automatisch herstartbeleid.
- yt-dlp met Node voor YouTube; feedparser en gestreamde downloads voor podcasts; FFmpeg maakt compacte mono-MP3's.
- Whisper large-v3 op `192.168.1.28:8765`, bestaande Plaud/Storyteller-worker.
- Entiteitsdetectie met de al aanwezige lokale Ollama `qwen3.5:9b` via dezelfde worker en GPU-lock.
- Data in `data/audioscraper.sqlite3`. De tabel `sources` is tegelijk de projectenlijst: elke aflevering, transcriptie, entiteit en nummervermelding hangt via `episodes.source_id` aan precies één project. Een bestaande database wordt bij het starten bijgewerkt: losse video's zonder bron krijgen alsnog hun eigen project. Gedownloade media is uitsluitend tijdelijk en wordt na een geslaagde transcriptie automatisch verwijderd; transcript, tijdcodes, entiteiten en de oorspronkelijke bron-URL blijven bewaard.
- Transcript en elke afgeronde analysestap worden tussentijds bewaard. Na herstart hervat de wachtrij. Na drie fouten verschijnt een knop **Opnieuw proberen**. Als het transcript al bestaat wordt audio niet opnieuw getranscribeerd.
- Bewaar een back-up van de hele `data`-map. Gebruik voor een consistente live databaseback-up SQLite's backup-API; kopieer niet uitsluitend het databasebestand terwijl de service schrijft.

Alle transcriptie en entiteitsanalyse gebeurt op je eigen netwerk. Alleen de gekozen bronnen worden via internet opgehaald. Het worker-token staat uitsluitend in `.env` en gaat niet naar de browser. Traefik beschermt de volledige website, API, audio en exports met `authelia-vroeg@file`. In Authelia staan vóór de algemene regels twee domeinregels: `user:bas` en `user:roos` → `one_factor`, daarna iedereen → `deny`. Gebruikers komen uit lldap; `users_database.yml` is ongebruikt. De beheerpoort 8788 is uitsluitend op `127.0.0.1` gebonden en is niet via het LAN bereikbaar. Publieke DNS: Cloudflare CNAME `audioscraper.vroeg.app` → `basflix.nl`, proxied; TLS via de bestaande Cloudflare-certificaatresolver.

## Worker-uitbreiding

Broncode: `/mnt/user/appdata/plaud-obsidian/worker`.
Windows-installatie: `C:\PlaudWhisperWorker`, Scheduled Task **Plaud Whisper Worker**.

- `POST /v1/media/transcriptions`: alias van de bestaande directe audio-uploadroute. Automatische taaldetectie, tijdcodes, geen toevoeging van openbare stemmen aan Plaud-stemprofielen.
- `POST /v1/entities`: Bearer-authenticatie; JSON `{"text":"..."}` tot 20.000 tekens. Antwoord: `{"entities":[{"name":"...","type":"person"}],"model":"..."}`.
- Extractie gebruikt de gedeelde GPU-lock en ontlaadt Ollama na ieder deel. Alleen letterlijk aangetroffen namen worden geaccepteerd. Een tweede modelcontrole controleert de context en vereist een letterlijk bewijsfragment om gewone woorden uit te sluiten.
- De bestaande `/audio/transcriptions`, Plaud-wachtrij en speaker-routes blijven beschikbaar.
- Back-up van de oorspronkelijke worker-API op Windows: `C:\PlaudWhisperWorker\backups\before-audioscraper-20260917-145518`.
- `scripts/worker-preflight.ps1` en `scripts/worker-deploy.ps1` documenteren de uitgevoerde back-up/test/installatie. Voer een worker-herstart uit wanneer Plaud/Storyteller geen lopende verwerking hebben.

## Starten en bijwerken

```sh
cp .env.example .env
# Zet WORKER_TOKEN op het bestaande Plaud-worker-token.
chmod 600 .env
# Op deze host ontbreekt een recente buildx, dus gebruik de klassieke builder:
DOCKER_BUILDKIT=0 docker build -t audioscraper:local .
docker compose up -d --no-build
docker compose logs -f
```

Wijzigingen aan app-code vereisen een nieuwe build en `docker compose up -d --no-build`. De gegevens blijven staan. Bij een lopende transcriptie kun je beter wachten op voltooiing voordat je de container vervangt; bij afbreken wordt de nog niet opgeslagen transcriptie opnieuw aangevraagd.

Instellingen: `WORKER_URL`, `WORKER_TOKEN`, `POLL_SECONDS` (3600), `DATA_DIR` (`/data`). Eén Uvicorn-proces gebruiken: de service start zelf een verwerkingsthread, een broncontrolethread en een onafhankelijke thread voor bedrijfskoppelingen.

## Automatische bedrijfskoppelingen

Op de bibliotheekpagina staan twee voortgangsbalken: transcripties en entiteitsdetectie. De tweede toont het aantal verwerkte afleveringen, hoeveel er wachten en de exacte voortgang in opgeslagen tekstdelen van de huidige aflevering. Bij herstart wordt de voortgang uit de opgeslagen analyse hervat. Beide balken vernieuwen om de zes seconden.

De knop **Pauzeer alles / Hervat alles** staat bovenaan iedere pagina. Hij stopt alle achtergrondverwerking van Audioscraper: nieuwe downloads, transcripties, entiteitsdetectie, broncontroles en automatische bedrijfszoekopdrachten. De stand blijft na een herstart behouden. Een lopende Whisper-aanvraag of download wordt veilig afgerond; entiteitsdetectie stopt na het huidige tekstdeel. Tijdens het afronden toont de site dit expliciet. Hervatten start de wachtrijen weer zonder opgeslagen transcripties of analysevoortgang te verliezen. Individueel gepauzeerde bronnen blijven individueel gepauzeerd.

Nieuwe en bestaande onbekende telefoonnummers worden automatisch opgezocht door `app/phone_lookup.py`, zonder GPU of aanvullende API-sleutel. De persistente tabel `phone_lookups` bewaart status, pogingen, controledatum, herhaaltijd en bewijs. Eén nummer wordt tegelijk onderzocht, met 60 seconden pauze tussen nummers. Vaste/bedrijfsnummers gaan bij de eerste poging voor mobiele nummers.

De zoekstap gebruikt DuckDuckGo en Bing, meerdere schrijfwijzen van het nummer en waar beschikbaar bedrijfsnamen uit de transcriptcontext/entiteiten. Zoekresultaten zijn uitsluitend aanknopingspunten: de gekoppelde website wordt apart opgehaald. Een koppeling vereist hetzelfde exacte telefoonnummer in de gestructureerde bedrijfsgegevens, of een officiële contactpagina met bedrijfsnaam, telefoonlink en adres. Algemene telefoongidsen krijgen geen koppeling op basis van hun eigen sitenaam. Afwijkende cijfers worden niet stilzwijgend gecorrigeerd en persoonsgegevens met alleen het schema-type `Person` worden niet als bedrijf gekoppeld. Pagina's zijn maximaal 2 MiB en dezelfde controles op privé-adressen en redirects gelden als voor RSS-bronnen.

Het nummeroverzicht toont de zoekstatus en het aantal gekoppelde nummers. Geen match: na zeven dagen opnieuw zoeken, en eerder zodra nieuwe entiteitsanalyse meer context oplevert. Tijdelijke fouten: opnieuw na 15 minuten, daarna oplopend tot maximaal één dag. Zoekmachines kunnen verzoeken tijdelijk beperken; een openbare bedrijfskoppeling is dus niet voor elk nummer gegarandeerd. Ontbrekende adressen worden niet verzonnen. KvK-nummers worden alleen overgenomen uit afzonderlijk gecontroleerde bronnen.

Ongekoppelde mobiele nummers krijgen na de openbare zoekactie een tweede, lokale
contextanalyse. Audioscraper stuurt een gemaskeerd venster van acht
transcriptsegmenten vóór en twaalf segmenten na de nummervermelding naar
`POST /v1/phone-context` op dezelfde lokale worker. Het model onderscheidt de
gebelde partij, een ingestuurd doelwit, een privécontact, het eigen nummer van
een beller en een ongeldig of verkeerd herkend nummer. Een naam en het
bewijsfragment moeten letterlijk in het transcriptvenster staan; anders wordt
de uitkomst verworpen of `onbekend`. Deze analyse draait alleen nadat
transcripties en entiteitsdetectie klaar zijn en respecteert **Pauzeer alles**.

Contextresultaten zijn nadrukkelijk suggesties, geen bewezen eigenaarschap. Op
het nummeroverzicht kunnen ze met hun tijdcode worden gecontroleerd, bewerkt,
bevestigd of afgewezen. Alleen een officiële pagina waarop exact hetzelfde
nummer bij een organisatie staat geeft automatisch het label **Openbaar
bevestigd**. Handmatige beoordelingen blijven bij een heranalyse behouden.
Mobiele nummers die tweemaal geen openbare match opleveren worden pas na
negentig dagen opnieuw algemeen gezocht, tenzij nieuwe transcriptcontext een
gerichte zoekopdracht mogelijk maakt.

Handmatig gecontroleerde aanvullingen staan in `app/phone_directory.json` (plus de bestaande Farent-vermelding). Ze gelden voor alle huidige en toekomstige transcripties en overschrijven geen recentere verificatie. Automatisch gevonden koppelingen staan duurzaam in SQLite. IBANs, ook met spreekpauzes of verdeeld over segmenten, en cijfers ingebed in alfanumerieke codes worden vóór het zoeken uitgefilterd.

Gevonden telefoonnummers staan onder aan de pagina **Entiteiten**, in dezelfde omgeving als de namen waar ze uit komen: die van het actieve project, of alle nummers samen in **Alle projecten**. Staat er geen enkel nummer, dan blijft dat deel weg. Je kunt filteren op **Groningen, Drenthe en Friesland** samen of per provincie. De keuze staat in de URL (`#entities?region=north`, binnen een project `#project/<id>/entities?region=north`) en blijft bij verversen behouden; een filterkeuze tekent alleen de nummers opnieuw, niet de entiteiten erboven. Oude links naar `#phones` komen op dezelfde pagina uit. Een herkende bedrijfslocatie gaat voor het netnummer, zodat ook mobiele en 0800/088-bedrijfsnummers meetellen en bekende bedrijven buiten de regio worden uitgesloten. Zonder herkenbare bedrijfslocatie telt het netnummergebied; gebieden over provinciegrenzen worden expliciet als gedeeld aangeduid. Plaatsnamen komen uit [CBS Woonplaatsen 2026](https://www.cbs.nl/nl-nl/cijfers/detail/86312NED); dubbele plaatsnamen zonder provincie worden niet als zekere locatie beschouwd. De brongegevens staan in `app/static/phone-regions.json`. De browser filtert lokaal, zonder externe geocoder. Controle: `node --test tests/phone-regions.test.cjs` en `scripts/browser-phones.cjs`. De projectomgevingen worden gecontroleerd met `scripts/browser-projects.cjs` (`npm run test:browser:projects`); stel `BASE_URL`/`AUDIOSCRAPER_ORIGIN` in om een andere instantie te testen.

Als YouTube een login vereist, kun je zelf een Netscape-cookiebestand plaatsen als `data/youtube-cookies.txt`. Dit bestand wordt alleen door de downloader gebruikt. Het is niet nodig voor de eerste testvideo. yt-dlp is een externe integratie: bij wijzigingen aan YouTube kan een dependency-update nodig zijn. De geïnstalleerde versies staan vast in `requirements.lock.txt`; werk dit bestand bewust bij bij dependency-updates. De gebruikte JavaScript-ondersteuning volgt de [officiële yt-dlp-documentatie](https://github.com/yt-dlp/yt-dlp/wiki/EJS).

## Grenzen

- Entiteitsdetectie is automatisch: namen kunnen ontbreken, onjuist zijn gespeld door Whisper, of een verkeerde categorie hebben. Controleer altijd de geciteerde transcriptcontext/audio.
- Verschillende schrijfwijzen worden niet automatisch aan één persoon gekoppeld; gelijk gespelde namen binnen een categorie worden in het algemene overzicht gegroepeerd. Dat is geen garantie dat het dezelfde persoon betreft.
- Geen sprekeridentificatie; de tool toont **genoemde** personen.
- Raadsvergaderingen: alleen platforms waarvoor een adapter bestaat. Een vergadering zonder downloadbare opname wordt overgeslagen met een foutmelding; bij Company Webcast telt een vergadering die alleen als video wordt aangeboden mee voor de limiet van 2 GiB. Bij Notubiz geldt die limiet niet, omdat alleen het geluid van de stream wordt gelezen. Bij GemeenteOplossingen kan een portaal zijn poort strenger zetten; wordt de opgave te zwaar, dan stopt Audioscraper met een foutmelding in plaats van door te rekenen. Er is geen tijdcodelink terug naar de webcastspeler, dus tijdcodes werken alleen zolang de audio lokaal staat.
- RSS: alleen openbare afleveringen met audio-enclosures. Privé-netwerkadressen als bron en redirectdoel worden geweigerd. Maximaal 2 GiB audio per aflevering en 10 MiB per RSS-feed.
- YouTube: bij de normale eerste import worden maximaal 50 items uit de gekozen broncatalogus bekeken; **Alle beschikbare afleveringen** doorloopt de volledige publiek zichtbare kanaaltab of afspeellijst. RSS: de beschikbare afleveringen in de feed. De eerste import gebruikt een benadering van publicatiedatums uit YouTube; de definitieve metadata wordt bij downloaden opgehaald.
- Tijdens een download of mislukte transcriptie blijft tijdelijke audio staan zodat een herpoging geen nieuwe download vereist. Zodra Whisper een transcript heeft opgeleverd, wordt de lokale audio verwijderd. YouTube-fragmenten openen vanuit het transcript de oorspronkelijke video op de bijbehorende tijdcode.
- In **Entiteiten** kan het type **Locaties** worden gefilterd op Nederland, Noord-Nederland, Groningen, Friesland, Drenthe, Europa en de overige werelddelen. De indeling gebeurt offline met GeoNames en Nederlandstalige Unicode CLDR-landnamen; niet herkende of fout getranscribeerde plaatsnamen blijven zichtbaar onder **Niet ingedeeld**.
- Projecten scheiden wat je ziet en in welke volgorde er wordt gewerkt, maar er blijft één gedeelde wachtrij en één GPU: twee projecten worden nooit tegelijk verwerkt. Stemprofielen, openbaar bevestigde bedrijfskoppelingen en handmatige nummerbeoordelingen gelden eveneens voor alle projecten.
- Dezelfde bron kan maar in één project staan. Voeg je een URL toe die al bestaat, dan krijg je dat project terug in plaats van een tweede kopie.
- Entiteitsdetectie wacht buiten de worker wanneer Whisper de gedeelde GPU gebruikt. Daardoor toont het dashboard duidelijk dat de analyse op de GPU wacht en kan één lange transcriptie geen open entiteitsaanvraag uren laten hangen.
- Als Ollama een groot tekstdeel niet als geldige entiteiten-JSON kan teruggeven, deelt Audioscraper alleen dat tekstdeel automatisch op in kleinere stukken en bewaart het samengevoegde resultaat als hetzelfde controlepunt. Tijdelijke netwerk- en workerfouten krijgen een oplopende herhaaltijd (maximaal 30 minuten) en worden niet meer na drie pogingen als definitief mislukt gemarkeerd.

## Tests

```sh
docker run --rm -v "$PWD:/app" audioscraper:local python -m pytest -q tests
npm ci
docker run --rm --network host -v "$PWD:/app" -w /app \
  mcr.microsoft.com/playwright:v1.61.1-noble node scripts/browser-check.cjs
docker run --rm --network host -v "$PWD:/app" -w /app \
  mcr.microsoft.com/playwright:v1.61.1-noble node scripts/browser-projects.cjs
```

`scripts/browser-projects.cjs` verwacht minstens twee projecten. Wil je niet tegen de draaiende dienst testen, start dan een tweede container op een kopie van `data/` (gebruik SQLite's backup-API) en zet `BASE_URL`/`AUDIOSCRAPER_ORIGIN` op die poort.

Screenshots en testresultaten staan in `test-results/` (niet in versiebeheer). De worker heeft eigen tests in `../plaud-obsidian/worker/tests`.

Het implementatieplan staat in [PLAN.md](PLAN.md).
