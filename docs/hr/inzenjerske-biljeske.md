# Inženjerske bilješke

> 🇬🇧 [English version](../en/engineering-notes.md)

Sustav koji vodi pravu kuću kvari se na načine koje nijedan test nije
predvidio. Ovdje su incidenti i odluke koji su oblikovali kod, zapisani dok su
bili svježi: što se vidjelo, što je stvarno bilo krivo, što se promijenilo i što
je naučeno. Datumi su kad se dogodilo.

---

## Bot koji je dva dana bio mrtav dok je systemd javljao da radi

**2026-08-18.** Telegram bot je prestao odgovarati, a `systemctl` je dva dana
javljao `active (running)`.

**Što je bilo krivo.** Lanac se odvio ovako:
1. Pri pokretanju je `get_me()` istekao, jer mreža unatoč
   `After=network-online.target` još nije stvarno radila.
2. Put čišćenja pozvao je `updater.stop()` na updateru koji se nikad nije
   pokrenuo. To je bacilo grešku i **u logu zamijenilo pravu grešku**.
3. `main()` je pozvao `sys.exit(1)`, a proces nije izašao. Handler Google Cloud
   Logginga ima ne-daemon dretvu koja je zapela pokušavajući poslati 252 zapisa
   iz reda.
4. `Restart=always` se zato nikad nije okinuo.

Potpis je, kad ga znaš, nepogrešiv: živ PID, osam dretvi i nula socketa.

**Što se promijenilo.**
- `utils/process_guard.py` dodaje `hard_exit`: ograničeno pražnjenje logova, pa
  `os._exit`, tako da nijedna dretva ne može spriječiti izlaz.
- Koraci gašenja provjeravaju stanje, hvataju iznimke i imaju vremensko
  ograničenje, pa čišćenje nikad ne može sakriti grešku koja ga je izazvala.
- Petlja u mirovanju postala je nadzornik: baca grešku kad polling stane ili kad
  tri uzastopna `getMe` pinga ne uspiju.
- Svaki servis hrani systemd watchdog.

Dokazano na Piju: s lažnim tokenom proces sada izlazi s kodom 1 za 8 sekundi i
prava greška ostaje sačuvana.

**Pouka.** Živ PID nije dokaz da servis radi. Neka proces dokaže da poslužuje, a
smrt neka bude nezaustavljiva.

---

## Tri dana „U redu" uređaju kojeg nije bilo

**2026-09-06.** Svjetlo u kuhinji prestalo je reagirati na glas, a Jarvis je
potvrđivao svaku naredbu.

**Što je bilo krivo.** Više stvari zajedno:
- **Pločica je nestala s MQTT-a.** ESP32 s relejima restartao se tri dana ranije
  i nikad se nije ponovno spojio na MQTT.
- **Panel nije ništa pokazivao.** Home Assistant ga je i dalje dosezao nativnim
  ESPHome API-jem, pa je zidni panel radio i ništa nije izgledalo krivo. Jarvis s
  relejima razgovara samo preko MQTT-a.
- **Broker je lagao.** `esp32-io/status = online` je *zadržana* poruka. Uredno
  odspajanje ne okida last will, pa je broker zauvijek javljao „online".
- **Potvrda se izgubila u sažetku.** Kod za potvrdu je razlikovao stvarnu jeku
  uređaja od zadržane poruke koja se samo poklapala. Sažetak je zatim oboje
  zbrojio i nazvao „potvrđeno", a kratki glasovni način svaki je ishod spljoštio
  u „U redu.".

**Što se promijenilo.** Razlika između *potvrđeno*, *već je u tom stanju* i *nema
odgovora* sada stiže do korisnikovih ušiju. „U redu." i tišina rezervirani su za
potvrđenu promjenu, a greške se uvijek izgovaraju.

**Pouka.** Dva prijenosna puta do istog uređaja otkazuju neovisno. Zadržana MQTT
poruka je sjećanje na prošlost, ne tvrdnja o sadašnjosti. Živ uređaj je onaj
koji telemetriju šalje sada.

---

## Sat koji je stao pri pokretanju

**2026-08-20.** U 20:29 zatraženo je nešto „za dvije minute". Agent za raspored
proizveo je 20:25, što je nova provjera okidača ispravno odbila kao prošlost.

**Što je bilo krivo.** Dvanaest agenata je trenutni datum i vrijeme ubacivalo u
upute **jednom, kad je agent nastao**. U pokretanju iz naredbenog retka to se ne
vidi, a u botu koji radi danima je jako pogrešno.

