# Audioscraper — implementatieplan

1. **Bestaande infrastructuur aansluiten.** Gebruik de Plaud Whisper Worker op `192.168.1.28:8765` (broncode: `../plaud-obsidian/worker`, Windows-installatie: `C:\PlaudWhisperWorker`). Behoud de gedeelde GPU-lock voor Plaud en Storyteller. Voeg een aparte route voor media-transcripties en een beveiligde route voor lokale entiteitsdetectie met Ollama toe.
2. **Bronnen en downloads.** Een Docker-service op deze server beheert YouTube-kanalen, losse video's en podcast-RSS. yt-dlp downloadt YouTube-audio; RSS-enclosures worden gestreamd gedownload. Bronnen worden elk uur gecontroleerd. Bij toevoegen is de historische import begrensd en instelbaar.
3. **Betrouwbare verwerking.** SQLite bewaart bronnen, ontdekte afleveringen, wachtrij, voortgang, fouten, transcripties en entiteiten. Eén verwerking tegelijk, herstel na herstart, herpogingen met wachttijd en handmatig opnieuw proberen. Bewaar het transcript vóór entiteitsdetectie zodat een fout geen nieuwe transcriptie vereist.
4. **Entiteiten.** Detecteer personen, organisaties/instanties, locaties, producten, evenementen en overige benoemde zaken met het bestaande lokale taalmodel. Analyseer alle tekst in overlappende delen; accepteer uitsluitend namen die letterlijk in de transcriptie staan. Bewaar vermeldingen met tijdcodes. Toon dat automatische detectie onvolledig of onjuist kan zijn.
5. **Dashboard.** Nederlandstalige, responsive website: bibliotheek met zoeken en statusfilters; bronnen toevoegen/pauzeren/verversen; overzicht van entiteiten over afleveringen heen; detailpagina met entiteiten boven het volledige transcript, audio, tijdcodes en tekstexport.
6. **Testen en opleveren.** Unit- en integratietests voor deduplicatie, hervatten, bronvalidatie en entiteitsverwijzingen. Controleer bestaande worker-tests. Download en transcribeer `https://www.youtube.com/watch?v=92wmvrQU7a0`, inspecteer resultaten en test het dashboard in een browser. Start de Docker-service met een automatisch herstartbeleid.

## Keuzes

- Python/FastAPI + SQLite; lichte frontend zonder externe CDN-afhankelijkheden.
- Downloads en resultaten onder `./data`; geen cloud-API of extra abonnement.
- Dashboard op https://audioscraper.vroeg.app, uitsluitend Authelia-gebruiker bas (2FA). Geen onbeveiligde LAN-ingang; beheerpoort 8788 alleen op localhost. Worker-token uitsluitend aan de serverzijde.
- Herkenning betreft genoemde entiteiten, geen identificatie van stemmen.
- Nieuwe bestanden in dit project; gerichte uitbreidingen in de bestaande worker, met back-up vóór uitrol.

## Aanvulling tijdens uitvoering

- Op verzoek gepubliceerd op `audioscraper.vroeg.app`, met expliciete Authelia-toegang voor `user:bas` en een aansluitende deny-regel voor alle anderen.
- Een tweede entiteitscontrole met letterlijk bewijs uit de tekst filtert gewone woorden die het model aanvankelijk als naam aanmerkte.
- Kanaalimport verwerkt de afzonderlijke YouTube-tabbladen (video's, streams en Shorts) en sorteert de gezamenlijke lijst.
- Bronbeheer biedt daarnaast gerichte YouTube-bronnen: een heel kanaal, alleen video’s, alleen afgeronde live-uitzendingen, alleen Shorts of één afspeellijst. De optie voor een volledige eerste import haalt alle publiek zichtbare items op; lopende en ingeplande streams worden uitgesloten.
- Media is tijdelijk: na een duurzaam opgeslagen Whisper-transcript verwijdert de service het lokale audiobestand. YouTube-tekstfragmenten verwijzen naar de originele video met de juiste `t=`-tijdcode.
- Het dashboard is herbouwd rond projecten: iedere bron — inclusief een losse video — is één project met een eigen omgeving voor afleveringen, transcripties, entiteiten, stemmen en telefoonnummers. Een projectenoverzicht, een projectwisselaar in de zijbalk, per-project instellingen (naam, omschrijving, kleur, bronbeheer, verwijderen) en een gezamenlijk overzicht over alle projecten heen. Bestaande databases krijgen bij het starten alsnog een project voor losse video's.
- Raadsvergaderingen zijn een bronsoort geworden. `app/meetings.py` bevat per vergaderplatform een adapter; de eerste is Company Webcast (channel.royalcast.com), getest op het archief van gemeente Opsterland. Daarna is Notubiz erbij gekomen, het platform achter de portalen op `<gemeente>.raadsinformatie.nl`, getest op gemeente Hoogeveen. Notubiz levert alleen video, dus FFmpeg leest daar de HLS-stream en schrijft meteen de mp3; de video komt niet op schijf.
- De verwerkingswachtrij volgt een instelbare projectvolgorde. Een project wordt helemaal afgemaakt voordat het volgende begint; binnen een project blijft transcript vóór entiteitsdetectie gaan en wacht de analyse op een openstaande transcriptie van datzelfde project.
- GemeenteOplossingen is als derde vergaderplatform toegevoegd, getest op gemeente Groningen. De agenda komt uit de open-data-API van het portaal; de vergaderpagina staat achter Anubis, een proof-of-work-poort. Dat die poort wordt gepasseerd is een uitdrukkelijke keuze van de opdrachtgever, gemaakt nadat de afweging is voorgelegd. Uitgevoerd met een eerlijke User-Agent, met de `Crawl-delay` uit de eigen `robots.txt` van het portaal, en met zo min mogelijk verkeer langs de poort: alleen het opzoeken van de bestandsnaam, eenmalig per vergadering.
- Het dashboard is op verzoek uitgedund: geen tellerrij bovenaan de projectpagina, geen projectkeuzelijst (ieder project is een eigen link in de zijbalk), geen losse nummerpagina (de nummers staan onder de entiteiten van dezelfde omgeving), en de wachtrijpanelen verschijnen alleen wanneer er werk open staat. De navigatie-iconen zijn getekende SVG's in plaats van tekensymbolen.
- Wachtrij en bibliotheek sorteren binnen een project op publicatiedatum, nieuwste eerst. Authelia geeft toegang aan `bas` en `roos` met `one_factor`.
