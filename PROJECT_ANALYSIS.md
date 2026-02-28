# IMPOSBRO Search – Analisi e miglioramenti

## Perché il progetto è interessante

- **Architettura federata reale**: non è un semplice wrapper su Typesense: sharding a livello documento, routing per campo, scatter-gather per la search e paginazione profonda corretta su più cluster.
- **Smart Producer**: la Query API decide il cluster di destinazione e lo mette nel messaggio Kafka; l’indexing service esegue senza ricalcolare il routing. Niente duplicazione di logica e comportamento coerente.
- **Stato HA**: la configurazione (cluster, routing) vive in un cluster Typesense interno (Raft), con sync tra istanze via Redis Pub/Sub. Adatto a deployment multi-istanza.
- **Stack moderno e separato**: FastAPI, Next.js 14 App Router, Kafka, Redis, Prometheus/Grafana, Helm. Buona base per estensioni (auth, più cluster, metriche custom).
- **Operatività**: docker-compose per sviluppo, Helm per Kubernetes, health con stato di Redis/Kafka, metriche Prometheus.

---

## Correzioni e miglioramenti applicati

### Correzioni

1. **README e Helm**: path aggiornati da `helm/imposbro-search` a `helm` (Chart e values in `helm/`).
2. **Settings**: `DEFAULT_DATA2_CLUSTER_*` resi opzionali (default `""`) per uso con un solo cluster.
3. **Federation**: risoluzione del cluster virtuale `"default"` verso un cluster dati reale (`default-data-cluster` o primo disponibile) in `get_client_for_document`, `get_clients_for_search` e `set_routing_rules`.
4. **Search/ingest**: in assenza di cluster valido l’API risponde 503 invece di pubblicare su Kafka (evita messaggi inutili e errori nel consumer).
5. **Indexing service**: ignorato il cluster virtuale `"default"` restituito da `/admin/federation/clusters` quando si costruiscono i client Typesense.
6. **Query API requirements**: rimosso `asyncio` (stdlib).
7. **Admin UI**: proxy `/api/*` verso la Query API tramite Route Handler (`app/api/[[...path]]/route.js`) invece del rewrite in middleware (non adatto a URL esterni).
8. **SyncConfigNotifier**: aggiunto `close()` e chiamata in shutdown per chiudere il client Redis.

### Miglioramenti

1. **Health**: `/health` ora espone `redis` e `kafka` con stato di connettività; `status` è `degraded` se non ci sono cluster o Redis non risponde.
2. **Helm**: aggiunto deployment per l’indexing-service (values + template).
3. **Test**: suite pytest per Query API (root, health, ingest senza `id`, ingest con documento valido), con lifespan di test (`TESTING=1`) e env minimo in `conftest`.
4. **Documentazione**: `.env.example` commentato; `PROJECT_ANALYSIS.md` (questo file).
5. **Root tooling**: `Makefile` e `package.json` alla root con target `test` / `npm run test` per eseguire i test Query API senza uscire dalla root; `.gitignore` esteso (pytest cache, coverage, OS); aggiunto `LICENSE` (MIT).
6. **Test aggiuntivi**: `test_search.py` (404 collection not found, 422 invalid collection name), `test_admin.py` (400 delete default cluster, GET clusters con default).
7. **README**: corretto nome variabile da `TYPESENSE_NODES` a `INTERNAL_STATE_NODES` nella sezione scaling HA.
8. **Pattern e best practice** (vedi anche `docs/PATTERNS_AND_PRACTICES.md`):
   - **Costanti**: modulo `constants.py` (versione, porta Typesense, pattern per nomi); niente numeri magici.
   - **CORS**: middleware CORS aggiunto solo se `CORS_ORIGINS` è impostato; origini esplicite per produzione.
   - **Validazione path**: `collection_name` e `cluster_name` validati con regex (`NAME_PATTERN`: alfanumerico, trattino, underscore).
   - **Sicurezza**: API key mascherate nella risposta di `GET /admin/federation/clusters` (solo ultimi 4 caratteri visibili).
   - **Federation**: in `load_from_state` la porta è normalizzata (int → string) per compatibilità Typesense.
   - **Admin UI**: client API gestisce risposte non-JSON (es. 502 dal proxy) senza crash.

---

## Come può essere migliorato (roadmap suggerita)

### Priorità alta

- **Autenticazione/authorization**: OAuth2/OIDC o API key per Admin e/o API pubbliche (README lo cita già).
- **Secret in produzione**: in Kubernetes usare Secret per API key e `REDIS_URL`; non lasciare chiavi in ConfigMap/values in chiaro.
- **Test di integrazione**: test che usano Kafka/Redis/Typesense (es. in Docker) per ingest + search end-to-end; da eseguire in CI opzionale.

### Priorità media

- **Fan-out documentale**: oggi il routing sceglie un solo cluster per documento; estendere per supportare più cluster per documento (replica su più regioni).
- **Collection aliases**: per reindici senza downtime (swap alias dopo reindex).
- **Paginazione a cursore**: per set molto grandi, oltre la “deep pagination” attuale.
- **Dashboard Admin UI**: metriche in tempo (queries/sec, latenza) via WebSocket o polling su `/metrics`/Prometheus.

### Priorità bassa

- **Grafana**: dashboard predefinite per metriche business (throughput, errori, latenza per cluster).
- **CORS**: configurazione esplicita in FastAPI se l’Admin UI viene servita da un dominio diverso.

---

## Come eseguire i test

```bash
# Query API (da query_api/)
pip install -r requirements-dev.txt
cd query_api
$env:TESTING="1"   # PowerShell
python -m pytest tests -v
```

---

## Documentazione per sviluppatori

- **[docs/PATTERNS_AND_PRACTICES.md](docs/PATTERNS_AND_PRACTICES.md)**: pattern architetturali, dependency injection, sicurezza (masking API key, validazione path, CORS), gestione errori e checklist per nuove modifiche.

## Riepilogo

Il progetto è solido e interessante per scenari di search federata e multi-cluster. Le modifiche introdotte allineano documentazione, configurazione, comportamento (cluster `default`, 503, proxy Admin UI) e qualità (test, health, shutdown). Sono state introdotte best practice su costanti, CORS, validazione input e sicurezza (API key mascherate). La roadmap sopra indica i passi successivi per produzione e funzionalità avanzate.
