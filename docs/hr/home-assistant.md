# Integracija s Home Assistantom

> 🇬🇧 [English version](../en/home-assistant.md)

Home Assistant radi na vlastitom Raspberry Pi 5 (Home Assistant OS) i drži
uređaje. Jarvis je s njim povezan u oba smjera:
- preko Home Assistanta čita i upravlja kućom;
- Home Assistant koristi Jarvisa kao glasovnog asistenta i prikazuje njegovo
  stanje na dashboardu.

---

## Što gdje radi

| Na Home Assistant Piju | Svrha |
|------------------------|-------|
| Mosquitto dodatak | MQTT broker za ESP32 releje i Jarvisove entitete |
| ESPHome | dva ESP32 čvora (nativni API) |
| Android TV Remote + Google Cast | TCL televizor |
| Tailscale dodatak (`serve`) | pravi HTTPS za mobitel izvan kuće |
| Vlastita integracija `jarvis` | Jarvis kao Assist agent za razgovor |
| Wyoming integracija | Jarvisovo prepoznavanje i sinteza govora |
| Dashboardi | generirani iz ovog repozitorija |

---

## Jarvis kao glas Home Assistanta

Detaljno u [glasovnom putu](glasovni-put.md#kroz-home-assistant-assist). Ukratko,
`deploy/home_assistant/custom_components/jarvis/` donosi:

- `conversation.py`: `ConversationEntity` koji prosljeđuje na Jarvisov
  `/api/chat`. Id razgovora iz Home Assistanta preslikava na Jarvisovu sesiju, a
  prije izgovaranja miče markdown i emoji.
- `config_flow.py`: postavljanje provjerava adresu i token na `/api/status` prije
  spremanja; opcije izlažu timeoute.
- `assist_patience.py`: Assistovu pauzu prije kraja izgovora diže na 3 s, a
  ograničenje upita na 30 s, sigurno i povratno.
- `history.py`, `sensor.py`: entitet `sensor.jarvis_razgovor`. Stanje mu je
  zadnje pitanje, a atributi zadnjih deset izmjena; čuva se kroz restarte.

`adk-wyoming` dodaje `stt.jarvis_stt` i `tts.jarvis_tts`. Assist pipeline
*Jarvis* spaja sve troje.

Koraci postavljanja su u
[`deploy/home_assistant/JARVIS_ASSIST.md`](../../deploy/home_assistant/JARVIS_ASSIST.md).

---

## Jarvis čita i upravlja kućom

### Svjetla, utičnice, dimer, scene: MQTT

`tools/adk_tools/mqtt_adk_tools.py` izravno preko MQTT-a upravlja ESP32
relejima: 15 svjetala, 14 utičnica, dimer i imenovane scene.
- Svaka naredba do tri sekunde čeka da uređaj vrati novo stanje.
- Tri trošila traže izričitu potvrdu prije prebacivanja u rizičnom smjeru:
  hladnjak, bojler i pećnica.
- Brzi glasovni put obične naredbe paljenja i gašenja izvršava bez modela.

### Senzori i povijest: REST i WebSocket

`tools/adk_tools/ha_sensor_tools.py` čita temperaturu, vlagu, tlak, kvalitetu
zraka i snagu, a da nikad ne zove Home Assistant servis. Test provjerava da modul
nikad ne dira `/api/services`.
- Senzori se razvrstavaju po `device_class`, ne po imenu, pa novi čvor ne traži
  promjenu koda.
- Prostorija se dobiva uklanjanjem prefiksa uređaja i usporedbom hrvatskih
  nastavaka, pa „vanjska" nalazi „Vanjski tlak".

Povijest („koja je danas bila najviša temperatura") dolazi iz dugoročne
statistike Home Assistanta, koja je dostupna **samo preko WebSocket API-ja**.
Alat traži razdoblja od sat vremena. Na dnevnoj razini jedan pokvaren uzorak bi
izbrisao stvarni maksimum cijelog dana, a na satnoj stoji jedan sat. Očitanja
izvan fizički mogućeg raspona odbacuju se i javljaju korisniku.

### Televizor

TCL Google TV za isti uređaj izlaže dva entiteta u Home Assistantu:
- **Android TV Remote**: tipke, paljenje, pokretanje aplikacija;
- **Google Cast**: apsolutna glasnoća, casting.

Izmjereno, ne pretpostavljeno:

- Entitet daljinskog nema apsolutnu glasnoću (`volume_set` vraća HTTP 500). Zato
  „glasnoća na 40 %" ide korak po korak uz ponovno čitanje razine i javlja ako
  nije mogla pogoditi točno.
- YouTube pretraga izravno otvara prvi rezultat (`watch?v=`), koji aplikacija na
  TV-u sama pusti.
- Aplikacije bez deep linka ne mogu se otvoriti nikakvim URI-jem. Android TV
  aplikacija od 12,6 KB u `deploy/tv_app_launcher/` registrira
  `jarvis://open?pkg=<paket>` i otvara bilo koju instaliranu aplikaciju
  ([priča](inzenjerske-biljeske.md#http-200-nije-značio-ništa-otvaranje-aplikacije-na-tv-u)).
- Svako pokretanje provjerava se praćenjem stvarne promjene aplikacije u prvom
  planu.
- Kanali se uče kao ime → broj i tipkaju kao tipke daljinskog. Prije toga Jarvis
  čeka dok A1 Xplore ne počne primati znamenke.

### Popis za kupovinu

`tools/adk_tools/ha_shopping_tools.py` radi na Home Assistantovu `todo`
entitetu: dodaj, pokaži, označi kupljeno, ukloni i „kupio sam sve osim mlijeka".

### Obavijesti

Odgovori koji nadžive Assist prozor stižu kroz `notify.<mobitel>`.

---

## Home Assistant prati Jarvisa

`services/host_metrics.py` preko MQTT discoveryja objavljuje stanje Jarvis Pija
kao uređaj `jarvis_pi_host`: procesor, memoriju, disk, temperaturu, opterećenje,
vrijeme pokretanja i IP adresu. Registrira i MQTT last will. Ako web servis
padne ili mreža nestane, broker za nekoliko sekundi sve senzore označi
nedostupnima. Izmjereno: 3 s do nedostupnosti i 11 s do povratka.

`services/ha_mqtt_bridge.py` dodaje glasovne entitete:
- pokazatelj dostupnosti;
- glasovni način rada;
- zadnji prijepis i odgovor;
- `switch.jarvis_slusanje`, koji otvara i zatvara mikrofon na Piju.

Oba klijenta spajaju se asinkrono i ponovno objavljuju discovery pri svakom
spajanju. Jarvis Pi se pokrene brže od brokera na drugom Piju, a jednokratni
`connect()` jednom je ostavio senzore `unknown` satima.

---

## Dashboardi i zidni panel

Dashboardi se **generiraju, ne uređuju ručno**.
`deploy/home_assistant/tools/build_dashboard.py` redom:
1. preko WebSocket API-ja čita registre entiteta i područja;
2. spremi kopiju trenutne konfiguracije;
3. zapiše dashboard `jarvis-dom`.

Pogledi: pregled, sobe, klima, zrak, uređaji, TV, sustav, energija i prijepis
razgovora. Sobe se grade iz registra područja, pa se novi senzor pojavi sam.
Svjetla i utičnice poredani su po tome koliko su se stvarno palili u zadnjih 14
dana. Broji se samo `off → on`, jer restart čvora sve utičnice čini zauzetima.

`deploy/home_assistant/www/` sadrži dva modula za sučelje:

- `jarvis-sky.js` crta pozadinu dashboarda prema stvarnom nebu:
  - položaj i visina sunca iz `sun.sun`;
  - položaj i mijena mjeseca izračunati u pregledniku;
  - naoblaka i oborine iz entiteta prognoze;
  - CSS kiša ili snijeg, uz poštovanje postavke `prefers-reduced-motion`.
- `jarvis-energy-card.js` prikazuje snagu i potrošnju kuće na panelu.

Zidni panel je zaslon samog Jarvis Pija: Chromium u kiosk načinu na Waylandu
(`deploy/rpi/panel-kiosk.sh`). Ima vlastiti profil pa ostaje prijavljen, i
iznimku za siguran kontekst, pa gumb „Pitaj Jarvisa" može otvoriti mikrofon
preko lokalne mreže.

---

## Korisno znati o API-jima Home Assistanta

Skupljeno tijekom izrade; ništa od ovoga nije očito iz dokumentacije.

- Config entryji se mogu stvoriti preko REST-a. Assist pipelineovi, statistika
  recordera i dashboardi samo preko WebSocketa.
- `config_entries/delete` postoji samo na REST-u.
- `url_path` novog dashboarda mora sadržavati crticu.
- Id-jevi WebSocket poruka moraju strogo rasti unutar jedne veze.
- Datoteke dodane u `/config/www` vraćaju 404 dok se Home Assistant ne restarta.
- Integration senzor vrijednost vraća po entity_id-ju. Obrišeš li ga i ponovno
  napraviš pod istim imenom, tiho nastavlja stari zbroj.
- Supervisor odbija djelomično ažuriranje opcija dodatka; treba poslati cijeli
  objekt opcija.
