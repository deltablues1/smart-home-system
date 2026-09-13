# Hardverska konfiguracija — mjerenje snage (2 × SCT-013-000 + 1 × ZMPT101B)

Odnosi se na node `bme280-mux-node`. Cilj: dva kontinuirana strujna kanala
(vlastita potrošnja + gornji kat), jedan naponski, jedna faza, ograničavač 32 A.

---

## TRENUTNA IZVEDBA (2026-08-08) — dva ADS-a, bez trećeg

Treći ADS1115 se čeka, pa je drugi strujni kanal riješen **blokovskom izmjenom
MUX-a na ADS #1**: ostane se na `A0-A1` cijeli batch (~1 s), objavi rezultat,
zapiše se config za `A2-A3`, pa isto za CT2. Svaki kanal dobiva **punih 860 SPS
i punu kvalitetu P/VA/PF**, samo se osvježava svake 2 s umjesto svake 1 s.

Configi: CT1 = `0x04E0` (mux 000), CT2 = `0x34E0` (mux 011). Sve ostalo isto —
±2,048 V, kontinuirano, 860 SPS, COMP_QUE = 00 pa RDY prekid preživi prebacivanje.

Prednost pred trećim ADS-om: **ne dodaje nijedno I²C čitanje.** Treći ADS na
zajedničkoj sabirnici tražio bi ~111 % kapaciteta — nemoguće. Isplati se tek
nakon razdvajanja sabirnica, i tada samo zbog 1 s umjesto 2 s osvježenja.

### Sabirnice u ovoj izvedbi

| Sabirnica | GPIO | Brzina | Uređaji |
|-----------|------|--------|---------|
| `i2c_power` | 21 / 22 | **400 kHz** | ADS1115 ×2 |
| `i2c_sensors` | 18 / 19 | **50 kHz** | TCA9548A → BME280 ×5 + SPS30 |

Senzorska je namjerno spora: SPS30 je specificiran do 100 kHz, linije su duge
5–10 m, a najbrži update interval je 30 s.

**Za duge linije:** stvarni limit je kapacitet kabela (I²C dopušta 400 pF, kabel
ima 50–100 pF/m → 10 m je već preko specifikacije). Zato pull-upovi
**1,5–2,2 kΩ** na GPIO18/19, i Cat5e s upredenim paricama gdje je uz svaki
signal vlastita masa (SDA+GND, SCL+GND, 3,3 V+GND). Ako neka grana bude
nestabilna, tek onda P82B715 bufferi.

### Burden pri 100 Ω je premali

Sa 100 Ω puna skala je **23,3 A** — s ograničavačem od 32 A reže vrhove.
Dva 100 Ω paralelno daju 50 Ω → 46,7 A, uz `current_calibration: 40.0`.
Vrijednost mora biti **ista na oba kanala** da im jedna fazna korekcija odgovara.

---

## Tri odluke i obrazloženja

### 1. SPS30 ostaje na I²C, ali na drugoj sabirnici (100 kHz)

Ne na UART. Razlozi:

- ESPHome-ov `sps30` je I²C komponenta; UART bi tražio vlastitu SHDLC komponentu
  za nula mjerne koristi.
- SPS30 na 5 V ima 5 V logiku → trebao bi level shifter u oba smjera.

Druga I²C sabirnica na 100 kHz rješava specifikaciju **i** uklanja glavni uzrok
gubitka uzoraka: TCA9548A prebacivanja i BME280 čitanja više ne blokiraju
magistralu na kojoj vise ADS-ovi (simptom: `IRQ I=4148, read I=4135`).

#### Izmjereno 2026-08-08 — razdvajanje sabirnica NIJE opcionalno

Potvrđeno na uređaju: SPS30 **ne radi na 400 kHz** (`ERROR_NOT_ACKNOWLEDGED` +
ESP-IDF hardware timeout), a na 100 kHz se odmah javi
(serijski broj `8F66E4A835E65C40`, firmware v2.2, nula grešaka).

Ali cijena je mjerljiva:

| Sabirnica | read I / 5 s | Gubitak | Uzoraka/periodi |
|-----------|--------------|---------|-----------------|
| 400 kHz | 4137 od 4149 | 0,3 % | 16,5 |
| **100 kHz** | **3856 od 4152** | **7,1 %** | **15,4 |

