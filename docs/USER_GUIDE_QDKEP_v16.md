# QDKEP v16.0 — Guidë Përdorimi Hap-pas-Hapi

> Nga nisja e platformës deri te **eksportimi i paketave të dokumentave** KB-RB,
> duke përfshirë tiparet e reja: **Signals Evidence** (F1), **Corpus Priors**
> (F2), **Case Prediction / Test Matrix** (F3) dhe **Generation Gate** (F4).
> Terminologjia teknike (SAP/CPI/BM25) mbahet në anglisht; shpjegimi në shqip.

---

## 0. Harta e platformës (6 tabs + cikli)

Dashboard-i hapet në `http://127.0.0.1:8765` me 6 tabs në anën e majtë:

| # | Tab | Roli |
|---|-----|------|
| 01 | **Monitoring** | Sinjalet live/local të CPI, PI/PO, S/4HANA, APIM, AEM; Error Corpus & MPL Archive; AEM Broker VPN Summary |
| 02 | **Scenario Lab** | Harvesting + **Signals Evidence** + **Case Prediction (Test Matrix)** — zona evidence-first |
| 03 | **Generation** | Prodhon `interface_inventory` docs (P3) dhe çiftet atomike **KB-RB** (evidence-gated) |
| 04 | **Benchmark** | Vlerëson cilësinë RAG (Policy/BM25/Chunking/Retrieval/Faithfulness/Citation) |
| 05 | **Feedback** | Corrective feedback + vendim promovimi + rigjenerim (modal same/new version) |
| 06 | **Packages** | Shfaq artefaktet (RB/KB/DOC) dhe **eksporton paketën si .zip** |

**Cikli evidence-first (i ri):**
```
Monitoring ─► Scenario Lab (Harvest → Signals Evidence → Case Prediction → [SAP CI manual] → evidencë reale)
          ─► Generation (KB-RB me evidencë të garantuar) ─► Benchmark ─► Feedback ─► Packages (.zip)
```

---

## 1. Përgatitja — `.env` (një herë)

Skedari: `platform_a/.env` (kopjo nga `.env.example`). Vendos vlerat reale të
lidhjeve (CPI/PI-PO/S4H/APIM/AEM). **Mos i publiko sekretet.**

**Flamujt kryesorë (ekzistues):**

| Variabël | Vlerë | Çfarë bën |
|---|---|---|
| `EXTERNAL_MONITORING_ENABLED` | `true` | Lejon marrjen live për APIM/AEM/S4H |
| `CPIVIEW_ENABLED` | `true` | Aktivon modelin e dizajnit CPIView (sender/receiver/adapter/mapping/router/scripts) |
| `CPIVIEW_DB` | `C:\CPIView\input\<tenant>\<tenant>.db` | Baza SQLite e CPIView |

**Flamujt e rinj (shto për tiparet e reja — opsionalë, default OFF):**

| Variabël | Rekomandim | Çfarë bën |
|---|---|---|
| `SUCCESS_BASELINE_ENABLED` | `1` | Merr 1 mesazh **COMPLETED** live si baseline suksesi për KB-RB |
| `CPIVIEW_XSD_DIR` | `C:\CPIView\xsd` | Dosje me XSD-të reale (`<iflow>.xsd`) → payload nga skema reale (F3) |
| `EVIDENCE_GATE_ENABLED` | `1` *(kur indeksi është i mbushur)* | **Gate:** gjeneron KB-RB vetëm kur iFlow-i ka ≥2 FAILED + ≥1 SUCCESS (F4) |
| `REGISTRY_DEDUPE_ENABLED` | `1` | Shmang ridokumentimin e të njëjtit pattern nëpër runs |
| `AEM_SEMP_DEBUG_DUMP` | `0` | `1` vetëm për test (shkruan `runs/aem_semp_debug.json`) |

> **Këshillë:** fillo me `EVIDENCE_GATE_ENABLED=0` derisa të mbushësh indeksin
> (hapi 2). Aktivoje `=1` kur do cilësi maksimale (çdo çift me evidencë të plotë).

---

## 2. Nisja e serverit

```bash
python -m orchestrator.server --runs-root runs --port 8765
```
Hape `http://127.0.0.1:8765` (server-i përpiqet ta hapë vetë).
> Pas çdo ndryshimi te `.env`, **ristarto serverin**.

---

## 3. Hapi 01 — Monitoring