**Što se promijenilo.** ADK prihvaća funkciju kao uputu, pa tvornica svaku uputu
s oznakama datuma omota u pružatelja koji sat iscrta pri svakom pozivu. Statični
dio prompta ostaje iznad granice predmemorije, pa iscrtavanje vremena ne ruši
predmemoriju.

---

## Alat koji u produkciji nikad nije radio

**2026-08-23.** Prvo pitanje kroz Home Assistant Assist o najvišoj dnevnoj
temperaturi nije uspjelo.

**Što je bilo krivo.** `home_climate_history` je zvao `asyncio.run()`, koji baca
grešku unutar event loopa koji već radi. Web API, Telegram handler i glasovna
petlja svi zovu alate iz asinkronog koda. Alat je testiran samo ručno iz
`python -c`, jedinog konteksta koji ništa u produkciji ne koristi.

**Što se promijenilo.** Most pokreće korutinu u vlastitoj dretvi kad loop već
radi, a test zove alat iznutra `asyncio.run()`.

**Pouka.** Alat provjeri u okruženju koje ga poziva, ne izolirano.

---

## Istraživačko pitanje od 6,28 $

**2026-09-03.** Jedno duboko pitanje o cijenama dizalica topline na Claude
Opusu: 13 minuta, 8 poziva researchera i 1,18 milijuna ulaznih tokena, od kojih
je samo 43.000 došlo iz predmemorije.

**Što je bilo krivo.** Nije kriv model, nego ReAct petlja. Svaki poziv alata
ponovno je slao cijeli razgovor, uključujući puni tekst svake dotad skrejpane
stranice. Točka predmemorije označavala je samo sistemski prompt.

**Što se promijenilo.**
- Druga točka predmemorije na kraju prefiksa razgovora.
- Skrejpane stranice ograničene na 12.000 znakova.
- Podjela posla: researcher pretražuje na Sonnetu, a `synthesizer` bez alata
  piše izvještaj na jačem modelu.

**Pouka.** Prije nego agentu s alatima digneš model, prebroji koliko puta njegova
petlja ponovno šalje kontekst.

---

## Researcher nije vidio cijenu

**2026-09-13.** Sirova pretraga iza researchera od 3. rujna vraćala je 403, a
stranice koje je ipak našao stizale su bez jedinog broja po koji je poslan.

**Što je bilo krivo.** Dvije stvari. Google Custom Search, planirani izvor
sirovih rezultata, odbijao je projekt porukom „does not have the access". Pokazalo
se da je taj JSON API zatvoren za nove korisnike i da se gasi 1. 1. 2027. Stranice
je pak čitao izravni dohvat parsiran BeautifulSoupom. Na tri stranice trgovina
vratio je 138, 54 i 1.095 riječi, a cijenu ni na jednoj, jer se Jina kao rezerva
palila tek ispod 50 riječi. Uz to je provjera HEAD/GET prije skupnog dohvata
trgovine koje blokiraju botove označavala nevažećima, pa ih nijedan čitač nije
ni vidio.

**Što se promijenilo.**
- Pretraga pita Jinu, zatim Firecrawl, zatim DuckDuckGo. Custom Search se
  uključuje samo izričito. Na Piju je Firecrawl odgovarao za 0,9–1,4 s, a Jina
  za 1,7–4,4 s, s istim rezultatima. Firecrawl ipak troši kredite, pa je drugi.
- Stranice čita Firecrawl, zatim Jina Reader, zatim izravni dohvat. Portali s
  vijestima zadržavaju svoje selektore i idu izravno prvi. Potrošeni krediti
  (HTTP 402) ili ograničenje brzine (429) znače samo prelazak na sljedećeg.
- Oba redoslijeda su u `.env` (`SEARCH_PROVIDERS`, `SCRAPE_PROVIDERS`). Firecrawl
  se zove preko REST API-ja, jer se SDK razlikovao između laptopa i Pija.

**Pouka.** Broj riječi nije sadržaj. Čitač izmjeri na stranicama koje stvarno
trebaš, za podatak koji stvarno trebaš.

---

## Wake word koji je ogluhnuo

**2026-07-12.** Šest pokušaja zaredom bez reakcije.

**Što je bilo krivo.** Tri obrane od lažnih buđenja naslagane jedna na drugu:
prag 0,40, dva uzastopna okvira i Silero VAD na 0,5. Presudio je VAD. Tiho
izgovoren „hej Jarvis" pri odabranom pojačanju doseže 3.000–7.000 od 32.768,
Silero je to proglasio negovorom i svaki rezultat spustio na nulu. Heartbeat log
je to pokazivao: vrhovi mikrofona na razini govora uz zadnji rezultat točno
0,000.

