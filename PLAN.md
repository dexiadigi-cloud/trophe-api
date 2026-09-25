# Trophe — PLAN.md

**Goal:** v1 of Trophe, a food & nutrition REST API designed to become a Muse custom connector,
following the Phos (Scripture Desk) pattern: FastAPI + SQLite, one container, one API key header,
OpenAPI 3.1 always current, milestones verified with real curl tests and real DB row counts.

**Repo:** `~/workspace/trophe/` — `PLAN.md` (this file), `BUILD_LOG.md` (append-only, verified facts only).

## Data source (verified license — do NOT substitute)
- USDA FoodData Central bulk downloads: https://fdc.nal.usda.gov/download-datasets.html
- License: **CC0 1.0 public domain**. No permission needed, no attribution required.
- Host our own index from the bulk CSVs. **Do NOT proxy USDA's keyed API** (1,000 req/hr limit).
- v1 scope: SR Legacy + Foundation Foods + FNDDS Survey Foods + Branded Foods
  (or a documented subset if size forces it — decide on measured evidence, note tradeoff in BUILD_LOG.md).

## Non-negotiables
- API description and docs carry: **"Reference data only, not medical or dietary advice."**
- Branded-food responses carry: **"Branded values are manufacturer self-reported; verify against the package label."**
- Every food response includes its `data_type` (`sr_legacy` | `foundation` | `survey` | `branded`).
- Auth: exactly one API key in one header: `X-API-Key`, from `TROPHE_API_KEY` env var, constant-time compare, HTTP 401 on failure. No OAuth.
- OpenAPI 3.1 spec served from the running server and validated against the code.
- DO NOT deploy to Cloud Run, create GCP resources, spend money, push to GitHub, or submit to Meta.
  Build and test locally. Deployment/connector decisions come from Jeremiah later.

## DB schema (`data/trophe.db`)
```sql
CREATE TABLE foods (
  fdc_id INTEGER PRIMARY KEY,
  data_type TEXT NOT NULL,               -- sr_legacy | foundation | survey | branded
  description TEXT NOT NULL,
  brand_owner TEXT,                      -- branded only
  brand_name TEXT,                       -- branded only
  gtin_upc TEXT,
  serving_size REAL,                     -- branded label serving
  serving_size_unit TEXT,
  household_serving TEXT,                -- e.g. "1 cup"
  publication_date TEXT
);
CREATE TABLE nutrients (
  id INTEGER PRIMARY KEY,
  name TEXT NOT NULL,
  unit_name TEXT NOT NULL,
  nutrient_nbr TEXT
);
CREATE TABLE food_nutrients (
  food_fdc_id INTEGER NOT NULL,
  nutrient_id INTEGER NOT NULL,
  amount REAL NOT NULL,                  -- per 100g
  PRIMARY KEY (food_fdc_id, nutrient_id)
);
CREATE INDEX idx_fn_food ON food_nutrients(food_fdc_id);
CREATE TABLE daily_values (              -- FDA label reference values (21 CFR 101.9)
  nutrient_id INTEGER PRIMARY KEY,
  daily_value REAL NOT NULL,
  dv_unit TEXT NOT NULL
);
CREATE TABLE food_portions (              -- household measures for sr/foundation/survey
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  food_fdc_id INTEGER NOT NULL,
  portion_description TEXT NOT NULL,
  gram_weight REAL NOT NULL
);
CREATE VIRTUAL TABLE foods_fts USING fts5(
  description, brand_owner, brand_name,
  content='foods', content_rowid='fdc_id',
  tokenize='porter unicode61'
);
```
Rebuild ETL script must live in `scripts/build_db.py` (reproducible).

## Search ranking (the main value-add)
- SQLite FTS5 + BM25. Porter stemming handles egg/eggs.
- Exact-phrase matches boosted above partial matches.
- Rank data types: SR Legacy / Foundation first for generic queries (analytical quality),
  branded hits interleaved below for packaged-food queries.
- Alias/synonym handling for common cases (document the list in the code).

## v1 endpoints
| Method | Path | Purpose |
|---|---|---|
| GET | /v1/foods/search?q=&type=&limit= | Ranked matches: fdc_id, description, data_type, brand_owner, brand_name, calories_per_100g |
| GET | /v1/foods/{fdcId} | Full profile per 100g + household servings + %DV, with disclaimers |
| POST | /v1/meals/estimate | Body: `{"items":[{"fdc_id":N,"grams":G}]}` → totals (kcal, macros, micros) + per-item breakdown |
| GET | /v1/foods/compare?ids={a,b} | Side-by-side macros ("which has more protein?") |
| GET | /v1/nutrients | Nutrient catalog with units + daily reference values |
| GET | /v1/brands/search?q= | Distinct brand_owner matches with food counts |
| GET | /health | No auth. Liveness. |

## Milestones & acceptance criteria
- **M1 — data ingested.** ETL script + `data/trophe.db` built. Acceptance: exact food counts per
  data_type queried from the DB itself (sqlite3), FTS index populated, sample sanity spot-checks.
- **M2 — API running locally.** All 7 endpoints curl-tested against the live server, including a
  sanity check on known values (e.g. raw egg calories in the sane range — verified against the
  DB data, never from memory). Acceptance: curl transcripts in BUILD_LOG.md.
- **M3 — auth + OpenAPI.** X-API-Key on all endpoints (401 without/wrong key, 200 with), OpenAPI 3.1
  JSON fetched from the running server and validated (every route in code appears in the spec).

## Verification discipline
Every subagent DONE is unverified until the raw output is seen (sqlite3 row counts, curl output,
openapi.json from the running server). Report only verified facts.
