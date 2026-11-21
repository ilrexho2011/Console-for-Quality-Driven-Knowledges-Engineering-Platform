# Knowledge Registry — Documented Error Registry

Regjistër i qëndrueshëm i **informacionit themelor të errorëve** që janë kthyer (ose po kthehen) në çifte atomike **KB-RB**. Plotëson JSON-et ekzistuese — **nuk i zëvendëson**: JSON-i burimor ruhet i paprekur në `raw_payload` / `design_payload`. Fushat që s'rikuperohen dot lihen **NULL** (asgjë nuk trillohet).

Skedarët:

- `postgresql/V001__documented_error_registry.sql` — dialekti primar (PostgreSQL 13+).
- `mssql/V001__documented_error_registry.sql` — varianti ekuivalent T-SQL (SQL Server 2017+).

Skema është **e veçantë** (`knowledge_registry`), e ndarë nga `scenario_lab` (i cili është *staging para gjenerimit*). Ky regjistër mban errorët **e dokumentuar tashmë** ose ata që **prodhojnë një çift të ri**.

---

## 1. Vlera e fushave (tabela `documented_error`)

### Provenienca / audit

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `error_uid` | Çelës surrogate (UUID). Identitet i brendshëm, i pavarur nga MessageGuid (që mund të mungojë). |
| `source_of_record` | Nga erdhi rreshti: `runs_backfill` (nga dokument ekzistues RUN-NNN), `mpl_live` (MPL live), `cpiview` (modeli CPIView), `mixed` (runtime + design join). Bën transparente sa i besueshëm është rreshti. |
| `run_id` | Dosja `RUN-NNN-YYYYMMDD` që mban/prodhoi çiftin KB-RB. |
| `registered_at` / `updated_at` | Kur u shkrua/rifreskua rreshti (audit). |

### Konteksti i sistemit (identifikues literalë)

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `system_kind` | Njëri nga 5 ambientet: `CPI`, `PIPO`, `S4`, `APIM`, `AEM`. Përcakton interpretimin e fushave. |
| `customer_id`, `system_id`, `sap_sid` | Identiteti i tenant-it (p.sh. `RCMO`, `CPI-RCMO`, `CPI`). Ruhet literal. |
| `integration_flow_name` | `IntegrationFlowName` i MPL-së (literal). |
| `integration_flow_id` | Id-ja e iFlow-t në CPI (literal, join-key me inventarin/CPIView). |
| `interface_id`, `package_name` | Interface/package ku ndodh errori. |

### Fushat themelore të kërkuara (zemra e regjistrit)

| Fusha | Burimi tipik | Kur mbetet bosh |
|-------|--------------|-----------------|
| `message_guid` | MPL `MessageGuid` / rreshti *Evidence* në dokument | Kur dokumenti/MPL s'e mban |
| `correlation_id` | MPL `CorrelationId` | Shpesh bosh nga backfill |
| `log_start` | MPL `LogStart` / timestamp-i i evidencës | — |
| `log_end` | MPL `LogEnd` | Shpesh bosh (dokumenti mban vetëm një timestamp) |
| `sender` | CPIView `bpmn_participant` / MPL header | `n/a` në dokument → bosh; reale nga CPIView |
| `receiver` | CPIView `bpmn_participant` / MPL header | si te `sender` |
| `status` | MPL `Status` (`FAILED`) | Nga backfill nënkuptohet `FAILED` |
| `error_message` | MPL `ErrorText` / rreshti *Error:* në dokument | — |

### Evidencë mbështetëse & klasifikim

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `error_phrase` | Fraza e shkurtër kanonike (BM25-friendly), p.sh. `SAP CPI HTTP Receiver - 406`. |
| `exception_class` | Klasa e përjashtimit, p.sh. `HttpResponseException`. |
| `http_status_code` | Kodi HTTP (404/502…) kur ka; CHECK 100–599. |
| `adapter_type` | Tipi i adapter-it (HTTP/SFTP/SOAP/IDoc), nga design. |
| `communication_channel` | Emri i kanalit (nga CPIView/ESR-DIR). |
| `object_name` | `object_name` i normalizuar, p.sh. `CPI_HTTP_404`. |
| `taxonomy` | Taksonomia `DOMAIN \| CATEGORY \| SUB \| CODE`. |
| `priority` | `P1..P4` (siç del në emrin e file-t KB-RB). |

