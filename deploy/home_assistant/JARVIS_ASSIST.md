# Jarvis kao Home Assistant glasovni agent

## Zašto integracija, a ne postavka

Home Assistant ne može uputiti Assist na proizvoljni HTTP servis. Ugrađene su
samo OpenAI, Anthropic i Google integracije, a svaka od njih bila bi *drugi*
asistent — bez Jarvisovih alata (Gmail, kalendar, TV, MQTT, senzori i njihova
povijest, raspored). Ova integracija tu rupu zatvara: prosljeđuje ono što je
korisnik rekao na Jarvisov web API i vraća njegov odgovor Assistu.

Uz nju ide i Wyoming most, koji Assistu daje Jarvisovo uho i Jarvisov glas
umjesto tuđih. Zajedno pokrivaju cijeli put od izgovorene rečenice do
izgovorenog odgovora:

```
mikrofon → HA Assist → stt.jarvis_stt  ──→ Chirp na Jarvis Pi-u
                     → conversation.jarvis ──→ POST /api/chat, orkestrator i alati
                     → tts.jarvis_tts  ──→ OpenAI cedar na Jarvis Pi-u
                     → zvučnik
```

## Preduvjet: Jarvis mora biti dostupan s HA-a

Jarvisov web servis po zadanom sluša samo na `127.0.0.1`, pa ga Home Assistant s
druge adrese ne vidi. U `.env` na Jarvis Pi-u:

```
WEB_HOST=0.0.0.0
WEB_PORT=8000
```

i u `adk-web.service` ukloniti `--host 127.0.0.1 --port 8000` iz `ExecStart`
(bez tih zastavica `run_web.py` čita `WEB_HOST` i `WEB_PORT` iz okoline, pa se
ubuduće mijenja u `.env` bez roota).

`API_TOKEN` mora biti postavljen — profil `rpi-home` ga ionako zahtijeva. Bez
njega bi Jarvisov API bio otvoren svakome na kućnoj mreži.

Provjera s bilo kojeg računala na mreži:

```bash
curl -H "Authorization: Bearer <API_TOKEN>" http://jarvis.local:8000/api/status
```

## Dio 1 — conversation agent

1. Kopiraj mapu `custom_components/jarvis/` u `/config/custom_components/` na HA-u
   (preko *File editor*, *Studio Code Server* ili Sambe)
2. Restart Home Assistanta
3. **Settings → Devices & Services → Add Integration → Jarvis**
4. Upiši adresu (`http://jarvis.local:8000`) i `API_TOKEN`

Config flow prije spremanja pozove `/api/status`, pa se pogrešna adresa ili
token vide odmah, a ne tek kad nešto pitaš.

## Dio 2 — Wyoming most za glas

### Zašto ne HA-ovi vlastiti servisi

Whisper add-on piše slab hrvatski i otimao bi procesor HA Pi-u, a Google
Translate TTS govori engleski. Jarvis je oboje već riješio na svom Pi-u: Cloud
Chirp za prepoznavanje, odabran nakon što su i OpenAI i xAI hrvatski
prepoznavali kao drugi slavenski jezik, te OpenAI `cedar` za glas koji ukućani
već poznaju. Wyoming je HA-ov protokol za vanjske glasovne servise, pa se
poslužuje ono što postoji umjesto da se pokraj instaliraju slabije kopije.

Jedan server nudi oba servisa, pa HA treba jedan unos, a Pi jedan systemd unit.

### Instalacija na Jarvis Pi

```bash
sudo cp deploy/rpi/systemd/adk-wyoming.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now adk-wyoming
```

Postavke se čitaju iz `.env` (sve imaju razumne zadane vrijednosti):

| | |
|---|---|
| `WYOMING_URI` | adresa slušanja, zadano `tcp://0.0.0.0:10400` |
| `WYOMING_LANGUAGES` | zadano `hr,en` |
| `JARVIS_TTS_URL` | zadano `http://127.0.0.1:8000/api/tts` |
| `WYOMING_TTS_TIMEOUT_SECONDS` | zadano 60 |

### Dodavanje u Home Assistant

**Settings → Devices & Services → Add Integration → Wyoming Protocol** →
host `jarvis.local`, port `10400`.

Jedan unos donosi dva entiteta: `stt.jarvis_stt` i `tts.jarvis_tts`.

## Dio 3 — Assist pipeline

**Settings → Voice assistants** → pipeline *Jarvis*:

| | |
|---|---|
| Conversation agent | **Jarvis** |
| Speech-to-text | **jarvis-stt** |
| Text-to-speech | **jarvis-tts**, glas `cedar` |
| Jezik | hrvatski |

Označi ga kao zadani. Na mobitelu: Companion app → Settings → Companion app →
Assist → odaberi taj pipeline.

## Dio 4 — koliko Assist čeka da završiš rečenicu

Home Assistant prekida snimanje nakon **0,7 s** tišine i reže cijeli izgovor na
**15 s**. Prvo je premalo za normalan govor: izmjereno 2026-08-23, snimka
„Upali svjetlo u dnevnoj sobi" + pauza 1 s + „i pojačaj grijanje na dvadeset dva
stupnja" vratila je samo prvu polovicu. Pi čeka 1,2 s
(`WAKEWORD_SILENCE_SECONDS`) i taj problem nema.

