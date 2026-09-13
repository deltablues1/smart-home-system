# PINOUT — bme280-mux-node (stanje 2026-08-08)

Referenca za ponovno spajanje. Sve provjereno na uređaju, ne iz glave.

---

## 1. ESP32 — svi korišteni pinovi

| GPIO | Funkcija | Ide na |
|------|----------|--------|
| **21** | SDA — sabirnica A (`i2c_power`, 400 kHz) | ADS #1 SDA, ADS #2 SDA |
| **22** | SCL — sabirnica A | ADS #1 SCL, ADS #2 SCL |
| **18** | SDA — sabirnica B (`i2c_sensors`, 50 kHz) | TCA9548A SDA |
| **19** | SCL — sabirnica B | TCA9548A SCL |
| **33** | ALERT/RDY ADS #1 (struja) | ADS #1 ALRT |
| **32** | ALERT/RDY ADS #2 (napon) | ADS #2 ALRT |
| **3V3** | napajanje | ADS ×2, TCA9548A, BME280 ×5 |
| **5V (VIN)** | napajanje | ZMPT101B VCC, SPS30 pin 1 |
| **GND** | zajednička masa | sve — zvjezdasto iz jedne točke |

Ništa drugo nije zauzeto. GPIO25 je bio predviđen za treći ADS — zasad slobodan.

### Pull-upovi

| Gdje | Vrijednost | Napomena |
|------|-----------|----------|
| GPIO21, GPIO22 → 3,3 V | 4,7 kΩ | samo ako scan ne nađe `0x48` i `0x49`; ADS pločice obično imaju svoje |
| GPIO18, GPIO19 → 3,3 V | **1,5–2,2 kΩ** | obavezno, zbog dugih linija do senzora (5–10 m) |
| GPIO33, GPIO32 → 3,3 V | 10 kΩ | ALERT/RDY je open-drain |

---

## 2. Sabirnica A — dva ADS1115

| ADS | ADDR pin na | Adresa | Uloga |
|-----|-------------|--------|-------|
| #1 | **GND** | `0x48` | dvije struje |
| #2 | **3,3 V** | `0x49` | napon |

### ADS #1 (`0x48`) — analogni ulazi

| Pin | Spojeno na |
|-----|-----------|
| **A0** | CT1 — kliješta "moja potrošnja", vrući kraj |
| **A1** | **NIJE SPOJEN** (2026-09-13) — shema je predvidjela VBIAS, ali zice nema. Zato kanal 1 cita `A0-A3` (MUX 001), ne `A0-A1`. |
| **A2** | CT2 — kliješta "kat", vrući kraj |
| **A3** | VBIAS |

Po kanalu, burden **47 Ω zalemljen izravno na kontakte utičnice 3,5 mm**:

```
   Utičnica 3,5 mm
   kontakt A ──────────────────── A0  (odnosno A2)
               │
            [47 Ω]
               │
   kontakt B ──┴──────────────┬── A1  (odnosno A3)
                              │
                          VBIAS čvor
```

**A1, A3, povratni kraj CT1, povratni kraj CT2 i djelitelj — svih pet u JEDNOJ
fizičkoj točki.** Nijedan zajednički segment žice prije te točke; upravo je to
bio uzrok sprege od 1,1 % koju smo lovili.

### ADS #2 (`0x49`) — analogni ulazi

| Pin | Spojeno na |
|-----|-----------|
| **A0** | ZMPT izlaz preko djelitelja 22k/22k |
| **A1** | **GND** (mjerenje je jednostrano A0–GND) |
| A2, A3 | slobodni — ne spajati, degradiralo bi naponski tok |

```
ZMPT101B OUT ──[22 kΩ]──┬── ADS#2 A0
                        │
                    [22 kΩ]
                        │
                       GND
```

---

## 3. VBIAS

```
3,3 V ──[10 kΩ]──┬── VBIAS (≈1,65 V) ──> ADS#1 A1 i A3 + oba povratna kraja CT
                 │
               [10 kΩ]
                 │
                GND
```

Na čvor: **47 µF elektrolit + 104 (100 nF) keramika prema masi.** Provjereno
dovoljno za dvije strujne petlje koje ga dijele.

---

## 4. Sabirnica B — TCA9548A (`0x70`)

`SDA` → GPIO18, `SCL` → GPIO19, `VCC` → 3,3 V, `A0/A1/A2` → GND (daje `0x70`).

### KANALI — koji senzor ide na koji kanal

