#!/usr/bin/env python3
"""Trophe M1 ETL: build ~/workspace/trophe/data/trophe.db from USDA FoodData Central bulk CSVs.

Reproducible: streams all CSVs straight out of the zips (never extracts multi-GB
files to disk, never loads a full CSV into RAM), rebuilds the DB from scratch.

Sources (CC0 1.0, public domain), downloaded 2026-09-24:
  - SR Legacy:      FoodData_Central_sr_legacy_food_csv_2018-04.zip       (6,074,592 B)
  - Foundation:     FoodData_Central_foundation_food_csv_2026-04-30.zip   (3,825,741 B)
  - Survey (FNDDS): FoodData_Central_survey_food_csv_2024-10-31.zip       (3,325,692 B)
  - Branded:        FoodData_Central_branded_food_csv_2026-04-30.zip      (448,767,220 B)

Data decisions (measured, see BUILD_LOG.md):
  - Foundation: only the `foundation_food` data_type rows (published aggregate
    profiles with complete nutrient data). `sample_food` rows carry no nutrient
    rows in this release; `sub_sample_food` / `market_acquisition` /
    `agricultural_acquisition` are sampling-collection records, not food profiles.
  - Branded: all ~2M foods and all nutrient rows kept. Measured: median 14
    nutrients/food and the FDA DV-core set covers 97.5% of the 26M rows, so a
    "top ~30 nutrients per food" cap is effectively a no-op - full fidelity won.
  - Survey (FNDDS) food_nutrient.nutrient_id references nutrient NUMBER
    (nutrient_nbr), not nutrient.id (65/65 distinct ids match nutrient_nbr, 0/65
    match nutrient.id). Remapped through the catalog at load time.
  - Foundation rows referencing nutrient id 2066 (33 rows, empty amounts) were
    dropped: 2066 exists in no dataset's nutrient.csv.

Usage: python3 scripts/build_db.py
"""

import csv
import io
import sqlite3
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RAW = ROOT / "data" / "raw"
DB = ROOT / "data" / "trophe.db"

DATASETS = {
    "sr_legacy": {
        "zip": "FoodData_Central_sr_legacy_food_csv_2018-04.zip",
        "inner": "FoodData_Central_sr_legacy_food_csv_2018-04",
        "nutrient_join": "id",
        "portions": True,
        "food_filter": lambda dt: True,
        "data_type": "sr_legacy",
    },
    "foundation": {
        "zip": "FoodData_Central_foundation_food_csv_2026-04-30.zip",
        "inner": "FoodData_Central_foundation_food_csv_2026-04-30",
        "nutrient_join": "id",
        "portions": True,
        "food_filter": lambda dt: dt == "foundation_food",
        "data_type": "foundation",
    },
    "survey": {
        "zip": "FoodData_Central_survey_food_csv_2024-10-31.zip",
        "inner": "FoodData_Central_survey_food_csv_2024-10-31",
        "nutrient_join": "nbr",
        "portions": True,
        "food_filter": lambda dt: True,
        "data_type": "survey",
    },
    "branded": {
        "zip": "FoodData_Central_branded_food_csv_2026-04-30.zip",
        "inner": "FoodData_Central_branded_food_csv_2026-04-30",
        "nutrient_join": "id",
        "portions": False,
        "food_filter": lambda dt: True,
        "data_type": "branded",
    },
}

SCHEMA = """
CREATE TABLE foods (
  fdc_id INTEGER PRIMARY KEY,
  data_type TEXT NOT NULL,
  description TEXT NOT NULL,
  brand_owner TEXT,
  brand_name TEXT,
  gtin_upc TEXT,
  serving_size REAL,
  serving_size_unit TEXT,
  household_serving TEXT,
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
  amount REAL NOT NULL,
  PRIMARY KEY (food_fdc_id, nutrient_id)
);
CREATE INDEX idx_fn_food ON food_nutrients(food_fdc_id);
CREATE TABLE daily_values (
  nutrient_id INTEGER PRIMARY KEY,
  daily_value REAL NOT NULL,
  dv_unit TEXT NOT NULL
);
CREATE TABLE food_portions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  food_fdc_id INTEGER NOT NULL,
  portion_description TEXT NOT NULL,
  gram_weight REAL NOT NULL
);
CREATE INDEX idx_fp_food ON food_portions(food_fdc_id);
CREATE VIRTUAL TABLE foods_fts USING fts5(
  description, brand_owner, brand_name,
  content='foods', content_rowid='fdc_id',
  tokenize='porter unicode61'
);
"""

