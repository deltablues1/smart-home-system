# Kad alat kaže da ne zna je li uspio

Alati koji nešto stvaraju ili šalju NEMAJU automatsko ponavljanje, namjerno:
ponovno slanje maila ili ponovno kreiranje dokumenta nakon prekinute veze
napravi drugi primjerak, jer je upis možda već prošao a izgubio se samo odgovor.

Zato razlikuj TRI ishoda, ne dva:

| Rezultat alata | Što se dogodilo | Što radiš |
|---|---|---|
| uspjeh | radnja je izvršena | javi rezultat |
| `status: "error"` bez `outcome` | radnja NIJE izvršena (400/401/403/404, neispravan argument, nema prava) | javi grešku; ponavljanje ima smisla tek ako si prvo ispravio uzrok |
| `status: "unknown"` ili `outcome: "unknown"` | **ne zna se** je li izvršena | ne ponavljaj — provjeri |

Kod `unknown`:

1. NE ponavljaj isti poziv. To je jedini način da nastane duplikat.
2. Potraži kandidata alatom za čitanje: `gmail_search_threads`,
   `drive_search_files`, `sheets_get_values`, `contacts_search_people`,
   `tasks_list_tasks`.
3. **Pretraga daje kandidata, ne dokaz.** Rezultat je sažetak, i to sažetak
   razgovora, a ne poruke: `gmail_search_threads` vraća naslov PRVE poruke u
   razgovoru, a primatelja i datum POSLJEDNJE. Na razgovoru s više poruka ta tri
   podatka mogu pripadati trima različitim porukama, a tijela i privitka ondje
   uopće nema. Isto vrijedi za dokument nađen po nazivu i vremenu: naziv i
   vrijeme nisu sadržaj.
4. Zato **otvori kandidata** — `gmail_get_thread`, `drive_get_file`,
   `sheets_get_values` — i provjeri konkretnu stavku: da su primatelji, naslov,
   tijelo i privitak baš oni koje si slao, i da je nastala poslije tvog
   pokušaja. Tek takav nalaz znači da je radnja izvršena; javi to i stani.
5. Djelomična podudarnost NIJE potvrda. Isti primatelj i isti naslov uz drugo
   tijelo ili drugi privitak znače da si našao neku drugu poruku, ne svoju.
6. Ako kandidata ne možeš otvoriti, ili otvoreni podaci ne mogu potvrditi
   identitet radnje — ishod ostaje `unknown`.
7. **To vrijedi jednako i kad pretraga ne vrati ništa.** "Nisam našao" nije
   "nije se dogodilo": indeksiranje kasni, pretraga promaši, prava nedostaju,
   filtar je bio krivi. Prazan rezultat nije dokaz neizvršenja i NE ovlašćuje
   ponovni pokušaj.
8. U svakom `unknown` slučaju ne ponavljaj sam. Reci korisniku što si pokušao,
   što si provjerio i što nisi mogao potvrditi, pa pitaj želi li da pokušaš
   ponovno. Ponovni pokušaj pokreće njegova odluka, ne tvoja pretpostavka — to
   je jedina razlika između jednog i dva ista maila.
9. Ako provjeru uopće ne možeš napraviti (nemaš alat za čitanje, vraća grešku),
   reci upravo to: "ne znam je li mail otišao i ne mogu provjeriti — pogledaj u
   Poslanima prije nego ponovimo."

Isto vrijedi za tekst koji pišeš: `unknown` nije "poslano" i nije "nije
uspjelo". Ako ne znaš, tako i reci — pogrešno "gotovo" košta povjerenje, a
pogrešno "palo je" košta duplikat kad korisnik zatraži ponovni pokušaj.

(English summary: creating and sending tools have no automatic retry on purpose.
A result carrying status/outcome "unknown" means the write may already have
landed and only the answer was lost. A search returns a candidate, not proof --
a Gmail thread summary carries the FIRST message's subject and the LAST one's
recipient and date, and no body at all -- so open the candidate and match the
actual message, body and attachment included. A partial match, an unopenable
candidate and an empty result all leave the outcome unknown, and an unknown
outcome is never retried on your own initiative: only the user's explicit
decision authorises that. "unknown" is neither "done" nor "failed".)
