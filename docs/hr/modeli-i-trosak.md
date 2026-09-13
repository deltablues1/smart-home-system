# Modeli i trošak

> 🇬🇧 [English version](../en/llm-and-cost.md)

Agenti su izgrađeni na Google ADK-u, koji izvorno govori s Geminijem. Claudeu se
pristupa kroz ADK-ov omotač za LiteLLM, po agentu. Gemini ostaje svugdje gdje
treba značajka dostupna samo kod Googlea. Svaki poziv modelu se broji, cijeni i
ograničava.

Izvor istine:
- `agents/adk_agents/adk_agent_factory.py`;
- `config/runtime_patches.py`;
- `tools/observability/token_stats.py`;
- `services/budget.py`.

---

## Izbor modela po agentu

```bash
LLM_PROVIDER=anthropic          # ili gemini
ANTHROPIC_API_KEY=...
```

Uz `anthropic` svaki agent model određuje ovim redom:

1. `CLAUDE_AGENT_MODELS`: izričiti parovi `agent=model` imaju prednost pred
   svime.
2. Radnici za sadržaj i rasuđivanje (analyst, mailer, researcher, scribe,
   synthesizer) → `CLAUDE_PRO_MODEL`.
3. Svi ostali → razina preslikana s Gemini razine agenta (`CLAUDE_FLASH_MODEL`,
   `CLAUDE_LITE_MODEL`).

Agenti koji ovise o Vertex AI RAG korpusima (`socrates`, `christian_guide`)
uvijek ostaju na Geminiju, a `CLAUDE_GEMINI_ONLY_AGENTS` dodaje još agenata. Ako
nema LiteLLM-a ili ključa, tvornica zapiše upozorenje i za tog se agenta vrati na
Gemini umjesto da padne.

Na Piju svaki Claude agent vrti `claude-sonnet-5` s uključenim prilagodljivim
razmišljanjem.

### Što Sonnet 5 radi drukčije

- **Odbija nezadane parametre uzorkovanja.** Tvornica zato uklanja
  `temperature` za Sonnet 5, Opus 4.7+ i Fable. Dva agenta su konfiguraciju
  generiranja slagala ručno, nakon što ju je tvornica već očistila, i time
  srušila svaki orkestrirani zahtjev. Nakon toga je zajednički pomoćnik
  `claude_safe_generation_kwargs` postao jedini način da se ona složi.
- **Tokeni razmišljanja troše izlazni limit.** Limiti ispod 8.192 rezali su JSON
  poziva alata usred argumenta, i tako je jedan agent ušao u petlju s neispravnim
  pozivom. Tvornica ih zato podiže.
- **LiteLLM odbacuje nepodržane parametre**, kao zadnja linija obrane.

---

## Predmemorija promptova

Anthropic čitanje iz predmemorije naplaćuje otprilike desetinu cijene ulaza, pa
je statični dio svakog prompta označen za predmemoriju. Dva detalja odlučila su
između predmemorije koja radi i `cached=0`:

- **Granica predmemorije.** Promptovi kojima treba trenutno vrijeme drže ga ispod
  oznake `<!-- CACHE_BREAK -->`. Sve iznad je jednako pri svakom zahtjevu i ide u
  predmemoriju, a redak sa satom ne ide. Datum na vrhu prompta rušio je
  predmemoriju svake minute.
- **Točka predmemorije na kraju razgovora**, ne samo na sistemskom promptu.
  Agenti s alatima u svakom krugu petlje ponovno šalju cijelu povijest. Bez druge
  točke duboko istraživanje platilo je punu cijenu za iste skrejpane stranice
  osam puta ([pitanje od 6,28 $](inzenjerske-biljeske.md#istraživačko-pitanje-od-628-)).

---

## Brojanje tokena

`tools/observability/token_stats.py` se kači na ADK-ov `after_model_callback`,
pa vidi svaki poziv modelu bez obzira na pružatelja. Po agentu i modelu bilježi:
- broj poziva;
- ulazne i izlazne tokene;
- tokene pročitane iz predmemorije;
- procijenjeni trošak.

Nakon svakog upita izvještaj ide u log:

```
[TOKENS]
TOKEN USAGE (turn)
agent             model                 calls       in     out  cached       ~$
smart_orchestrator claude-sonnet-5          2     9120     410    8400   0.0118
smart_home        claude-sonnet-5           1     3050     120    2600   0.0034
```

Telegram zbirne brojke izlaže naredbom `/tokens` (i `/tokens reset`).

Trošak je izveden, ne izmjeren:
- broj tokena je točan;
- cijene su u tablici u istom modulu;
- čitanja iz predmemorije računaju se po `CACHE_READ_RATE` (0,1).

Dodatak za prvo pisanje u predmemoriju ne prati se zasebno, pa je brojka bliska
procjena, a ne račun.

---

## Limit

`DAILY_LLM_BUDGET_USD` ograničava procijenjenu dnevnu potrošnju
(`services/budget.py`, stanje u `data/llm_budget.json`). Postoji zbog incidenta:
lažni wake wordovi u jednom su danu potrošili ~6,9 milijuna tokena na pune
upite agentima
([detalji](inzenjerske-biljeske.md#lažna-buđenja-su-račun)).

---

## Kamo ide novac

Dva mjerenja vrijedi pamtiti:

| Slučaj | Tokeni | Trošak | Uzrok |
|--------|--------|--------|-------|
| Jedno duboko istraživanje, Opus | 1,18 M ulaza, 43 k iz predmemorije | 6,28 $ | petlja alata ponovno šalje skrejpane stranice |
| Jedan lažni wake word | ~56 k po upitu | nekoliko centi, puta broj nastavaka | puna orkestracija za šum |

Odluke o usmjeravanju u [arhitekturi](arhitektura.md#glasovni-usmjerivač) jednako
su odluke o trošku koliko i o brzini. Pitanje o vremenu ne košta ništa, opće
pitanje ide agentu od ~1.500 tokena, a samo stvarni zadaci stižu do orkestratora
od ~7.000 tokena.
