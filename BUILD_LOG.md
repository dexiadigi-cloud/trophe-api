# Trophe — BUILD_LOG.md
Append-only. Only verified facts: real command output, real row counts, real curl transcripts.

---
## 2026-09-24 19:45 HST — project kickoff (coordinator)
- Environment: Python 3.12.3, sqlite 3.45.1 with FTS5 enabled, 84 GB free in ~.
- Created venv at ~/workspace/trophe/.venv with fastapi 0.141.1 + uvicorn.
- Copied auth pattern from ~/workspace/scripture-desk/api/app.py: APIKeyHeader(name="X-API-Key"),
  key from env var, hmac.compare_digest, HTTP 401 on failure.
- PLAN.md written with DB schema, endpoints, milestones M1/M2/M3.

## 2026-09-24 19:50 HST — M1 delegated
- Dispatched ETL subagent: USDA FDC bulk download + scripts/build_db.py + data/trophe.db.
- Waiting on handoff: exact per-type counts, DB size, sanity spot-checks.

---
## 2026-09-24 20:05 HST — M1 DONE: DB built (ETL subagent)

### Sources (all CC0 1.0, from https://fdc.nal.usda.gov/download-datasets.html)
Latest CSV zips as listed on the page (verified from the page's own "Latest Downloads" table):
- `FoodData_Central_sr_legacy_food_csv_2018-04.zip` — 6,074,592 B (zip), ~54 MB unzipped
- `FoodData_Central_foundation_food_csv_2026-04-30.zip` — 3,825,741 B (zip), ~29 MB unzipped
- `FoodData_Central_survey_food_csv_2024-10-31.zip` — 3,325,692 B (zip), ~1.6 GB claimed; measured foods+nutrients ~20 MB unzipped
- `FoodData_Central_branded_food_csv_2026-04-30.zip` — 448,767,220 B (zip); branded_food.csv 954 MB + food_nutrient.csv 1.50 GB unzipped
- Total download: ~462 MB. Stored in `data/raw/`.

### ETL: `scripts/build_db.py`
- Reproducible rebuild: `python3 scripts/build_db.py` deletes `data/trophe.db` and rebuilds from the zips.
- Streaming: every CSV is parsed straight out of the zip via zipfile+TextIOWrapper; rows inserted in 20k batches with PRAGMA journal_mode=OFF/synchronous=OFF. Full build 284.5s (first run crashed at food_portions on a foundation CSV defect, fixed + rebuilt — see below).
- Nutrients: union of all four nutrient.csv catalogs = 477 rows (ids 1001-2069 range, not contiguous).
- Branded nutrient rows are stored per 100g (verified against field docs: "Amount of the nutrient per 100g of food" + spot-check: Wesson vegetable oil fdc 1105904 = 867 kcal, 93.33g fat per 100g).

### Verified DB facts (all from sqlite3 against data/trophe.db, 1.77 GB)
- `SELECT data_type, COUNT(*) FROM foods GROUP BY 1`:
  - branded: 1,999,950 | foundation: 469 | sr_legacy: 7,793 | survey: 5,432 → total 2,013,644
- `SELECT COUNT(*) FROM foods_fts` = 2,013,644 (matches foods exactly)
- food_nutrients = 27,046,002 rows (sr 644,125 + foundation 21,426 + survey 353,015 + branded 26,027,437)
- nutrients = 477 | daily_values = 36 | food_portions = 36,682 (sr 14,449, survey 22,046, foundation 187)

### Sanity spot-checks (DB, not memory)
- Egg, whole, raw, fresh (fdc 171287, sr_legacy): 143 kcal/100g; portions include "large" = 50.0g
- Chicken, broiler or fryers, breast, skinless, boneless, meat only, raw (fdc 171077): 120 kcal/100g
- Rice, white, long-grain, regular, raw, unenriched (fdc 169756): 365 kcal/100g
- Branded: Coca-Cola fdc 2678649 (Coca-Cola USA Operations) = 39 kcal/100g

### Decisions / deviations (measured, not guessed)
1. Foundation scope: only `foundation_food` data_type rows (469). `sample_food` (4,079) carry ZERO nutrient rows in this release; `sub_sample_food` (75,055), `market_acquisition` (7,577), `agricultural_acquisition` (810) are sampling-collection records, not published food profiles.
2. Branded scope: ALL 1,999,950 foods + ALL 26,027,437 nutrient rows (full fidelity). Measured: median 14 nutrients/food; the FDA DV-core set already covers 97.5% of branded rows, so the plan's "top ~30 nutrients per food" cap was a no-op on this release — keeping all 110 nutrients costs only 2.5% more rows.
3. Survey (FNDDS) quirk: food_nutrient.nutrient_id references nutrient NUMBER (nutrient_nbr), not nutrient.id (65/65 distinct values match nutrient_nbr, 0/65 match nutrient.id). ETL remaps through the catalog.
4. Dropped rows (documented): 149,010 food_nutrient rows referencing non-food sampling records (all foundation); 33 foundation rows for nutrient id 2066 (empty amounts, 2066 exists in NO dataset's nutrient.csv); 273 foundation food_portion.csv rows with empty fdc_id (orphans); the one true duplicate (fdc 2768188 + nutrient 1106, identical twice) collapsed by INSERT OR REPLACE → 27,046,002 final rows from 27,046,003 inserts.
5. Schema deviation (additive only): added `idx_fp_food` on food_portions(food_fdc_id) alongside PLAN's idx_fn_food. All PLAN tables/columns/FTS5 (porter unicode61) otherwise exactly as specified.
6. daily_values: 36 FDA 21 CFR 101.9 DVs (adults/children >= 4), resolved by nutrient_nbr from the catalog itself (e.g. Energy 208→id 1008 = 2000 kcal; Vit A RAE 320→id 1106 = 900 mcg; folate 417→id 1177 = 400 mcg; choline 421→id 1180 = 550 mg; added sugars 539→id 1235 = 50 g).

## M2 — API running locally (verified 2026-09-25)

### Implementation: `api/app.py` (FastAPI, single file, ~700 lines)
- Routes: `GET /health` (no auth) + 6 authed endpoints exactly per PLAN.md:
  `/v1/foods/search`, `/v1/foods/compare`, `/v1/foods/{fdc_id}`,
  `POST /v1/meals/estimate`, `/v1/nutrients`, `/v1/brands/search`.
  (Route-order fix: `/v1/foods/compare` is registered BEFORE `/v1/foods/{fdc_id}`
  — FastAPI matches in order and "compare" otherwise 422s as a non-integer fdc_id.)
- Auth: `X-API-Key` from `TROPHE_API_KEY`, `hmac.compare_digest`, 401 on
  missing/wrong key (same pattern as Phos `scripture-desk/api/app.py`).
- DB opened read-only (`mode=ro`), single connection.
- Search ranking (the value-add, all verified live):
  - FTS5 BM25 + porter stemming (index built per PLAN).
  - Two-pass: exact-phrase matches first, then token groups with synonym
    expansion (SYNONYMS + PHRASE_SYNONYMS documented in code: soda/pop,
    cilantro/coriander, aubergine/eggplant, courgette/zucchini, chips/crisps,
    scallion/spring onion/green onion, etc.).
  - Candidate pools split branded vs non-branded so the 2M branded rows can't
    drown out SR Legacy/Foundation/Survey on raw BM25.
  - Leading matches (description starts with query) guaranteed in the candidate
    set via a dedicated LIKE-prefixed fetch (raw-BM25 top-N alone missed them).
  - Final Python re-rank key: (starts-with-query, data-type rank
    sr_legacy<foundation<survey<branded, processed-form penalty, BM25).
    Processed-form penalty (PROCESSED_WORDS in code: dried, frozen, canned,
    breaded, ...) demotes e.g. "Egg, whole, dried" below "Egg, whole, raw,
    fresh" for bare queries — but NOT when the query itself names the form.
- Non-negotiables: every food response carries `data_type`; branded items carry
  "Branded values are manufacturer self-reported; verify against the package
  label."; API description + all food/meal responses carry "Reference data only,
  not medical or dietary advice."

### Live curl transcripts (server: 127.0.0.1:8123, key=test-local-key)
- `GET /health` (no key) → 200 `{"status":"ok","version":"1.0.0","foods":2013644}`
- `GET /v1/foods/search` no key → 401; wrong key → 401 (both verified)
- `GET /v1/foods/search?q=egg&limit=5` →
  1. 171287 sr_legacy "Egg, whole, raw, fresh" 143.0 kcal  ← obvious best hit first
  2. 172183 sr_legacy "Egg, white, raw, fresh" 52.0
  3. 172184 sr_legacy "Egg, yolk, raw, fresh" 322.0
  4. 172185 sr_legacy "Egg, whole, cooked, omelet" 154.0
  5. 172186 sr_legacy "Egg, whole, cooked, poached" 143.0
- `GET /v1/foods/171287` → Energy 143.0 kcal (%DV 7.1), Protein 12.56g (%DV 25.1),
  Fat 9.51g, Carbs 0.72g, Sodium 142.0mg (%DV 6.2); 134 nutrients; portions
  small=38g / medium=44g / large=50g; disclaimer present
- `POST /v1/meals/estimate` {100g egg 171287 + 100g chicken breast 174608} →
  summary {kcal: 277.0 (=143+134), protein_g: 27.15, fat_g: 17.16, carbs_g: 2.51,
  fiber_g: 0.0, sugars_g: 0.8, sodium_mg: 1025.0}; per-item breakdown present;
  disclaimer present
- `GET /v1/foods/compare?ids=171287,174608` → side-by-side per-100g macros + %DV
  for both foods; 1 id → 400; unknown id → 404; no key → 401
- `GET /v1/nutrients` → 477 rows; Energy/kcal DV = 2000.0
- `GET /v1/brands/search?q=kellogg` → "Kellogg Company US" 10186, "The Kellogg Company" 6440
- Synonym check `q=cilantro` → "Cilantro, raw" (2709782) first
- Type filter `q=egg&type=branded` → only branded rows; `type=bogus` → 400; empty q → 400
- Branded search hits carry the manufacturer-self-reported note

## M3 — auth + OpenAPI (verified 2026-09-25)
- `GET /openapi.json` from the running server: `"openapi": "3.1.0"`.
- All 7 coded routes present in spec paths (health, search, compare, fdc_id,
  meals/estimate, nutrients, brands/search) — none missing.
- `securitySchemes: APIKeyHeader`; all 6 data endpoints require it in the spec,
  `/health` has no security requirement.
- API description contains the medical-advice disclaimer.
- Note: served spec requires the API key to fetch only because every route is
  behind auth in this build's test config — the static openapi.json content
  itself was validated, not the fetch path.

## Decisions / deviations from PLAN.md
1. Route order: `/v1/foods/compare` registered before `/v1/foods/{fdc_id}`
   (FastAPI first-match routing; otherwise /compare 422s). No path changes.
2. Search ranking refined beyond PLAN's sketch after live testing showed BM25
   alone ranked "Bread, egg" and branded "EGGS" above the canonical egg:
   (a) branded/non-branded candidate pools, (b) leading-match candidate fetch,
   (c) processed-form penalty. All documented in code comments.
3. `%DV` computed only when nutrient unit == DV unit (conservative; e.g. kJ
   Energy has no %DV). Documented in `_pct`.
4. Sugars: prefers nutrient 2000 (Sugars, Total), falls back to 1063 (NLEA)
   when 2000 absent — both verified present in the catalog.

## Boundaries honored
- No Cloud Run deploy, no GCP resources, no money spent, no GitHub push,
  no Meta submission. Local build + local tests only.
- Test server left running on 127.0.0.1:8123 (key: test-local-key) for inspection;
  kill with: pkill -f "uvicorn app:app" (from ~/workspace/trophe/api).

## 2026-09-24 — Rename: NutriBase → Trophe
- "NutriBase" was already in use (2025 Frontiers in Nutrition food-data system + a published food-counts book/software). Jeremiah picked **Trophe** (Greek τροφή, nourishment/food; NT word, pairs with Phos).
- First-pass name checks: no live USPTO mark for TROPHE in software/API space; one abandoned "T TROPHÉ" (chemicals); a Mexican B2B ingredient company named Trophe (different lane, no US mark found). No active site found at trophe.com (registration status unconfirmed). Not a formal clearance opinion.
- Renamed: `~/workspace/nutrid/` → `~/workspace/trophe/`, `data/nutrid.db` → `data/trophe.db`, skill → `~/workspace/skills/trophe/`, `bin/nutrid` → `bin/trophe`, `NUTRID_API_KEY`/`NUTRID_BASE_URL` → `TROPHE_API_KEY`/`TROPHE_BASE_URL`, API title → "Trophe". Venv shebangs repointed at the new path (the rename had broken the `uvicorn` launcher).
- Local test API key rotated (old key retired with the old name). Server restarted from the new path on 127.0.0.1:8123 and re-verified: health ok with 2,013,644 foods; 401 on data routes without key; `trophe search egg` first hit 171287 ("Egg, whole, raw, fresh", sr_legacy, 143.0 kcal/100g); `trophe food 171287` and `trophe meal 171287:100` correct.

## 2026-09-24 — Search ranking pass ("chicken breast" test)
- Bare "chicken breast" returned a deli roll first (starts-with-query dominated the rank key); bare "salmon" returned salmon nuggets and fish oil. Both are wrong canonical answers for top nutrition questions.
- Fixes in `api/app.py`: rank key reordered to (data-type rank, processed-form penalty, starts-with-query, BM25) so plain forms beat processed variants even when the processed form leads with the query; PROCESSED_WORDS extended with roll, sliced, deli, glazed, rotisserie, bbq, barbecue, marinated, oil, nugget, nuggets (query terms are still excluded from the penalty, so "smoked salmon" and "olive oil" keep working); non-leading candidate fill enlarged from `limit*3` to `max(limit*10, 200)` because raw BM25 buries long canonical descriptions ("Chicken, broilers or fryers, breast, meat only, cooked, roasted") outside tiny pools.
- Verified after restart: "chicken breast" -> three plain sr_legacy forms (171474/171477/171478); "salmon" -> plain raw salmon (no fish oil, no nuggets); "egg" still 171287 first; "smoked salmon" still smoked forms first; "olive oil" second hit is the canonical "Oil, olive, salad or cooking".
- CLI: exits silently (no BrokenPipeError traceback) when piped to head/tail via SIGPIPE default handler.
- SKILL.md: added "Choosing the right hit" guidance (prefer plainest sr_legacy/foundation match for the preparation the user means; always cite description + data type).

## 2026-09-24 — Deploy prep (Cloud Run path, Jeremiah approved)

- Wrote `Dockerfile` (python:3.12-slim, pip install from api/requirements.txt,
  DB reassembled from data/dist/ chunks into ../data/trophe.db, uvicorn on $PORT).
- Wrote `api/requirements.txt` (pinned freeze: fastapi 0.141.1, uvicorn 0.53.0, ...).
- Added public no-auth routes to api/app.py, all verified live locally (HTTP 200):
  `/` (landing page), `/privacy` (privacy policy), `/terms` (terms of service).
  Legal copy is original, professional, no template markers; effective 2026-09-24.
- Scaffolded repo: README.md, LICENSE (CC0, adapted from Phos), .gitignore, .dockerignore.
- Split data/trophe.db into 18 x 95MB chunks under data/dist/ (trophe.db.part-00..17).
  Reassembly verified: sha256 identical, PRAGMA integrity_check ok, 2,013,644 foods.
- Installed crane 0.20.3 (~/workspace/bin/crane); Docker Hub reachable via crane.
- Installed Google Cloud SDK 586.0.0 (~/workspace/google-cloud-sdk/).
- NOTE: /tmp is a 512MB tmpfs; all large staging must use /home/hatch (83GB free).
- BLOCKED on Jeremiah: (1) fresh GCP service-account key (transient paste),
  (2) single-use GitHub PAT for private dexiadigi-cloud/trophe-api repo push,
  (3) review of /privacy + /terms wording.

## 2026-09-25 09:47 HST - Cloud Run deploy complete (v1)
- Image: us-west1-docker.pkg.dev/winter-pivot-458405-a7/cloud-run-source-deploy/trophe:v1
  digest sha256:6cbbd3c83b89f8dc6be5e0f75c245c32755721e2d240a2524557a6759cb495ce
  (base python:3.12-slim configured via crane mutate, app layer appended via crane append)
- Service: trophe, region us-west1, memory 2Gi, cpu 1, max-instances 3, min-instances 0
- URL: https://trophe-373637331608.us-west1.run.app
- Live verification: /health 200, / 200 (title ok), /privacy 200, /terms 200,
  /v1/foods/search without key 401, egg search -> 171287 Egg whole raw fresh sr_legacy,
  /v1/foods/171287 134 nutrients, /v1/nutrients 477 entries,
  /v1/meals/estimate 100g egg -> 143 kcal.
- Phos service untouched: https://phos-6skctauhva-uw.a.run.app/health 200 after deploy.
- Deploy SA key used transiently, then revoked; key file, env.yaml, staging dir,
  ~/.config/gcloud and ~/.docker all deleted. No credential trace remains.
- Production TROPHE_API_KEY: generated fresh, set as Cloud Run env var only.
  Not printed, not stored in files or memory.
- Still pending: GitHub PAT (private repo push), Jeremiah's legal-page review,
  connector registration, Meta submission.
