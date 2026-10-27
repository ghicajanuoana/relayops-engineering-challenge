# RelayOps — Engineering Challenge

> Implemented startup, upgrade, secrets, health checks, and CI/test procedures:
> [Operations guide](docs/OPERATIONS.md). Run `python scripts/configure.py` before
> the first `docker compose up --build` to create private local configuration.

**Senior · Software Engineering · Networking · DevOps · AI**

| Software | Networking | DevOps | Timp recomandat | Livrare |
|----------|------------|--------|-----------------|---------|
| 50% | 25% | 25% | 6–8 ore | GitHub fork |

## Context

Ai preluat un sistem intern care procesează evenimente și le livrează către alte servicii prin HTTP webhooks.

Sistemul a fost lăsat într-o stare problematică: anumite componente nu comunică, există buguri în aplicație, iar infrastructura necesită îmbunătățiri.

**Obiectivul tău:** investighează, repară și extinde sistemul astfel încât să funcționeze corect și fiabil.

### Arhitectura inițială

```
             Client
                |
                v
          Nginx Gateway        infrastructure/nginx.conf
                |
                v
           FastAPI API         api/
                |
                v
           PostgreSQL          db/ (schema)
                ^
                |
          Python Worker        worker/
                |
                v
         Mock Receiver         receiver/
       (HTTP Webhooks)
```

### Structura repository-ului

```
api/                   FastAPI — primește și expune evenimente
worker/                livrează evenimentele prin HTTP webhooks
receiver/              server HTTP mock (receptorul webhook-urilor)
db/                    schema PostgreSQL
infrastructure/        configurația Nginx
tests/                 teste inițiale
.github/workflows/     CI
candidate/             CV-ul tău
docker-compose.yml
.env.example
SOLUTION.md            de completat de tine
```

### Pornire

```bash
docker compose up --build
```

Gateway-ul ascultă pe `http://localhost:8080`. Receptorul mock este expus pe `http://localhost:9000` pentru inspecție.

```bash
pip install -r tests/requirements.txt
pytest tests
```

---

## Contractul API

Contractul de mai jos este cel verificat de testele automate. Implementarea internă o poți schimba oricum.

### `POST /events`

```json
{
  "event_id": "evt-001",
  "type": "invoice.created",
  "payload": {
    "invoice_id": 1234,
    "amount": 250
  }
}
```

| Situație | Răspuns |
|----------|---------|
| Eveniment nou | `202 Accepted` — `{"event_id": "...", "status": "pending"}` |
| Același `event_id`, același conținut (`type` + `payload`) | `200 OK` — starea curentă a evenimentului existent, fără o nouă procesare |
| Același `event_id`, conținut diferit | `409 Conflict` |
| Body invalid | `422 Unprocessable Entity` |

Payload-uri de până la **1 MB** trebuie acceptate prin gateway.

### `GET /events/{event_id}`

`404` dacă evenimentul nu există, altfel `200`:

```json
{
  "event_id": "evt-001",
  "type": "invoice.created",
  "payload": {"invoice_id": 1234, "amount": 250},
  "status": "delivered",
  "attempts": 2,
  "delivery_attempts": [
    {"attempt": 1, "status_code": 503, "error": null, "created_at": "..."},
    {"attempt": 2, "status_code": 200, "error": null, "created_at": "..."}
  ],
  "created_at": "...",
  "updated_at": "..."
}
```

`status` este unul dintre: `pending`, `processing`, `delivered`, `failed`.
Pentru erori de rețea (timeout, conexiune refuzată), `status_code` este `null` și `error` descrie problema.

### `GET /health`

`200` când serviciul funcționează.

### `GET /metrics`

Format Prometheus text. Minimum:

```
relayops_events{status="pending"} 3
relayops_events{status="processing"} 0
relayops_events{status="delivered"} 120
relayops_events{status="failed"} 2
relayops_delivery_attempts_total 131
```

### Request ID

Fiecare răspuns care trece prin gateway are header-ul `X-Request-ID`. Dacă clientul trimite deja un `X-Request-ID`, același ID este propagat până în API, returnat în răspuns și apare în loguri.

## Contractul de livrare

Worker-ul trimite `POST` către `RECEIVER_URL` cu body-ul `{"event_id", "type", "payload"}`.

- **Semnătură:** fiecare cerere are header-ul `X-RelayOps-Signature: sha256=<hex>`, unde `<hex>` este HMAC-SHA256 cu cheia `WEBHOOK_SECRET`, calculat peste **body-ul brut trimis**. Receptorul respinge cu `401` semnăturile invalide.
- **Succes:** orice răspuns `2xx`. Un eveniment este `delivered` doar după un răspuns `2xx`.
- **Erori temporare:** erori de rețea, timeout (**5 secunde** per încercare), `429`, `5xx` → retry cu backoff exponențial. Dacă răspunsul conține `Retry-After` (secunde), următoarea încercare nu are loc mai devreme.
- **Erori permanente:** orice alt răspuns (ex. `400`, `401`) → `failed`, fără retry.
- **Maximum 3 încercări** per eveniment, apoi `failed`.
- **Concurență:** worker-ul rulează în **minimum 2 instanțe**. Un eveniment nu este livrat de două ori în funcționare normală.
- **Garanție:** at-least-once. Dacă un worker moare în timpul unei livrări, evenimentul este reluat (un duplicat e acceptabil în acest caz, pierderea evenimentului nu).
- **Oprire:** la `SIGTERM`, worker-ul termină livrarea curentă și se oprește în câteva secunde, fără să lase evenimente blocate.