| Kanal | `bus_id` | Uređaj | Adresa | Entiteti u HA |
|-------|----------|--------|--------|---------------|
| **0** | `tca_ch0_vanjska` | BME280 | 0x76 | Vanjska temperatura / vlaga / tlak |
| **1** | `tca_ch1_ulaz` | BME280 | 0x76 | Ulaz temperatura / vlaga / tlak |
| **2** | `tca_ch2_dnevni` | BME280 | 0x76 | Dnevni prostor temperatura / vlaga / tlak |
| **3** | `tca_ch3_kupaona` | BME280 | 0x76 | Kupaona temperatura / vlaga / tlak |
| **4** | `tca_ch4_soba` | BME280 | 0x76 | Soba temperatura / vlaga / tlak |
| **5** | `tca_ch5_sps30` | SPS30 | 0x69 | Kvaliteta zraka PM1 / PM2.5 / PM4 / PM10 |

Svaki kanal ima svoj par `SDn` / `SCn` na TCA pločici.

**Pazi:** svih pet BME280 ima istu adresu `0x76`. Ako zamijeniš kanale, senzori
će raditi ali će sobe biti krivo imenovane, a to se u logu **ne vidi** — nema
načina da firmware to primijeti. Označi kabele prije odspajanja.

### BME280 modul (GY-BME280) — po kanalu

| Pin modula | Na |
|------------|-----|
| VCC | 3,3 V |
| GND | GND |
| SCL | TCA `SCn` odgovarajućeg kanala |
| SDA | TCA `SDn` odgovarajućeg kanala |
| SDO | GND (daje adresu 0x76) |
| CSB | 3,3 V (bira I²C) |

Za linije 5–10 m: **Cat5e, po jedna masa uz svaki signal** — parica 1 = SDA+GND,
parica 2 = SCL+GND, parica 3 = 3,3 V+GND. Ne pljosnati kabel.

### SPS30 — kanal 5

| Pin | Na |
|-----|-----|
| 1 `VDD` | **5 V** |
| 2 `SDA` | TCA `SD5` |
| 3 `SCL` | TCA `SC5` |
| 4 `SEL` | **GND** (bira I²C) |
| 5 `GND` | GND |

Pin 1 je uz označeni rub konektora. `SEL` mora biti na masi — bez toga senzor
ode u UART način i na I²C ga nema.

---

## 5. ZMPT101B

| Pin | Na |
|-----|-----|
| VCC | 5 V |
| GND | GND |
| OUT | → 22 kΩ → ADS#2 A0 (i 22 kΩ s A0 na GND) |

**Trimer ne dirati** — određuje i pojačanje i fazu, a `voltage_calibration`
i `voltage_lag_correction_us` su vezani uz njegov trenutni položaj. Zapečatiti
lakom.

---

## 6. Kliješta SCT-013-000

| Kanal | Kliješta | Burden | Kalibracija |
|-------|----------|--------|-------------|
| 1 (**A0-A3**) | vlastita potrošnja | 47 Ω | 42,553 (globalna) |
| 2 (A2-A3) | gornji kat | 47 Ω | **42,09** (izmjereno: čita 1,011× više) |

- Puna skala **49,7 A** po kanalu; upozorenje u logu na 46,6 A
- Kliješta **samo oko faznog vodiča**, nikad L+N zajedno
- Strelica na kućištu — ista orijentacija na oba
- **Nikad ne odspajati burden dok su kliješta na živom vodiču**
- Glavnu sklopku isključiti prije otvaranja razdjelnika

---

## 7. Provjera nakon spajanja

U boot logu mora biti:

```
ESPHome version 2026.7.0 compiled on <datum tvoje zadnje kompilacije>
  SDA Pin: GPIO21 ... Frequency: 400000 Hz
    Found device at address 0x48
    Found device at address 0x49
  SDA Pin: GPIO18 ... Frequency: 50000 Hz
    Found device at address 0x70
  Current channels: 2 (A0-A1 and A2-A3, alternating per batch)
  Serial number: 8F66E4A835E65C40      <- SPS30 živ
  Firmware version v2.2
```

- **nula** redaka s `[E]` ili `marked as failed`
- `RDY / 5 s`: `read I` mora biti ~0,3 % ispod `IRQ I`; `paired` je 10 manje od
  `read` (2 odbačene konverzije × 5 prebacivanja MUX-a u 5 s) — to je ispravno
- `Mrezni napon` vidljiv **i kad ništa nije upaljeno**
- `Synchronized batch: ch=1` i `ch=2` naizmjenično

Za BME280 sobe: dahni u jedan senzor i gledaj koji entitet skoči. Jedini način
da provjeriš da kanali nisu zamijenjeni.