**Što se promijenilo.** VAD je isključen, jedan okvir, prag 0,32. Lažna buđenja
rješavaju se nakon prijepisa, gdje se tekst bez slova odbacuje.

**Pouka.** Detektor podesi na osobi koja će ga koristiti. Prije nego dodaš još
jednu fazu, pročitaj što svaka postojeća stvarno propušta dalje.

---

## Lažna buđenja su račun

**2026-08-30.** Oko 30 $ API kredita nestalo je u dva dana.

**Što je bilo krivo.** Lanac se odvio ovako:
1. Zvuk iz prostorije i TV prelazili su prag wake worda.
2. Prepoznavanje je vraćalo besmislice („Pa da se peva.").
3. Filtar šuma uhvatio je samo dio.
4. Ostatak je išao u pune upite agentima na Sonnetu 5, po ~56.000 tokena.

Način za nastavak držao je mikrofon otvorenim, pa je jedno lažno buđenje postalo
više upita. U jednom danu: 61 prepoznavanje, 123 upita i ~6,9 milijuna tokena.

**Što se promijenilo.**
- Mikrofon je postao opt-in, kroz sklopku u Home Assistantu s gašenjem nakon 30
  minuta.
- Jeftina pitanja po zadanom idu agentu `voice_qa` bez alata umjesto
  orkestratoru.
- Dnevni limit potrošnje sprječava ponavljanje.

---

## Home Assistant je rezao rečenice na pola

**2026-08-23.** Izgovoreni zahtjevi stizali su kao svoje prve dvije sekunde.

**Što je bilo krivo.** Assist završava upit nakon 0,7 s tišine i ograničava ga na
15 s. Nijedna vrijednost nije izložena za mobilnu aplikaciju: ni u sučelju, ni u
zapisu pipelinea, ni u WebSocket naredbi `assist_pipeline/run`. Držanje gumba
mikrofona ništa ne mijenja.

**Što se promijenilo.** Vlastita integracija pri učitavanju pomiče zadane
vrijednosti dataclassa na 3 s i 30 s. Traži ih po imenu, provjerava je li
promjena primljena, vraća ih pri uklanjanju, a ako se Home Assistant iznutra
promijeni, samo upozori. Provjereno: pauze od 2,5 s prolaze, a pauze od 3,5 s i
dalje završavaju upit.

---

## Odgovori koji su umirali s ekranom mobitela

**2026-09-03.** Izgovoreno istraživačko pitanje od 29 sekundi je prepisano, a
orkestrator ga je dovršio tri i pol minute kasnije. Govor nikad nije zatražen.

**Što je bilo krivo.** Cjevovod iza Assist prozora više nije postojao, jer prozor
umre kad se ekran ugasi ili aplikacija ode u pozadinu. Dokazano da nije kriv Home
Assistant: upit od 53 sekunde iz vlastitog WebSocket klijenta vratio se
kompletan. Nikakva promjena timeouta ne može popraviti slušatelja kojeg više
nema.

**Što se promijenilo.** Nakon 25 sekundi izvršavanje je zaštićeno, upit kaže da
se posao nastavlja, a gotov odgovor stiže kao obavijest na mobitel.

---

## HTTP 200 nije značio ništa: otvaranje aplikacije na TV-u

**2026-08-22.** „Otvori A1 Xplore TV" javljao je uspjeh i nije radio ništa.

**Što je bilo krivo.** Svaki put pokretanja na TCL Google TV-u vraćao je HTTP
200. Praćenje aplikacije u prvom planu nakon svakog pokušaja pokazalo je:
- ništa nisu napravili goli nazivi paketa, `market://`, `intent://` ni
  pogađane sheme;
- radili su samo pravi deep linkovi (YouTube, Netflix).

Čitanje manifesta A1 Xplore APK-a to je razriješilo. Aplikacija deklarira samo
`MAIN`/`LAUNCHER`: nema akcije `VIEW` ni sheme, pa je nijedan URI nikad ne može
otvoriti. TV nema ADB preko mreže.

**Što se promijenilo.**
- Android TV aplikacija od 12,6 KB (`deploy/tv_app_launcher/`) registrira
  `jarvis://open?pkg=<paket>` i pokreće bilo koju instaliranu aplikaciju.
  Izgrađena je običnim `javac`, D8, `aapt` i `apksigner` na Piju.
- `tv_open_app` sada pokretanje provjerava čekanjem stvarne promjene aplikacije u
  prvom planu, jer atribut `app_id` u Home Assistantu zna zastarjeti.
- Za prebacivanje kanala trebalo je još jedno otkriće: A1 Xplore ignorira
  utipkane znamenke dok ne radi ~25 sekundi.

**Pouka.** Kad uređaj na sve odgovara „OK", mjeri učinak, ne potvrdu. Kad se čini
da niz tipki ne stiže, najprije posumnjaj na vrijeme, a tek onda na tipke.

---

## Orkestrator je radnikovu ogradu pretvorio u tvrdnju

**2026-08-22.** `smart_home` je rekao „Poslao sam broj 2 na daljinski; **ne mogu
potvrditi** da se aplikacija prebacila." Korisnik je čuo „Prebacio sam na HRT 2."

**Što se promijenilo.** Orkestrator je dobio izričito pravilo, s ovim incidentom
kao primjerom:
- „poslao" nikad ne postaje „prebacio";
- „ne mogu potvrditi" nikad ne postaje „gotovo";
- *nepoznat* ishod nije ni uspjeh ni neuspjeh i nikad se ne ponavlja „za svaki
  slučaj", jer tako jedan mail postaje dva.

---

## Petlja alata koja je pobjegla i upit koji nije završio

**2026-07-02.** Na Sonnetu 5 agent `scribe` je isti neuspjeli poziv ponavljao
svakih ~55 sekundi više od 15 minuta, a glasovna petlja je zauvijek visjela.

**Što je bilo krivo.** JSON poziva alata odrezan je na izlaznom limitu od 4.096
tokena, jer je prilagodljivo razmišljanje potrošilo dio limita. Glasovni timeout
postojao je samo u dokumentaciji.

**Što se promijenilo.**
- Zaštita od petlje u tvornici: tri jednake greške prekidaju se, šest ruši
  izvršavanje.
- Stvarni glasovni timeout koji otkazuje izvršavanje i to kaže.
- Donja granica izlaza od 8.192 tokena za Claude agente.

---

## Filozofski način procurio je u sve kanale

**2026-09-02.** Nakon jednog izgovorenog pitanja o Sokratu cijeli je proces pola
sata odgovarao kao Sokrat, na svakom kanalu.

**Što je bilo krivo.** Kanal Home Assistanta bio je na popisu govornih kanala, ali
ne i na popisu za usmjeravanje. Posljedice redom:
1. Njegovi zahtjevi preskakali su glasovni usmjerivač.
2. Stizali su do stare grane koja je na filozofsku ključnu riječ postavljala
   način rada za cijeli proces.
3. Taj način je ostajao.

**Što se promijenilo.** Assist se usmjerava kao svaki drugi glasovni kanal. Brzi
odgovori pali su s desetaka sekundi na 0,3 s za vrijeme i 0,7 s za prognozu.

---

## Potvrda koja je stigla prije pitanja

**2026-09-04.** Brisanje termina u kalendaru stajalo je neizvršeno kroz tri upita,
a model je zadržani poziv u jednom upitu ponovio pet puta.

**Što je bilo krivo.** Vrata su o zadržavanju odlučivala prije nego što su
primijenila „da" koje je već stiglo, pa je pristanak zapisan i bačen. Ništa
modelu nije govorilo da je već pitao.

**Što se promijenilo.**
- „Da" se čuva za točno one radnje koje su korisniku pokazane.
- Ponovljena zadržavanja rastu od pitanja do tvrdog zaustavljanja koje završava
  upit.
- Poruka o zadržavanju jasno kaže da je to sigurnosni korak, a ne kvar. Model je
  to korisniku jednom opisao kao „sustav traži potvrdu u krug".

---

## Uvjerljivost dolazi iz fizike, ne iz tehničkog lista

**2026-08-20.** Prva inačica alata za senzore očitanja čestica iznad 1.000 µg/m³
proglašavala je greškom senzora i to pisala na dashboardu. Korisnik je upozorio
da je dim cigarete u sobi vrijednost digao preko 100.000.

**Što se promijenilo.** Provjera raspona za čestice je uklonjena: 1.000 µg/m³ je
granica specificirane *točnosti* SPS30, ne stvarnosti.

Provjere temperature, vlage i tlaka su ostale, jer su uhvatile stvarni kvar. Dva
BME280 na najdužim kabelima povremeno su vraćala vrijednosti registara pri
uključenju, uvijek iste dvije po senzoru (188,5 °C i −57,6 °C). Upiti za povijest
koriste satnu statistiku, pa jedan loš uzorak košta sat, a ne dan.