Na 100 kHz `read I` i `read V` postaju **točno jednaki** (3856/3856) — potpis
zasićene petlje: I²C vrijeme, a ne ADS, postaje usko grlo. Opterećenje
magistrale je ~74 % (480 µs po čitanju × 1542 čitanja/s).

Posljedica: **treći ADS1115 za SCT #2 je na jednoj 100 kHz sabirnici neizvediv.**
Razdvajanje na I²C-A (ADS, 400 kHz) i I²C-B (TCA + BME280 + SPS30, 100 kHz) je
preduvjet za drugi strujni kanal, ne kozmetika.

Do tada je YAML ostavljen na 100 kHz: SPS30 radi, mjerenje snage gubi ~7 %
uzoraka što je podnošljivo.

### 2. Treći ADS1115 na `0x4A`, ne dijeljenje postojećeg

ADS1115 **nema hardverski sekvencer**. Svaka promjena kanala je upis u config
registar koji restarta konverziju → ~300 SPS s nepravilnim razmacima.
Treći čip radi jednokanalno kontinuirano kao i prva dva. Ukupno 3 toka = 31 %
opterećenja sabirnice, bez konkurencije.

### 3. Burden 100 Ω → 56 Ω (OBAVEZNO)

Uz 3,3 V napajanje i VBIAS na 1,65 V ulaz ADS-a smije ići samo 0…3,3 V, pa je
vršni napon na burdenu ograničen na **1,65 V**, ne na PGA-ovih 2,048 V.

| Burden | Puna skala |
|--------|-----------|
| 100 Ω (staro) | 23,3 A — premalo za 32 A ograničavač |
| **56 Ω** | **41,7 A** |

Sa 100 Ω bi glavni vod odrezao vrhove već na 23 A, tiho i bez poruke.
Bonus: manji burden smanjuje faznu pogrešku kliješta (ide kao R_burden/ωL_m).

---

## Popis za nabavu (~4,50 €)

| Dio | Kom | Cijena |
|-----|-----|--------|
| ADS1115 pločica | 1 | 2,00 € |
| Otpornik 56 Ω, 1 %, metal-film | 2 | 0,20 € |
| Otpornik 1 kΩ, 1 %, metal-film | 2 | 0,20 € |
| Otpornik 10 kΩ | 3 | 0,10 € |
| Kondenzator 0,33 µF film (MKT) | 2 | 0,40 € |
| Kondenzator 33 nF film | 1 | 0,15 € |
| Elektrolit 100 µF | 1 | 0,15 € |
| Utičnica 3,5 mm stereo | 2 | 1,00 € |
| Zener 5,1 V | 4 | 0,30 € |

Burden **mora** biti metal-film 1 %, ne ugljični — inače temperaturni
koeficijent šeta kalibraciju.

---

## Arhitektura sabirnica

| Sabirnica | Brzina | Uređaji |
|-----------|--------|---------|
| **I²C-A** GPIO21/22 | 400 kHz | ADS1115 ×3 — ništa drugo |
| **I²C-B** GPIO18/19 | 100 kHz | TCA9548A `0x70` → 5× BME280; SPS30 `0x69` izravno |

SPS30 ide izravno na I²C-B, ne kroz TCA9548A (adresa `0x69` se ne sudara).
TCA kanal 5 time postaje slobodan.

## GPIO mapa

| GPIO | Funkcija |
|------|----------|
| 21 / 22 | SDA / SCL — I²C-A (ADS) |
| 18 / 19 | SDA / SCL — I²C-B (TCA, BME280, SPS30) |
| 33 | ALERT/RDY — ADS #1 (SCT vlastita potrošnja) |
| 32 | ALERT/RDY — ADS #2 (ZMPT) |
| 25 | ALERT/RDY — ADS #3 (SCT kat) — novo |

Izbjegnuti: GPIO 0/2/12/15 (strapping), 6–11 (flash), 34–39 (nemaju interni
pull-up, a ALERT/RDY je open-drain).

## Adrese ADS1115

| ADS | ADDR na | Adresa | Ulazi |
|-----|---------|--------|-------|
| #1 | GND | `0x48` | A0 = SCT1, A1 = VBIAS |
| #2 | VDD (3,3 V) | `0x49` | A0 = ZMPT, A1 = GND |
| #3 | **SDA** | `0x4A` | A0 = SCT2, A1 = VBIAS |

Na jeftinim pločicama je ADDR već povezan na GND — za `0x4A` prekini tu vezu i
povuci žicu na SDA.

