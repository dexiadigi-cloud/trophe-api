# Trophe API

☕ [Support on Ko-fi](https://ko-fi.com/dexiadigi)

Trophe is a free food & nutrition lookup API. Search 2M+ foods, get per-100g
nutrient profiles with % Daily Values, estimate whole meals, and compare foods
side by side.

**Reference data only, not medical or dietary advice.** Branded values are
manufacturer self-reported; verify against the package label.

## Data

All food and nutrient data comes from the USDA FoodData Central bulk
downloads (https://fdc.nal.usda.gov/download-datasets.html), released under
**CC0 1.0 Universal (public domain)**. No permission needed, no attribution
required. The database is built locally from the bulk CSVs with
`scripts/build_db.py`; the running service only reads the resulting SQLite
file.

## Quickstart (local)

```bash
cd api
TROPHE_API_KEY=your-long-random-key ../.venv/bin/uvicorn app:app --port 8123
```

Then:

```bash
curl -H "X-API-Key: your-long-random-key" \
  "http://127.0.0.1:8123/v1/foods/search?q=chicken%20breast&limit=3"
```

Interactive docs: http://127.0.0.1:8123/docs (OpenAPI 3.1, always current).

## Endpoints

| Method | Path | Description |
| ------ | ---- | ----------- |
| GET | `/v1/foods/search?q=...` | Full-text food search, ranked |
| GET | `/v1/foods/{fdc_id}` | Full nutrient profile for one food |
| POST | `/v1/meals/estimate` | Nutrient totals for a list of foods + grams |
| GET | `/v1/compare?ids=...` | Side-by-side nutrient comparison |
| GET | `/v1/brands/search?q=...` | Search brand owners / brand names |
| GET | `/v1/nutrients` | Nutrient catalog with Daily Values |
| GET | `/health` | Service status (no auth required) |

Every food response includes its USDA `data_type`
(`sr_legacy`, `foundation`, `survey`, or `branded`).

## Auth

Exactly one API key in one header: `X-API-Key`. The server reads the expected
key from the `TROPHE_API_KEY` environment variable and compares in constant
time. Requests without a valid key get HTTP 401. Set the key in the host
environment, never in the image or repo.

## Docker

The SQLite corpus ships as <100MB chunks under `data/dist/` (GitHub
single-file limit) and is reassembled during the image build:

```bash
docker build -t trophe .
docker run -e TROPHE_API_KEY=your-long-random-key -p 8000:8000 trophe
```

## Legal

- Privacy policy: served at `/privacy`
- Terms of service: served at `/terms`
- Code license: CC0 1.0 Universal (see LICENSE)
- Operator contact: dexiadigi@gmail.com
