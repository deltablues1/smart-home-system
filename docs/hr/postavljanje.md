# Postavljanje

> 🇬🇧 [English version](../en/deployment.md)

Kako se Jarvis Pi postavlja i održava. Putanje i korisnik ispod su oni koje
koristi živi Pi i koje prikivaju systemd jedinice. Ako su tvoji drukčiji,
promijeni zajedno jedinice i `deploy/rpi/setup.sh`; unit test provjerava da se
slažu.

---

## 1. Osnovni sustav

Raspberry Pi OS (64-bit) na Raspberry Pi 5, Python 3.11, SSH s ključem i
statička adresa postavljena na samom Piju.

```bash
sudo apt-get update && sudo apt-get full-upgrade -y
```

## 2. Kod i ovisnosti

```bash
git clone <repozitorij> /home/raspberrypijarvis/google_claude
cd /home/raspberrypijarvis/google_claude
./deploy/rpi/setup.sh
```

`setup.sh` redom:
- instalira sistemske pakete (ALSA, PortAudio, ffmpeg, alate za izgradnju);
- napravi `.venv`;
- instalira fiksirani `requirements.txt`;
- kopira systemd jedinice u `/etc/systemd/system`.

## 3. Postavke i tajne

```bash
cp .env.example .env
nano .env
mkdir -p secrets && chmod 700 secrets
# kopiraj JSON Google service accounta u secrets/, zatim:
chmod 600 .env secrets/*
```

[`.env.example`](../../.env.example) je grupiran po podsustavima i nosi podešene
vrijednosti. Dva pravila:
- komentari idu u zaseban redak, jer systemdov `EnvironmentFile=` komentar na
  kraju retka zadrži kao dio vrijednosti;
- kod ponovljenog ključa vrijedi **zadnje** pojavljivanje.

### Google računi

- **Vertex AI, Firestore, Cloud TTS/STT**: service account.
- **Gmail, Kalendar, Drive, Docs, Sheets, Kontakti, Tasks**: OAuth kao kućni
  račun. Autorizira se jednom:

  ```bash
  .venv/bin/python tools/oauth_cli.py --auth     # povratni poziv u lokalni preglednik
  .venv/bin/python tools/oauth_cli.py --manual   # zalijepi URL preusmjeravanja, radi i s mobitela
  ```

  Traženi opsezi neka budu samo oni koje alati koriste. Širok opseg
  `cloud-platform` na korisničkom tokenu pokretao je Googleovu periodičnu
  ponovnu prijavu i svako jutro tiho ubijao token.

- **Gemini Transcribe**: Developer API ključ s AI Studio kreditom, koji se
  naplaćuje odvojeno od Google Clouda.

## 4. Zvuk

```bash
bash deploy/rpi/setup_audio.sh
arecord -l && aplay -l
```

`WAKEWORD_INPUT_DEVICE` postavi na uređaj za snimanje, a
`WAKEWORD_OUTPUT_DEVICE=default` ostavi (vidi [hardver](hardver.md#zvuk-na-jarvis-piju)).

## 5. Servisi

```bash
sudo systemctl enable --now adk-web adk-wakeword adk-telegram adk-scheduler adk-wyoming
journalctl -u adk-web -f
```

| Jedinica | Watchdog | Napomene |
|----------|----------|----------|
| `adk-web` | — | prije pokretanja vrti `scripts/check_oauth_health.py` |
| `adk-wakeword` | — | treba zvučni uređaj |
| `adk-telegram` | 180 s | pri kobnoj grešci izlazi tvrdo pa ga systemd restarta |
| `adk-scheduler` | 180 s | **jedini** proces koji izvršava poslove |
| `adk-wyoming` | da | port 10400 za Home Assistant |

Za restart treba `sudo`, a aplikacijski korisnik ga nema. Kao zamjena radi i
gašenje glavnog procesa servisa kao njegov vlastiti korisnik: `Restart=always`
ga vrati sa svježe pročitanom datotekom okoline.

## 6. Strana Home Assistanta

1. Kopiraj `deploy/home_assistant/custom_components/jarvis/` u
   `/config/custom_components/` i restartaj Home Assistant. Promjena koda traži
   puni restart, ne samo ponovno učitavanje unosa.
2. Dodaj integraciju **Jarvis** (adresa i `API_TOKEN`) i integraciju **Wyoming
   Protocol** (Jarvis Pi, port 10400).
3. Napravi Assist pipeline *Jarvis* od te tri komponente.
4. Kopiraj `deploy/home_assistant/www/*.js` u `/config/www/`, registriraj
   `jarvis-sky.js` pod `frontend: extra_module_url` i restartaj.
5. Izgradi dashboard: `HA_URL=... HA_TOKEN=... python
   deploy/home_assistant/tools/build_dashboard.py`.

Detalji: [`JARVIS_ASSIST.md`](../../deploy/home_assistant/JARVIS_ASSIST.md).

## 7. Zidni panel

`deploy/rpi/panel-kiosk.sh` pokreće se iz `~/.config/labwc/autostart` (primjer u
`deploy/rpi/labwc-autostart`). Ne treba root i poništava se brisanjem jednog
retka.

---

## Ažuriranje

```bash
# na razvojnom računalu
git push
# na Piju
cd ~/google_claude && git pull --ff-only
sudo systemctl restart adk-web adk-wakeword adk-telegram adk-scheduler adk-wyoming
```

Povučeni kod bez restarta ne radi ništa. Prije postavljanja koje mijenja format
spremljenih podataka napravi snimku stanja:

```bash
.venv/bin/python scripts/backup_runtime_state.py
```

Sprema spremište sesija, `data/` i trenutni commit. Vraćanje je namjerno ručno.

## Provjera postavljanja

```bash
.venv/bin/python scripts/rpi_smoke_check.py      # profil, okolina, zvuk, broker
curl -H "Authorization: Bearer $API_TOKEN" http://localhost:8000/api/status
```

Korisni retci u logu:
- `Direct voice route ->` pokazuje koja je traka preuzela izgovoreni zahtjev;
- `[TOOL]` i `[TOOL=]` pokazuju svaki poziv alata unutar agenta i njegov
  rezultat;
- `[TOKENS]` pokazuje koliko je upit koštao.

## Sigurnost

- Na profilu `rpi-home` API traži bearer token i sluša na lokalnoj mreži; ništa
  nije izloženo internetu. Udaljeni pristup ide kroz Home Assistant preko
  Tailscalea.
- Tajne su u `.env` i `secrets/`. Git ih ignorira, a čitati ih može samo
  korisnik servisa.
- Telegram prihvaća samo chatove iz `TELEGRAM_AUTHORIZED_CHAT_IDS`.