# (nutrient_nbr, dv_value, dv_unit) - FDA 21 CFR 101.9(c)(9) Daily Values,
# adults and children >= 4 years. nutrient_nbr values verified against the
# nutrient.csv catalogs (not from memory of USDA ids).
FDA_DV = [
    ("208", 2000, "kcal"),  # Energy
    ("204", 78, "g"),       # Total Fat
    ("606", 20, "g"),       # Saturated Fat
    ("601", 300, "mg"),     # Cholesterol
    ("307", 2300, "mg"),    # Sodium
    ("205", 275, "g"),      # Total Carbohydrate
    ("291", 28, "g"),       # Dietary Fiber
    ("539", 50, "g"),       # Added Sugars
    ("203", 50, "g"),       # Protein
    ("328", 20, "mcg"),     # Vitamin D (D2 + D3)
    ("301", 1300, "mg"),    # Calcium
    ("303", 18, "mg"),      # Iron
    ("306", 4700, "mg"),    # Potassium
    ("320", 900, "mcg"),    # Vitamin A, RAE
    ("401", 90, "mg"),      # Vitamin C
    ("323", 15, "mg"),      # Vitamin E (alpha-tocopherol)
    ("430", 120, "mcg"),    # Vitamin K (phylloquinone)
    ("404", 1.2, "mg"),     # Thiamin
    ("405", 1.3, "mg"),     # Riboflavin
    ("406", 16, "mg"),      # Niacin
    ("415", 1.7, "mg"),     # Vitamin B6
    ("417", 400, "mcg"),    # Folate, total
    ("418", 2.4, "mcg"),    # Vitamin B-12
    ("416", 30, "mcg"),     # Biotin
    ("410", 5, "mg"),       # Pantothenic acid
    ("305", 1250, "mg"),    # Phosphorus
    ("314", 150, "mcg"),    # Iodine
    ("304", 420, "mg"),     # Magnesium
    ("309", 11, "mg"),      # Zinc
    ("317", 55, "mcg"),     # Selenium
    ("312", 0.9, "mg"),     # Copper
    ("315", 2.3, "mg"),     # Manganese
    ("310", 35, "mcg"),     # Chromium
    ("316", 45, "mcg"),     # Molybdenum
    ("302", 2300, "mg"),    # Chloride
    ("421", 550, "mg"),     # Choline, total
]

UNIT_NORM = {"G": "g", "MG": "mg", "UG": "mcg", "KCAL": "kcal"}

BATCH = 20_000  # insert batch size


def stream_csv(zip_path: Path, name: str):
    """Yield dict rows from a CSV inside a zip, streaming (no full extract)."""
    z = zipfile.ZipFile(zip_path)
    fh = io.TextIOWrapper(z.open(name), encoding="utf-8", errors="replace", newline="")
    reader = csv.DictReader(fh)
    yield from reader


def fnum(s):
    try:
        v = float(s)
    except (TypeError, ValueError):
        return None
    return v if v == v else None  # reject NaN


def log(msg):
    print(f"[{time.strftime('%H:%M:%S')}] {msg}", flush=True)