---

## Spajanje

### A. Napajanje

| Izvor | Na |
|-------|-----|
| ESP32 `5V` (VIN) | ZMPT101B `VCC`, SPS30 pin 1 `VDD` |
| ESP32 `3V3` | ADS ×3 `VDD`, TCA9548A `VCC`, BME280 ×5 `VCC` |
| ESP32 `GND` | sve mase, jedna zajednička točka |

Masu vodi zvjezdasto od jedne točke uz ESP32. Ne ulančavaj mase mjernih
dijelova preko digitalnih.

### B. VBIAS — jedan čvor za oba SCT-a

```
3,3 V ──[10 kΩ]──┬── VBIAS (≈1,65 V)
                 │
               [10 kΩ]
                 │
                GND
```

Na čvor VBIAS: **100 µF elektrolit + 100 nF keramika prema GND**. Drži ga
nisko-impedantnim na 50 Hz, bitno sad kad ga dijele dva kanala.

VBIAS ide na **ADS #1 A1** i **ADS #3 A1**. Jedan djelitelj, dva kanala.

### C. Strujni kanal — dvaput (ADS #1 i ADS #3)

```
   Utičnica 3,5 mm
   kontakt A ──┬──────[1 kΩ]──┬───────── ADS  A0
               │              │
            [56 Ω]        [0,33 µF]
            burden            │
               │              │
   kontakt B ──┴──────────────┴───┬───── ADS  A1
                                  │
                              VBIAS čvor
```

1. **Odredi aktivna dva kontakta utičnice.** SCT-013 koristi 3,5 mm stereo
   utikač, ali su spojene samo dvije od tri točke i proizvođači se razlikuju.
   Ommetrom traži kontinuitet — sekundar SCT-013-000 ima ~60–100 Ω istosmjerno.
2. **Burden 56 Ω lemi izravno na kontakte utičnice**, žice kraće od centimetra.
   Jedini dio koji ne smije nikad zakazati.
3. **Dva zenera 5,1 V antiserijski (katoda na katodu) preko burdena.** Ako
   burden pukne dok su kliješta na živom vodiču, sekundar razvija napon.
4. **RC 1 kΩ + 0,33 µF** → granica ~480 Hz. Bez toga se sve iznad 415 Hz
   presavija u osnovni pojas. 1 kΩ usput ograničava struju kvara prema ADS
   ulazu na ~2 mA, pa dodatna zaštita nije potrebna.
5. Kondenzator **film (MKT/poliester)**, ne keramika. X7R mijenja kapacitet s
   naponom i temperaturom → pomiče faznu kalibraciju.

Isti burden i isti RC na oba kanala → **ista fazna korekcija za oba**.

### D. Naponski kanal

```
ZMPT101B OUT ──[22 kΩ]──┬── ADS #2  A0
                        │
                    [22 kΩ]  [33 nF]
                        │       │
                       GND     GND
```

Jedina promjena je **33 nF prema masi na A0**. Djelitelj 22k/22k ima Thévenina
11 kΩ → τ = 363 µs, praktički jednako strujnim kanalima (330 µs). Kad su
izjednačene, dodani fazni pomaci se gotovo pokrate.

ADS #2 A1 ostaje na GND (mjerenje A0-GND, jednostrano).

Trimer na ZMPT modulu **ne dirati**, zapečatiti lakom — određuje i pojačanje i
fazu.

### E. ALERT/RDY

Sve tri open-drain, aktivne u nuli. **Vanjski 10 kΩ pull-up na 3,3 V** na svaku.

| ADS | → GPIO | Pull-up |
|-----|--------|---------|
| #1 `0x48` | 33 | 10 kΩ → 3,3 V |
| #2 `0x49` | 32 | 10 kΩ → 3,3 V |
| #3 `0x4A` | 25 | 10 kΩ → 3,3 V |

Interni ESP32 pull-up je ~45 kΩ; s tri linije na 860 SPS vanjskih 10 kΩ daje
puno čišći brid.

### F. SPS30 na I²C-B

| SPS30 pin | Na |
|-----------|-----|
| 1 `VDD` | 5 V |
| 2 `SDA` | GPIO18 |
| 3 `SCL` | GPIO19 |
| 4 `SEL` | **GND** (bira I²C način) |
| 5 `GND` | GND |