1. Kliko **↻ Refresh sources** (lart-djathtas). Statusi bëhet `N sources connected`.
2. Lexo 4 kartat KPI: **Failed messages (24h)**, **Completed (24h)**, **Systems watched**, **Error patterns**. Dy pills-at (**Eligible for KB+RB**, **Duplicate in batch**) japin një pamje paraprake.
3. **Connected systems** — një rresht për sistem. Për **AEM-RCA** shfaqet edhe **Broker VPN Summary** (donut: spool / connections / endpoints / consumers + Status/Subscriptions/Messages Queued).
4. **Error corpus & MPL archive** (poshtë Connected systems):
   - **⤓ Sync archive** — tërheq FAILED-et live në arkivin mujor (`mpl_MM_YYYY.json`).
   - Zgjidh muajt në listë → **↺ Load & analyze** — rifreskon patterns nga runs + archive.

> Ky corpus është burimi që do e ushqejë evidencën në Scenario Lab (hapi 2b).

---

## 4. Hapi 02 — Scenario Lab (zona evidence-first)

Zgjidh **Source system** në panelin e majtë (p.sh. `CPI-RCA`).

### 4a. Pull material
- (Opsionale) vendos **Local export path** (një MPL/JSON) dhe/ose **CPIView SQLite**.
- Zgjidh **Harvest** mode (design/config/MPL/params/full) → kliko **Pull material**.
- Rezultat: KPI-t (Objects staged / Errors captured / New / Atomic pairs) dhe tabela **New errors** mbushen. Kjo mbush edhe **Signals Evidence** me FAILED+COMPLETED lokale.

### 4b. Harvest from corpus  *(F2 — priors + pasurim)*
- Kliko **⤓ Harvest from corpus**.
- Corpus-i (runs + MPL archive) kalon në kandidatë **dhe** në indeksin **Signals Evidence** si evidencë reale, me **priors** për iFlow (frekuencë taksonomish + prioritet).
- Nota tregon: `… records · … candidates · evidence +N (M iFlows prioritized)`.

### 4c. Signals Evidence  *(F1 — lexo gjendjen)*
Karta **Signals Evidence** me kaskadë `Package ▸ iFlow ▸ [Negative|Positive]`:
- iFlow-t renditen sipas **prioritetit** (prevalencë corpus), me badge `×N`.
- Pika e readiness: **jeshile** = *generation-ready* (≥2 FAILED & ≥1 SUCCESS), **portokalli** = ka ≥2 FAILED por s'ka sukses, **kuqe** = e pamjaftueshme.
- Kliko një iFlow → shiko samples (ts, MessageGuid, Sender→Receiver, taxonomy/error) + rreshtin **Corpus prior**.

> Qëllimi: të shohësh **para gjenerimit** cilët iFlow kanë evidencë të plotë.

### 4d. Case Prediction — Test Matrix  *(F3)*
Karta **Case Prediction — Test Matrix**:
1. Kliko **⚙ Predict test matrix**. Për çdo iFlow parashikohen:
   - **Negative cases** (≥5 kur dizajni e lejon): invalid_auth→401, wrong_endpoint→404, schema_violation (SAXParse), missing_field, script_exception, routing_no_match, backend 503, + `observed_pattern` nga corpus.
   - **Positive case**: payload valid → COMPLETED.
2. Tabela tregon `Neg`, `≥5 ✓`, dhe burimin e payload-it (**XSD** kur `CPIVIEW_XSD_DIR` është vendosur, ndryshe **template**).
3. Kliko një iFlow → paneli i djathtë tregon çdo case me **method · taxonomy · confidence · config delta · payload · how-to-run**.
4. Kliko **⤓ Export** → shkarkon `test_matrix_<system>.json`.

### 4e. Ekzekutimi manual në SAP CI  *(vendim: manual)*
- Merr `test_matrix_...json` dhe **ekzekuto rastet në SAP CI** (dërgo payload-et/config-et e parashikuara) për të provokuar **≥2 errore** + **≥1 sukses** për iFlow.
- Platforma **nuk** e prek sistemin live — ti e kryen ekzekutimin.
- Kur mesazhet reale hyjnë në MPL/arkiv, **rikthehu te 4a/4b** (Pull / Harvest from corpus) → Signals Evidence mbushet me evidencë reale dhe iFlow-i bëhet **generation-ready** (jeshil).

> Ky është "closed loop": parashiko → ekzekuto → mble­dh evidencë → gjenero.

---

### 4f. Si të mbushen 3 zonat e fundit të Scenario Lab

**Parakusht:** ristarto serverin (që F1–F4 të jenë aktive) dhe te `.env`:
`CPIVIEW_ENABLED=1` + `CPIVIEW_DB=…`; për sukses live `SUCCESS_BASELINE_ENABLED=1`;
për payload XSD `CPIVIEW_XSD_DIR=…`.