Ta se postavka **ne može promijeniti** kroz sučelje ni kroz API: WebSocket
naredba `assist_pipeline/run` prima `noise_suppression_level`, `auto_gain_dbfs`,
`volume_multiplier` i `no_vad`, ali ne i `silence_seconds`; sam zapis pipelinea
takvo polje nema; entitet za osjetljivost VAD-a dobivaju samo *assist
sateliti*, a mobitel to nije.

Zato integracija pomiče same zadane vrijednosti (`assist_patience.py`), oprezno:
polje traži po imenu u potpisu, provjeri je li promjena stvarno primljena, pamti
staru vrijednost i vraća je kad se Jarvis ukloni. Ako se HA iznutra promijeni,
zapiše upozorenje i ne dira ništa — najgori ishod je stari kratki prekid, ne
pokvarena instalacija.

**Settings → Devices & Services → Jarvis → Configure:**

| | zadano | HA bez ovoga |
|---|---|---|
| Pauza prije kraja izgovora | 3 s | 0,7 s |
| Najdulje trajanje izgovora | 30 s | 15 s |
| Čekanje na Jarvisov odgovor | 300 s | — |

Cijena je poštena: odgovor kreće tri sekunde kasnije, jer se pauza ne može
razlikovati od kraja rečenice dok ne potraje dovoljno dugo. Tko voli brže,
spusti na 1,2 s kao na Pi-u.

Provjereno na živom sustavu nakon promjene: pauza od 2,5 s prolazi cijela, pauza
od 3,5 s i dalje reže (dakle detekcija radi, samo je strpljivija), a 30 s
neprekinutog govora stigne do kraja.

## Dio 5 — povijest razgovora i nastavak niti

Assist prozor pamti razgovor samo dok je otvoren. Zatvori se — ili ga zatvori
mobitel umjesto tebe — i nestane oboje: tekst odgovora, koji je znao biti predug
da se pročita u letu, i sama nit razgovora.

Nit nestaje jer svako otvaranje prozora dobiva **novi `conversation_id`**, pa je
integracija otvarala novu Jarvisovu sesiju. Sada, ako se vratiš unutar
**10 minuta**, nastavlja se prethodna sesija. Provjereno: „Zapamti broj
sedamnaest" u jednom prozoru, „Koji sam ti broj rekao?" u drugom — odgovorio je
17.

Tekst se čuva u entitetu **`sensor.jarvis_razgovor`**:

| | |
|---|---|
| stanje | zadnje postavljeno pitanje (pa HA-ova povijest pokazuje vremensku crtu) |
| `pitanje`, `odgovor`, `vrijeme` | zadnja izmjena |
| `povijest` | zadnjih 10 izmjena |

Čuva se i kad Jarvis javi grešku — poruka o isteku vremena tako ostane
pročitljiva umjesto da nestane s prozorom. Zapisuje se u HA-ovo spremište, pa
preživljava restart (provjereno). Drži se zadnjih 25 izmjena; starije ispadaju.

Na ploči **Jarvis → Razgovor** stoji prijepis i gumb *Pitaj Jarvisa* koji odmah
otvara glasovni prozor s tim pipelineom.

Trajanje nastavka niti mijenja se ondje gdje i ostala: **Settings → Devices &
Services → Jarvis → Configure**. Nula ga isključuje.

## Kako provjeriti da odgovara baš Jarvis

Pitaj nešto što HA-ov ugrađeni Assist ne može znati:

> „koja je danas bila najviša vanjska temperatura"

Ugrađeni Assist čita samo trenutna stanja entiteta; povijesnu statistiku ima
jedino Jarvis (`home_climate_history`).

## Detalji koje je dobro znati

- **Kontinuitet razgovora**: HA-ov `conversation_id` se pamti i preslikava na
  Jarvisov `session_id`, pa „a ugasi i ono drugo" ima na što se osloniti.
- **Vremensko ograničenje** je 300 s jer zahtjev koji pokrene lanac alata
  legitimno zna trajati minutama. Ako ipak istekne, poruka to kaže pošteno:
  posao se najčešće dovrši i nakon što Assist odustane od čekanja.
- **Čišćenje za govor**: iz odgovora se prije izgovaranja miču markdown znakovi
  i emoji — Jarvis njima ukrašava chat, ali izgovoreni su šum.
- **Greške se ne prešućuju**: istek vremena, odbijen token i nedostupan servis
  vraćaju različite poruke, da se iz odgovora vidi što je zapravo palo.
- **Wyoming ne obara vezu na grešci**: neuspjelo prepoznavanje vraća prazan
  tekst pa Assist kaže da nije razumio, a neuspjela sinteza ipak pošalje
  start/stop par pa Assist ne čeka zvuk koji nikad neće stići.
- **Zvuk kraći od pola sekunde** ne ide na Chirp — slučajan klik nije govor, a
  mrežni put nije besplatan.