Pull-upovi I²C-B: 4,7 kΩ na 3,3 V na SDA i SCL, osim ako ih TCA pločica već ima.

Napomena: SPS30 se napaja s 5 V pa mu je logika 5 V, a vozimo ga 3,3 V
pull-upovima. `VIH` mu je nominalno 3,5 V → 3,3 V je na rubu. Radi empirijski
(kao i u većini ESPHome instalacija), ali nije po katalogu. Prelazak na 100 kHz
rješava brzinsku specifikaciju; naponsku bi rješavao level shifterom.

### G. BME280 na TCA9548A

Bez promjene, samo nova sabirnica: TCA `SDA/SCL` → GPIO18/19, kanali 0–4 kao
dosad.

---

## Redoslijed sastavljanja

Svaki korak ima provjeru koja mora proći prije sljedećeg.

1. **Prebaci BME280 + SPS30 na I²C-B**, ADS-ove ostavi na I²C-A. Flash, pa
   provjeri da `scan: true` na obje sabirnice nađe očekivane adrese. Mjerenje
   snage mora raditi kao prije.
2. **Provjeri diagnostiku.** `IRQ` i `read` brojevi trebaju biti puno bliži nego
   4148 / 4135 — dokaz da je kontencija nestala.
3. **Zamijeni burden SCT #1 sa 100 Ω na 56 Ω**, dodaj RC filtar i 33 nF na
   naponskom. Ne dodaj još drugi kanal.
4. **Rekalibriraj s kuhalom** — pojačanje i fazu. Očekuj da
   `voltage_lag_correction_us` osjetno odstupi od −1280 zbog filtara.
5. **Tek onda dodaj ADS #3 i SCT #2.**

---

## Postavljanje kliješta — sigurnost

1. **Isključi glavnu sklopku** prije otvaranja razdjelnika.
2. Kliješta **samo oko faznog vodiča** svakog dovoda. Nikad oko L+N zajedno.
3. Ormarić brojila je plombiran — ne dirati. Radi iza glavne sklopke.
4. Kliješta mehanički fiksirati da ne mogu pasti na sabirnicu. Kabel kroz
   propisnu uvodnicu.
5. **Nikad ne odspajati burden dok su kliješta na živom vodiču.**
6. Paziti na **smjer** kliješta (strelica na kućištu). Obrnuto → negativna
   snaga; popravlja se s `invert_current`, ali je urednije fizički jednako
   orijentirati.

Oba voda su na istoj fazi → jedan ZMPT poslužuje oba kanala. Besplatna
provjera: **CT1 + CT2 mora otprilike odgovarati brojilu.**

---

## Kalibracija — redoslijedom

1. **Napon prvo**, prema **objavljenom senzoru `Mrezni napon`**, ne prema sirovom
   ADS RMS-u. Postojećih 2427,1 je izveden prije nego je interpolacija
   postojala → Vrms je oko 1,2 % nizak. 20–30 stabilnih očitanja vs. Rigol.
2. **Faza**, s kuhalom, na jednom kanalu. Traži `voltage_lag_correction_us` koji
   dovodi PF najbliže 1,000.
3. **Pojačanje struje**, ne prema natpisnoj pločici. Najbolje: fototranzistor na
   kalibracijski LED brojila, jedno stabilno trošilo, 20–30 min. Alternativa:
   otpor grijača izmjeren Rigolom dok je kuhalo isključeno.
4. Drugi kanal istim postupkom — treba tražiti istu faznu korekciju jer je
   analogni lanac identičan.

---

## Što ostaje u firmwareu

Komponenta `phase_power` je pisana za **jedan** strujni kanal — jedan
`I2CDevice`, jedan RDY pin, jedan red čekanja, jedan set akumulatora
(`phase_power.h:43-79`). Za drugi kanal treba refaktor: strujna strana postaje
polje struktura, naponski tok ostaje zajednički, interpolacija se izvede dvaput
po naponskom uzorku.

Instanciranje komponente dvaput **ne prolazi**: obje instance bi čitale isti
konverzijski registar ADS #2 i borile se za isti prekid na GPIO32.

U istom prolazu popraviti i:

- objavu `rms_voltage` koja se preskače pri malom teretu
  (`phase_power.cpp:203-209`) — Vrms se zamrzne čim se ugasi trošilo;
- detekciju zasićenja, koja potpuno nedostaje.

Do koraka 4 postojeći kod radi bez izmjena.