def main():
    t0 = time.time()
    DB.parent.mkdir(parents=True, exist_ok=True)
    if DB.exists():
        log(f"removing existing {DB}")
        DB.unlink()

    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    con.execute("PRAGMA cache_size=-262144")  # 256 MB page cache during build
    con.executescript(SCHEMA)
    cur = con.cursor()

    # ---- nutrients: union of all four catalogs (branded has the 477-row superset)
    nutrients = {}  # id -> (name, unit_name, nutrient_nbr)
    for key, spec in DATASETS.items():
        for r in stream_csv(RAW / spec["zip"], f"{spec['inner']}/nutrient.csv"):
            nid = int(r["id"])
            if nid not in nutrients:
                nutrients[nid] = (r["name"], r["unit_name"], r["nutrient_nbr"])
    log(f"nutrients catalog: {len(nutrients)} distinct (union of all datasets)")
    cur.executemany(
        "INSERT INTO nutrients (id, name, unit_name, nutrient_nbr) VALUES (?,?,?,?)",
        [(nid, name, UNIT_NORM.get(unit, unit), nbr)
         for nid, (name, unit, nbr) in sorted(nutrients.items())],
    )
    nbr_to_id = {nbr: nid for nid, (_, _, nbr) in nutrients.items()}
    con.commit()

    # ---- foods (+ branded extras)
    food_ids = set()  # fdc_ids actually inserted (food_nutrient is inner-joined)
    counts = {}
    for key, spec in DATASETS.items():
        n = 0
        brand_rows = {}
        if key == "branded":
            for r in stream_csv(RAW / spec["zip"], f"{spec['inner']}/branded_food.csv"):
                brand_rows[r["fdc_id"]] = (
                    r.get("brand_owner") or None,
                    r.get("brand_name") or None,
                    r.get("gtin_upc") or None,
                    fnum(r.get("serving_size")),
                    r.get("serving_size_unit") or None,
                    r.get("household_serving_fulltext") or None,
                )
        batch = []
        for r in stream_csv(RAW / spec["zip"], f"{spec['inner']}/food.csv"):
            if not spec["food_filter"](r["data_type"]):
                continue
            fid = int(r["fdc_id"])
            owner = name_ = gtin = ssize = ssunit = hh = None
            if key == "branded":
                owner, name_, gtin, ssize, ssunit, hh = brand_rows.get(r["fdc_id"], (None,) * 6)
            batch.append((
                fid, spec["data_type"], r["description"],
                owner, name_, gtin, ssize, ssunit, hh,
                r.get("publication_date") or None,
            ))
            food_ids.add(fid)
            n += 1
            if len(batch) >= BATCH:
                cur.executemany(
                    "INSERT INTO foods VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT INTO foods VALUES (?,?,?,?,?,?,?,?,?,?)", batch)
        con.commit()
        counts[spec["data_type"]] = n
        log(f"foods[{key}]: {n:,} rows")

    # ---- food_nutrients (streaming; survey joins on nutrient_nbr)
    total_nut_rows = 0
    skipped = {"bad_amount": 0, "unknown_nutrient": 0, "unknown_food": 0}
    for key, spec in DATASETS.items():
        use_nbr = spec["nutrient_join"] == "nbr"
        n = 0
        batch = []
        for r in stream_csv(RAW / spec["zip"], f"{spec['inner']}/food_nutrient.csv"):
            fid = int(r["fdc_id"])
            if fid not in food_ids:
                skipped["unknown_food"] += 1
                continue
            amt = fnum(r["amount"])
            if amt is None:
                skipped["bad_amount"] += 1
                continue
            if use_nbr:
                nid = nbr_to_id.get(r["nutrient_id"])
            else:
                nid = int(r["nutrient_id"])
            if nid not in nutrients:
                skipped["unknown_nutrient"] += 1
                continue
            batch.append((fid, nid, amt))
            n += 1
            if len(batch) >= BATCH:
                cur.executemany(
                    "INSERT OR REPLACE INTO food_nutrients VALUES (?,?,?)", batch)
                batch.clear()
        if batch:
            cur.executemany("INSERT OR REPLACE INTO food_nutrients VALUES (?,?,?)", batch)
        con.commit()
        total_nut_rows += n
        log(f"food_nutrients[{key}]: {n:,} rows (cumulative {total_nut_rows:,})")
    log(f"food_nutrients total: {total_nut_rows:,}; skipped={skipped}")

    # ---- food_portions (sr_legacy / foundation / survey)
    pn = 0
    for key, spec in DATASETS.items():
        if not spec["portions"]:
            continue
        batch = []
        for r in stream_csv(RAW / spec["zip"], f"{spec['inner']}/food_portion.csv"):
            try:
                fid = int(r["fdc_id"])
            except (TypeError, ValueError):
                continue  # malformed rows exist (e.g. empty fdc_id in foundation)
            if fid not in food_ids:
                continue
            gw = fnum(r.get("gram_weight"))
            if gw is None:
                continue
            desc = (r.get("portion_description") or "").strip()
            mod = (r.get("modifier") or "").strip()
            label = (f"{desc} {mod}".strip()) or f"serving"
            batch.append((fid, label, gw))
            if len(batch) >= BATCH:
                cur.executemany(
                    "INSERT INTO food_portions (food_fdc_id, portion_description, gram_weight) VALUES (?,?,?)",
                    batch)
                batch.clear()
        if batch:
            cur.executemany(
                "INSERT INTO food_portions (food_fdc_id, portion_description, gram_weight) VALUES (?,?,?)",
                batch)
        con.commit()
        n = cur.execute("SELECT COUNT(*) FROM food_portions").fetchone()[0] - pn
        pn += n
        log(f"food_portions[{key}]: {n:,} rows")
    log(f"food_portions total: {pn:,}")

    # ---- daily_values (FDA 21 CFR 101.9), resolved via nutrient_nbr from the catalog
    dv_rows = []
    for nbr, value, unit in FDA_DV:
        nid = nbr_to_id.get(nbr)
        if nid is None:
            raise RuntimeError(f"FDA_DV nutrient_nbr {nbr} not found in catalog - aborting")
        dv_rows.append((nid, value, unit))
    cur.executemany("INSERT INTO daily_values VALUES (?,?,?)", dv_rows)
    con.commit()
    log(f"daily_values: {len(dv_rows)} rows")

    # ---- FTS5 index
    log("building foods_fts...")
    cur.execute(
        "INSERT INTO foods_fts(rowid, description, brand_owner, brand_name) "
        "SELECT fdc_id, description, brand_owner, brand_name FROM foods")
    con.commit()
    fts_n = cur.execute("SELECT COUNT(*) FROM foods_fts").fetchone()[0]
    food_n = cur.execute("SELECT COUNT(*) FROM foods").fetchone()[0]
    log(f"foods_fts: {fts_n:,} (foods: {food_n:,})")

    con.execute("PRAGMA journal_mode=DELETE")
    con.commit()
    con.execute("ANALYZE")
    con.commit()
    con.close()
    log(f"done in {time.time()-t0:.1f}s -> {DB} ({DB.stat().st_size/1e9:.2f} GB)")
    log(f"food counts per data_type: {counts}")


if __name__ == "__main__":
    main()