**① ATOMIC KB-RB CANDIDATES** — *"No candidates queued yet"*
- Mbushet **automatikisht** nga harvest-i: një kandidat për çdo **signaturë të re first-seen**.
- Veprimi: kliko **Pull material** (me një burim që ka FAILED — Local export ose CPIView) **ose** **⤓ Harvest from corpus**.
- Nëse "New errors" është plot por candidates bosh → signaturат janë tashmë të dokumentuara (`status ≠ new`); bëj **Harvest from corpus** (i annoton me flag `new`/`documented`) ose harvest një burim me pattern-e të reja.

**② SIGNALS EVIDENCE** — *"No evidence yet — capture FAILED + COMPLETED"*
- Kërkon sample **FAILED dhe COMPLETED** për iFlow.
- Veprimi A: **Pull material** me një **Local export path** që përmban rreshta me `Status: FAILED` **dhe** `Status: COMPLETED` (jo vetëm FAILED).
- Veprimi B: **Harvest from corpus** — mbush indeksin me FAILED reale nga runs+archive **+ priors/prioritet**. (Arkivi është FAILED-centric → positives vijnë nga export me COMPLETED ose `SUCCESS_BASELINE_ENABLED=1`.)
- Rezultati: drill-down `Package ▸ iFlow ▸ [Negative|Positive]`; pika bëhet **jeshile** kur ≥2 FAILED & ≥1 SUCCESS.

**③ CASE PREDICTION — TEST MATRIX** — *"No predictions yet"*
- Kërkon që **Zona ② të jetë mbushur** (parashikimi bëhet për iFlow-t në indeksin Evidence) + dizajn CPIView për ≥5 negative.
- Veprimi: pas ②, kliko **⚙ Predict test matrix**. Për ≥5 negative & payload nga skema reale: `CPIVIEW_ENABLED=1` + `CPIVIEW_DB` + `CPIVIEW_XSD_DIR`.
- Rezultati: një rresht për iFlow (Neg · ≥5 · XSD|template) → kliko për detaje → **⤓ Export** për SAP CI.

**Rrjedha e varësisë:**
```
Pull material / Harvest from corpus  ─►  ① Candidates  +  ② Evidence
                                                         └►  ⚙ Predict  ─►  ③ Test Matrix
```

## 5. Hapi 03a — Interface Inventory (shtresa themelore)

Në tab-in **Generation**:
1. Kliko **Generate interface_inventory_docs**.
2. Prodhohet 1 dokument `DOC_...` (P3) për çdo iFlow: Seksioni 4 = objektet e dizajnit nga CPIView; Seksioni 5 = evidenca FAILED+SUCCESS nga MPL. Ruhen si run i pavarur **INV-NNN** (`inventory_runs/`).
3. Këto docs janë baza që KB-RB do t'i citojnë (cross-reference).

> Për evidencë të pasur në INV, mund të vendosësh `INV_LOCAL_MPL` te një dosje me arkivat MPL `.json`.

---

## 6. Hapi 03b — Generation (çiftet atomike KB-RB, evidence-gated F4)

Ende në **Generation**:
1. Zgjidh **burimin** (gen-source: CPI incl. APIM/AEM, PIPO, ose ALL).
2. (Opsionale) zgjidh **AI enrich** (pasuron vetëm seksionet diagnostike; default OFF).
3. Kliko **Generate atomic KB + RB pairs**.
4. **Me `EVIDENCE_GATE_ENABLED=1`:** gjenerohen vetëm iFlow-t **generation-ready** (≥2 FAILED + ≥1 SUCCESS nga indeksi Evidence); të tjerët mbahen me arsye `INSUFFICIENT_EVIDENCE` (shihet te rezultatet). Seksioni 4 (MONITORING AND MPL EVIDENCE) i çdo çifti mbushet me **2 FAILED + 1 SUCCESS reale nga indeksi**.
5. Rezultati: çifte `KB_...` + `RB_...` (një error → një çift, i njëjti numër ID) nën `runs/RUN-<seq>-<date>/knowledge/{KB,RB}`.

> Rregulla e shenjtë e strukturës respektohet (RB: 13 seksione me ROLLBACK; KB: 6. VALIDATION / POST-FIX VALIDATION). Emrat e seksioneve janë "sacred".

---

## 7. Hapi 04 — Benchmark

