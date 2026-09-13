# Home Assistant Integration Notes

## Zidni dashboard Jarvis

Generator je `tools/build_dashboard.py`. Panel ima 1024 × 600 piksela;
`deploy/rpi/panel-kiosk.sh` koristi skaliranje 0,8, odnosno viewport 1280 × 750.
Pri provjeri visine treba sklopiti bočnu traku kao na panelu.

- **Sobe:** jednaka kartica za svaku sobu; dodir naziva ili ikone otvara komande
  i mjerenja te sobe. Strelica vraća na popis. Otvaranje sobe ne pali uređaj.
- **Klima i Zrak:** zajednički izbor razdoblja iznad grafova. Ako helper još nije
  dostupan, dnevni graf ostaje prikazan. Dnevni temperaturni ekstremi dostupni su
  preko gumba **Današnji min / max** na Klimi.
- **Mjesec:** naziv i ikona dolaze iz `sensor.moon_phase`. Animiranu pozadinu i
  položaj Mjeseca i dalje crta `www/jarvis-sky.js`; Mjesec može biti ispod horizonta
  ili iza kartica, neovisno o prikazanom nazivu mijene.

- **Lista za kupovinu:** na pregledu je pločica s brojem stavki; dodir otvara
  podprikaz `kupovina` s pravom listom. Pločica sama prikazuje samo *stanje*
  entiteta, a to je broj otvorenih stavki — zato dodir vodi dalje.
- **Kanali:** gumb A1 Xplore otvara podprikaz `kanali` s programiranim
  kanalima. Popis dolazi iz `config/tv_channels.json` (gitignoriran, uči se
  govorom: *"N1 je 105"*), pa se novi kanal pojavi na panelu nakon sljedećeg
  pokretanja generatora. Kanali bez broja nemaju gumb. Dodir zove
  `script.jarvis_tv_kanal`, koji je HA-ova replika alata `tv_channel` —
  **skripta se ne generira ovim alatom** i živi u HA-u.
- **Glasnoća:** klizač pripada cast entitetu i mrtav je dok TV prikazuje
  program; gumbi Tiše/Mute/Glasnije rade preko `media_player.tv`, koji zna
  samo korake. Oba su namjerno prisutna.
- **Overview:** generator preslikava isti board i na zadani dashboard, jer
  otvaranje HA-a s mobitela ili računala vodi onamo. Isključuje se s
  `MIRROR_TO_OVERVIEW=false`.

- **Energija:** naslovnica prikazuje trenutačnu snagu kuće i etaža, uz ukupnu
  energiju od početka mjerenja. Tab Energija (`potrosnja`) ima širi graf snage
  i procjenu brojila; izbor razdoblja mijenja graf, ne kumulativna očitanja.
  Procjena je početno očitanje + integrirana energija, nije izravno očitanje
  brojila. Nedostupan senzor prikazuje se kao crtica, a ne nula.
  Prije pokretanja generatora kopirati `www/jarvis-energy-card.js` u HA
  `/config/www/jarvis-energy-card.js`. Generator registrira njegovu verzioniranu
  `/local/` adresu kao Lovelace modul. Kartica nema vanjske biblioteke ni alate
  koji uključuju uređaje; dodir očitanja otvara standardne detalje senzora.

Prije pokretanja generatora postaviti `HA_URL`, `HA_TOKEN` i `DASHBOARD_BACKUP`
(putanju postojeće mape za sigurnosnu kopiju). Generator sprema prethodnu
konfiguraciju prije upisa nove. Za obnovu se spremljeni JSON šalje preko HA
WebSocket naredbe `lovelace/config/save` za `url_path: jarvis-dom`.
Kopija prije uređenja 6. 9. 2026. spremljena je na Piju u
`~/google_claude/data/dashboard-backups/jarvis-dom-20260906-100501.json`.

## Recommended Architecture

If Home Assistant already exists in your setup, keep this project as a separate service on the Raspberry Pi and integrate it with HA over LAN.

Recommended split:
- Home Assistant remains the smart-home control plane
- this project remains the agent, voice, Telegram, and orchestration plane

That avoids forcing this repo into a Home Assistant app/add-on shape before it is necessary.

## Best V1 Integration Options

## 1. HA Dashboard Web App

Use a Home Assistant dashboard `webpage` card or sidebar entry that points to:

`http://<rpi-ip>:8000`

This is the fastest path if you want the web UI available inside Home Assistant.

Example YAML is in:
- `deploy/home_assistant/examples/webpage_dashboard.yaml`

## 2. HA REST Status + Trigger Bridge

Use Home Assistant `rest` and `rest_command` definitions to:
- show API health and deployment profile in HA
- trigger simple agent requests from HA automations

Example YAML is in:
- `deploy/home_assistant/examples/google_clause_status.yaml`

## 3. MQTT Bridge

This is the strongest long-term integration if your house already relies on MQTT.

Recommended next step for HA-native feel:
- publish health and voice-mode state over MQTT
- publish command/result events over MQTT
- expose entities with MQTT Discovery

Current minimal implementation exposes these MQTT Discovery entities:
- current voice mode
- online/offline status
- last command/result

Home Assistant can also send a mode change back through MQTT:
- `agent`
- `live`

## 4. Future HA-Native Paths

If you later want tighter HA ownership, there are two stronger options:

- custom HA integration
  - best when you want entities, services, config flow, and better HA UX
- HA app/add-on
  - best when you want the whole service lifecycle managed by Home Assistant Supervisor

The add-on path is possible, but it is not the best first deployment target because the current repo is already shaped around `venv + systemd`.

## Recommended Decision

For v1:
- run this project on `Raspberry Pi OS Lite 64-bit`
- keep Home Assistant separate
- embed the web UI in HA
- add REST status now
- add MQTT Discovery next if you want HA-native entities
