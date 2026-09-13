# Jarvis Launcher — otvaranje TV aplikacija na zahtjev

## Zašto postoji

TCL pokreće aplikaciju samo kad mu se preda **poveznica koju je neka instalirana
aplikacija registrirala**. Izmjereno 22.08.2026. na stvarnom uređaju: YouTube i
Netflix se otvore (imaju javne poveznice), a package ime, `market://`,
`intent://` i `media_player.play_media` ne rade ništa — svi vrate HTTP 200 i TV
ostane gdje je bio.

A1 Xplore TV **ne registrira nijednu poveznicu**. Pročitano iz njegovog
`AndroidManifest.xml`: ima samo `MAIN` + `LAUNCHER`/`LEANBACK_LAUNCHER`, bez
`VIEW`, bez `BROWSABLE`, bez sheme. Zato ga ništa ne može otvoriti izvana.

ADB bi zaobišao problem, ali ovaj TV u opcijama za razvojne programere nema
mrežno otklanjanje pogrešaka — samo USB, a televizori nemaju USB port za
spajanje na računalo.

Ostaje jedno: instalirati aplikaciju koja **jest** registrirala poveznicu i koja
zna otvoriti bilo koju drugu.

## Kako radi

```
remote.turn_on(activity="jarvis://open?pkg=hr.a1.android.tv.xploretv")
   -> Android razriješi jarvis:// na ovu aplikaciju
   -> ona pozove getLaunchIntentForPackage() i otvori traženu
   -> i odmah se makne s puta (noHistory)
```

Nije vezana za A1 — radi za svaku instaliranu aplikaciju.

## Build

Bez Gradlea i bez Android Studija; sve je u Debianu na Jarvis Pi-u:

```bash
sudo apt install -y default-jdk aapt zipalign apksigner
cd deploy/tv_app_launcher
./build.sh
```

`android.jar` (stub za prevođenje) skine se jednom i ostane u mapi. Rezultat je
`build/jarvis-launcher.apk`, potpisan i poravnat.

## Instalacija na TV (sideload, bez ADB-a)

1. Na TV-u instaliraj **Downloader** (Google Play, AFTVnews) ili sličan.
2. Pokreni `python3 -m http.server 8081` u `build/` mapi na Pi-u.
3. U Downloaderu upiši `http://<ip-pi-a>:8081/jarvis-launcher.apk`.
4. Dopusti Downloaderu instalaciju iz nepoznatih izvora kad pita.
5. Instaliraj.

Provjera da je prošlo: pojavi se "Jarvis Launcher" u popisu aplikacija na TV-u,
a `remote.turn_on(activity="jarvis://open?pkg=com.google.android.youtube.tv")`
otvori YouTube.

## Nakon instalacije

`tv_open_app` u `tools/adk_tools/ha_adk_tools.py` treba slati poveznicu
`jarvis://open?pkg=<paket>` za naučene aplikacije umjesto golog package imena.
Alat već provjerava je li se aplikacija stvarno otvorila, pa će razlika biti
odmah vidljiva.