Në tab-in **Benchmark**:
1. Kliko **Run benchmark** (ose **Benchmark latest INV** për INV-docs).
2. Lexo **Overall readiness** + kategoritë: Policy compliance, BM25 readiness, Chunking, Retrieval precision/recall, Faithfulness, Citation accuracy.
3. Për `interface_inventory` shfaqet **profili i adaptuar** (Faithfulness = N/A).
4. Statuset: 90-100 *Enterprise Ready*, 80-89 *Ready w/ minor*, 70-79 *Conditional*, 60-69 *Needs remediation*, <60 *Not ready*.

> Me ciklin evidence-first të mbushur, prit ngritje te **Citation / Faithfulness / BM25** (evidencë literale, kontrastive, e garantuar).

---

## 8. Hapi 05 — Feedback

Në tab-in **Feedback**:
1. Shiko **Promotion decision** dhe dokumentet nën threshold me shkaqe + veprime.
2. **Apply corrective feedback** (një dok ose grup): shfaqet **modal-i i versionit**:
   - **Same version (Remediated)** — mban versionin, rishkruan variant të korrigjuar.
   - **New version (Update v++)** — bump v1→v2, ruan versionin e vjetër.
3. Feedback-u prodhon corrective JSON; **rigjenerimi i kontrolluar** bëhet në gjenerator (nën miratimin tënd) — benchmark-u nuk i rishkruan drejtpërdrejt dokumentet.

---

## 9. Hapi 06 — Packages (eksportimi i paketës)

Në tab-in **Packages**:
1. Filtro sipas tipit me **pkg-type-filter**: *All / RB — runbook / KB — kedb / DOC — interface_inventory*.
2. Shiko artefaktet në `knowledge/RB`, `knowledge/KB`, `knowledge/DOC`.
3. (Opsionale) **Regenerate the Group** — aplikon feedback te çdo dok nën threshold, pastaj Regeneration → Benchmark → Feedback.
4. **⤓ Download .zip** — **eksporton paketën** e dokumentave të aprovuara për ingestion në sistemin AI4BIZ RAG.

> Kjo është pika fundore: paketa `.zip` përmban KB-RB (dhe/ose DOC) e gatshme.

---

## 10. Shtojcë A — Cikli i plotë (checklist i shpejtë)

1. `.env` konfiguruar + server i nisur (`:8765`).
2. **Monitoring** → Refresh sources → Sync archive.
3. **Scenario Lab** → Pull material → **Harvest from corpus** (mbush evidence + priors).
4. **Signals Evidence** → identifiko iFlow-t jo-ready.
5. **Case Prediction** → Predict → **Export** → ekzekuto manualisht në **SAP CI**.
6. Rikthe evidencën (Pull/Harvest) → iFlow-t bëhen **generation-ready**.
7. **Generation** → interface_inventory_docs → **Generate KB-RB pairs** (gate ON).
8. **Benchmark** → **Feedback** (version modal) → **Packages → Download .zip**.

## 11. Shtojcë B — Komanda CLI & mirëmbajtje

```bash
# Rifresko interface inventory (i planifikueshëm me systemd/cron)
python -m orchestrator.cli --runs-root runs inventory-refresh --sap-sid CPI --benchmark

# Pas ndryshimeve strukturore te gjeneratori — rigjenero golden fixtures + testo
python -m scripts.regenerate_golden
pytest -q
```

## 12. Shtojcë C — Zgjidhje problemesh

| Simptomë | Shkak i mundshëm | Zgjidhje |
|---|---|---|
| `0 sources connected` | `.env` bosh/gabim ose host jashtë rrjetit | Plotëso `.env`, kontrollo VPN/Cloud Connector |
| Signals Evidence bosh | S'ke bërë Pull/Harvest | Bëj **Pull material** dhe **Harvest from corpus** |
| iFlow gjithnjë kuqe (jo-ready) | S'ka ende 2 FAILED / 1 SUCCESS | Ekzekuto Test Matrix në SAP CI, rikthe evidencën |
| Payload = "template" jo XSD | Mungon `CPIVIEW_XSD_DIR` | Vendos dosjen me `<iflow>.xsd` te `.env` |
| Shumë docs mbahen `INSUFFICIENT_EVIDENCE` | Gate ON por indeksi bosh | Mbush indeksin ose vër `EVIDENCE_GATE_ENABLED=0` |
| AEM dump s'shkruhet | Koment inline pas vlerës | `AEM_SEMP_DEBUG_DUMP=1` në rresht të vet |

---

*Statusi i tipareve: F1 Signals Evidence · F2 Corpus Priors · F3 Case Prediction ·
F4 Generation Gate — të gjitha aktive. SAP CI ekzekutohet manualisht.*
