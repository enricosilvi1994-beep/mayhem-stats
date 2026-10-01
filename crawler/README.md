# Statistiche ARAM: Mayhem (crawler)

Scarica partite ARAM: Mayhem dalla Riot Match-V5 API, conta vittorie e partite per
campione, oggetto e augment, e pubblica `docs/stats.json`. L'app Metasrc lo legge
nelle modalità **Nostre** e **Unite**.

La chiave Riot resta nei *Secrets* di GitHub e non entra né nell'app né nel JSON.
Il JSON pubblico contiene solo conteggi aggregati, senza PUUID né nomi di giocatori.

## Configurazione (una volta, ~10 minuti)

1. **Chiave Riot**: accedi a <https://developer.riotgames.com> e copia la *Development API Key*.
   - Dura 24 ore e va rigenerata ogni giorno. Per non doverlo fare, chiedi una *Personal API Key*
     (gratuita, non scade) dalla stessa pagina: "Register Product" → "Personal".
2. **Nuovo repository GitHub pubblico**, es. `mayhem-stats`, e carica il contenuto di questa
   cartella `crawler/` nella radice (deve esserci `.github/workflows/mayhem-stats.yml`).
3. **Secret**: Settings → Secrets and variables → Actions → *New repository secret*
   - Nome `RIOT_API_KEY`, valore la chiave.
   - (opzionale) tab *Variables*: `RIOT_PLATFORM` (default `euw1`), `MAX_MATCHES` (default `1500`).
4. **GitHub Pages**: Settings → Pages → *Deploy from a branch* → branch `main`, cartella `/docs`.
5. **Prima esecuzione**: tab Actions → "Statistiche ARAM Mayhem" → *Run workflow*.
   Con una chiave development servono ~30–40 minuti per 1500 partite (limite: 100 richieste ogni 2 minuti).
6. **Collega l'app**: in `OwnStatsSource.kt` imposta

   ```kotlin
   const val STATS_URL = "https://<tuo-utente>.github.io/mayhem-stats/stats.json"
   ```

   Ricompila: "Nostre" e "Unite" si attivano.

Poi il workflow gira da solo ogni giorno alle 04:17 UTC. Le partite si accumulano in `data/state.json`
(per patch: quando esce una patch nuova le statistiche ripartono su quella).

## Prova in locale

```bash
python -m unittest -v test_mayhem_crawler          # test, nessuna rete
RIOT_API_KEY=RGAPI-... python mayhem_crawler.py --max-matches 200
```

## Come vengono calcolati i numeri

- **Partite valide**: solo coda Mayhem (`2400`), senza remake (< 5 minuti o resa anticipata).
  Se a fine esecuzione compare "partite scartate per coda diversa", la coda Mayhem ha cambiato numero:
  si passa con `--queue`.
- **Oggetti**: inventario finale (`item0…item5`), solo oggetti finiti (niente componenti né consumabili);
  ogni oggetto conta una volta per partita.
- **Augment**: `playerAugment1…6`, solo quelli Mayhem (`ARAM_…` su CommunityDragon).
- **Classifiche** (le calcola l'app): limite inferiore di Wilson al 95%, minimo 30 partite.
  Così 3 vittorie su 3 non battono 5.400 su 10.000.
- **Limite noto**: Match-V5 non dice cosa hai comprato all'inizio (servirebbe la *timeline*, una
  richiesta in più per partita), quindi in modalità Nostre gli oggetti iniziali non ci sono.