### Receptorul mock (`receiver/`)

Simulează serviciul destinatar. **Nu modifica contractul lui** — testele automate îl folosesc.

| Endpoint | Descriere |
|----------|-----------|
| `POST /webhook` | primește un eveniment (`event_id`, `type`, `payload`) |
| `GET /received` | lista livrărilor primite, în ordinea sosirii |
| `DELETE /received` | golește lista și resetează comportamentul |
| `POST /control` | ex. `{"fail_next": 2, "status_code": 503, "retry_after": 0, "delay_ms": 0, "delay_next": 0}` — vezi `receiver/receiver.py` |

---

## Task 1 — Software Engineering (50 puncte)

Implementează și corectează procesarea evenimentelor.

**Cerințe:**

- `POST /events` — primește și salvează un eveniment.
- `GET /events/{id}` — returnează starea și istoricul încercărilor de livrare.
- Worker-ul livrează evenimentele prin HTTP către serviciul receptor configurat.
- Implementează retry pentru erori temporare (maximum 3 încercări).
- Asigură idempotency: același ID și același conținut nu produc procesări duplicate; același ID cu alt conținut returnează `409 Conflict`.
- Adaugă teste pentru scenariile importante.

Sistemul trebuie să poată recupera evenimentele nelivrate după repornirea worker-ului.

Respectă **contractul de livrare** de mai sus (semnătură, timeout, `Retry-After`, mai multe instanțe de worker, oprire grațioasă), `GET /metrics` și propagarea `X-Request-ID`.

## Task 2 — Networking (25 puncte)

Există probleme introduse intenționat în configurația rețelei.

**Cerințe:**

- Identifică și repară problemele de comunicare dintre servicii.
- Repară accesul HTTP prin Nginx.
- Verifică DNS-ul și conectivitatea între containere.
- Asigură izolarea bazei de date față de accesul extern.
- Documentează diagnosticul și soluțiile aplicate.

Vrem să înțelegem cum ai identificat cauza, nu doar ce configurație ai modificat.

## Task 3 — DevOps & Infrastructure (25 puncte)

Pregătește sistemul pentru a putea fi rulat și verificat în mod reproductibil.

**Cerințe:**

- Sistemul pornește cu `docker compose up --build`.
- Adaugă health checks relevante.
- Configurează variabilele de mediu fără secrete hardcodate.
- Adaugă un pipeline GitHub Actions pentru teste.
- Asigură logging util pentru diagnosticare.
- Documentează procedura de pornire și verificare.

Nu este necesar un deployment într-un cloud real.

---

## Reguli

- Poți utiliza **orice instrument AI**: Claude Code, Codex, Cursor etc.
- Poți modifica orice componentă, păstrând contractele API și cerințele funcționale.
- Poți adăuga biblioteci și instrumente dacă poți justifica alegerea.
- Nu este necesar să finalizezi toate cerințele; soluțiile parțiale sunt acceptate.
- Nu trebuie să plătești pentru servicii cloud sau API-uri AI.

## Cum trimiți soluția

1. Fă fork repository-ului.
2. Implementează soluția în fork-ul tău.
3. Completează fișierul `SOLUTION.md`, explicând:
   - Ce probleme ai identificat și cum le-ai rezolvat.
   - Ce decizii tehnice ai luat.
   - Ce instrumente AI ai utilizat și cum ai verificat rezultatele.
   - Ce ai îmbunătăți dacă ai avea mai mult timp.
4. Include CV-ul în `candidate/CV.pdf`.
5. Trimite linkul către fork și commit-ul final.

> **Atenție:** dacă fork-ul este public, CV-ul va fi public. Elimină din acesta datele personale pe care nu dorești să le publici.

## Evaluare

| Categorie | Punctaj |
|-----------|---------|
| Software Engineering | 50 |
| Networking | 25 |
| DevOps | 25 |
| **Total** | **100** |

Soluțiile vor fi evaluate prin teste automate și analiză tehnică. Pe lângă testele din acest repository, există o suită suplimentară de teste rulată de noi, care folosește contractul API de mai sus și pornește sistemul cu `docker compose up --build`.

Candidații selectați vor participa la o discuție în care vor explica propria implementare și vor rezolva un scenariu suplimentar de debugging.

**Timp recomandat: 6–8 ore.** Nu ne așteptăm ca toată lumea să termine tot — prioritizează și explică ce ai lăsat deoparte.