### Lidhja me çiftin atomik KB-RB

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `kedb_id`, `runbook_id` | ID-të e çiftit, p.sh. `KEDB-P3-0237` / `RB-P3-0237` (i njëjti numër — rregulli i shenjtë). |
| `pair_number` | Komponenti numerik i përbashkët (237). |
| `kb_filename`, `rb_filename` | Emrat e file-ve `.md` në `knowledge/{KB,RB}`. |
| `document_version` | `v1` / `v2` … |

### Ruajtja e papërpunuar (JSON mbahet)

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `raw_payload` (JSONB) | Record-i origjinal MPL/log, **verbatim** — kërkesa jote që JSON të ruhet gjithashtu. |
| `design_payload` (JSONB) | Konteksti design nga CPIView/ESR-DIR: channels, scripts, mappings, routers. |

### Idempotencë

| Fusha | Vlera / kuptimi |
|-------|-----------------|
| `occurred_at` | Koha e normalizuar e ndodhjes (zakonisht = `log_start`). |
| `content_hash` | `sha256` i identitetit+evidencës kanonike. **UNIQUE** → i njëjti error nuk regjistrohet dy herë (p.sh. re-run ose re-scan). |

Dedupe: `UNIQUE(content_hash)` gjithmonë; plus indeks unik i pjesshëm `UNIQUE(system_id, message_guid)` kur MessageGuid ekziston (guid-et bosh nga backfill nuk përplasen).

---

## 2. Tabela opsionale `documented_error_design_element`

Zbërthim i normalizuar i dizajnit pas errorit — një rresht për: `sender`, `receiver`, `channel`, `adapter`, `message_mapping`, `router` (me condition), `script` (emri Groovy/JS), `content_modifier`. Lejon query të tipit *"cilët errore kalojnë nga script X / mapping Y / router condition Z"* pa hapur `design_payload`. Mbushet nga CPIView ose `.iflw`/ESR-DIR kur disponohet; ndryshe mbetet bosh.

---

## 3. Si mbushet (dy rrugë)

**A) Backfill nga `runs/`** — parse i dokumenteve `.md`:
- Nga blloku i metadata-s: `object_name`, `integration_flow_id/name`, `interface_id`, `package_name`, `priority`, `system_id`, `customer_id`, `sap_sid`, `version`, `KEDB/RB ID`.
- Nga rreshti *Evidence* (seksioni CONFIRMING EVIDENCE / MPL EVIDENCE): `LogStart`, `MessageGuid`, `Interface`, `Sender`, `Receiver`, `Error` (→ `error_message`).
- `status` → `FAILED` (nënkuptuar); `source_of_record='runs_backfill'`.
- `Sender/Receiver = n/a` → NULL; `LogEnd` zakonisht NULL.

**B) Forward nga pipeline-i** — kur monitor/generate dokumenton një error të ri (MPL/CPIView JSON), i njëjti record shkruhet këtu me `source_of_record='mpl_live'` ose `'mixed'`, me `raw_payload` = record-i MPL dhe `design_payload` = konteksti CPIView. Vendi natyror: një hap i vogël *register* brenda `generate_handler` (ose stage-i i propozuar `enrich`), pasi ndërtohet `ErrorRecord` dhe alokohet `pair_number`.

Të dyja rrugët respektojnë idempotencën përmes `content_hash`, kështu që backfill + forward nuk krijojnë dublikatë.

---

## 4. Query-t e gatshme

Në fund të secilit skedar `.sql` ka shembuj: regjistri me 7 fushat themelore, errorët per iFlow me çiftin e tyre, errorët per Groovy script, dhe një query *coverage* që tregon sa % e fushave u rikuperuan (`have_guid`, `have_sender`, `have_log_end` …) — e dobishme për të matur cilësinë e backfill-it.
