"""Trophe v1 - food & nutrition REST API.

Serves USDA FoodData Central (CC0 1.0 public domain) from a local read-only
SQLite index. Designed to become a Muse custom connector: single API key in
one header (X-API-Key), OpenAPI 3.1 spec, stateless.

Reference data only, not medical or dietary advice.

Run locally (from this directory)::

    TROPHE_API_KEY=test-key ../.venv/bin/uvicorn app:app --port 8123
"""

from __future__ import annotations

import hmac
import os
import re
import sqlite3
from pathlib import Path
from typing import Annotated, Optional

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.responses import HTMLResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field

DISCLAIMER = "Reference data only, not medical or dietary advice."
BRANDED_NOTE = (
    "Branded values are manufacturer self-reported; "
    "verify against the package label."
)

# ---------------------------------------------------------------------------
# Auth: exactly one API key in one header (X-API-Key), constant-time compare.
# ---------------------------------------------------------------------------
_API_KEY = os.environ.get("TROPHE_API_KEY", "")
if not _API_KEY:
    raise RuntimeError(
        "TROPHE_API_KEY environment variable is required (the API key clients "
        "must send in the X-API-Key header)."
    )

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    key: Annotated[str | None, Depends(api_key_header)],
) -> str:
    """Reject requests without the correct X-API-Key header (HTTP 401)."""
    ok = key is not None and hmac.compare_digest(key, _API_KEY)
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="Invalid or missing API key. Send it in the X-API-Key header.",
        )
    return key  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# DB (read-only)
# ---------------------------------------------------------------------------
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "trophe.db"
_db = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True, check_same_thread=False)
_db.row_factory = sqlite3.Row

# Key nutrient IDs (USDA nutrient numbers, verified against nutrients table)
N_KCAL = 1008      # Energy, kcal
N_PROTEIN = 1003   # Protein, g
N_FAT = 1004       # Total lipid (fat), g
N_CARBS = 1005     # Carbohydrate, by difference, g
N_FIBER = 1079     # Fiber, total dietary, g
N_SUGARS = 2000    # Sugars, Total, g (fallback 1063 = Sugars, Total NLEA)
N_SUGARS_NLEA = 1063
N_SODIUM = 1093    # Sodium, Na, mg

DATA_TYPES = ("sr_legacy", "foundation", "survey", "branded")

# ---------------------------------------------------------------------------
# Synonym handling (documented here per PLAN.md).
# Word-level: applied per query token. Phrase-level: applied when the whole
# query matches. Covers common US/UK and alias differences in food names.
# ---------------------------------------------------------------------------
SYNONYMS: dict[str, list[str]] = {
    "soda": ["pop", "cola", "soft drink"],
    "pop": ["soda", "cola"],
    "coriander": ["cilantro"],
    "cilantro": ["coriander"],
    "aubergine": ["eggplant"],
    "eggplant": ["aubergine"],
    "courgette": ["zucchini"],
    "zucchini": ["courgette"],
    "chips": ["crisps", "fries"],
    "crisps": ["chips"],
    "fries": ["chips"],
    "rocket": ["arugula"],
    "arugula": ["rocket"],
    "scallion": ["spring onion", "green onion"],
    "prawn": ["shrimp"],
    "shrimp": ["prawn"],
    "mince": ["ground"],
    "biscuit": ["cookie"],
    "cookie": ["biscuit"],
    "cornflour": ["cornstarch"],
    "cornstarch": ["cornflour"],
    "capsicum": ["bell pepper"],
    "yam": ["sweet potato"],
}
PHRASE_SYNONYMS: dict[str, list[str]] = {
    "green onion": ["scallion", "spring onion"],
    "spring onion": ["scallion", "green onion"],
    "bell pepper": ["capsicum"],
    "sweet potato": ["yam"],
    "ground beef": ["mince"],
    "french fries": ["chips", "fries"],
}

_FTS_JUNK = re.compile(r'[\"*():^]')


def _clean(s: str) -> str:
    """Strip FTS5 special chars (prevents query-syntax injection)."""
    return _FTS_JUNK.sub(" ", s).strip()


def _build_token_match(q: str) -> Optional[str]:
    """Build an FTS5 MATCH expression: per-token groups of token OR synonyms.

    Groups are AND-ed together; multi-word alternatives are quoted phrases.
    """
    cleaned = _clean(q)
    if not cleaned:
        return None
    lowered = cleaned.lower()
    if lowered in PHRASE_SYNONYMS:
        alts = [cleaned] + PHRASE_SYNONYMS[lowered]
        return "(" + " OR ".join(f'"{_clean(a)}"' for a in alts) + ")"
    groups = []
    for tok in cleaned.split():
        alts = [tok] + SYNONYMS.get(tok.lower(), [])
        quoted = " OR ".join(f'"{_clean(a)}"' for a in alts if _clean(a))
        groups.append(quoted if " OR " not in quoted else f"({quoted})")
    return " ".join(groups)


# Words indicating a processed/prepared form. When the user's query does NOT
# contain them, hits containing them are penalized so the plain/canonical form
# (e.g. "Egg, whole, raw, fresh") outranks processed variants for bare queries.
PROCESSED_WORDS = frozenset({
    "dried", "frozen", "canned", "powder", "dehydrated", "mix", "smoked",
    "pickled", "salted", "sweetened", "breaded", "fried", "battered", "cured",
    "fermented", "condensed", "evaporated", "instant", "flavored", "seasoned",
    "roll", "sliced", "deli", "glazed", "rotisserie", "bbq", "barbecue",
    "marinated", "oil", "nugget", "nuggets",
})

_WORD_RE = re.compile(r"[a-z]+")


def _fts_rows(match: str, data_type: Optional[str], limit: int,
              branded_only: Optional[bool] = None,
              leading_only: bool = False, prefix: str = ""):
    """Fetch FTS candidates ordered by raw BM25.

    branded_only=True/False splits the candidate pool so the 2M branded rows
    can't drown out SR Legacy / Foundation / Survey hits on raw BM25.
    leading_only=True restricts to descriptions starting with the query text,
    guaranteeing leading matches are in the candidate set for re-ranking.
    """
    sql = (
        "SELECT f.fdc_id, f.description, f.data_type, f.brand_owner, f.brand_name,"
        " bm25(foods_fts) AS rnk FROM foods_fts"
        " JOIN foods f ON f.fdc_id = foods_fts.rowid"
        " WHERE foods_fts MATCH ?"
    )
    params: list = [match]
    if data_type:
        sql += " AND f.data_type = ?"
        params.append(data_type)
    elif branded_only is True:
        sql += " AND f.data_type = 'branded'"
    elif branded_only is False:
        sql += " AND f.data_type != 'branded'"
    if leading_only:
        sql += " AND lower(f.description) LIKE ?"
        params.append(prefix + "%")
    sql += " ORDER BY rnk LIMIT ?"
    params.append(limit)
    return _db.execute(sql, params).fetchall()


def _rank_key(row, prefix: str, query_tokens: set[str]):
    """(data-type rank, processed-form penalty, starts-with-query, BM25).

    SR Legacy / Foundation / Survey outrank branded; among otherwise equal
    hits the plain form beats processed variants ("dried", "frozen", ...).
    """
    desc = row["description"].lower()
    starts = 0 if desc.startswith(prefix) else 1
    type_rank = {"sr_legacy": 0, "foundation": 1, "survey": 2, "branded": 3}[
        row["data_type"]
    ]
    desc_tokens = set(_WORD_RE.findall(desc))
    penalty = len(PROCESSED_WORDS & desc_tokens - query_tokens)
    return (type_rank, penalty, starts, row["rnk"])


def _kcal_map(fdc_ids: list[int]) -> dict[int, float]:
    if not fdc_ids:
        return {}
    placeholders = ",".join("?" for _ in fdc_ids)
    rows = _db.execute(
        f"SELECT food_fdc_id, amount FROM food_nutrients"
        f" WHERE nutrient_id = ? AND food_fdc_id IN ({placeholders})",
        [N_KCAL, *fdc_ids],
    ).fetchall()
    return {r["food_fdc_id"]: r["amount"] for r in rows}


def search_foods(q: str, data_type: Optional[str], limit: int):
    """Two-pass ranked search: exact-phrase hits first, then token matches.

    Both passes feed a candidate pool that is re-ranked by data-type rank
    (SR Legacy / Foundation / Survey / Branded), then plain-form preference,
    then leading matches, then BM25.
    """
    if data_type is not None and data_type not in DATA_TYPES:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown type '{data_type}'. Valid types: {', '.join(DATA_TYPES)}.",
        )
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required.")
    prefix = _clean(q).lower()
    query_tokens = set(_WORD_RE.findall(q.lower()))
    seen: set[int] = set()
    candidates = []
    # Generous non-leading fill: raw BM25 favors short descriptions, so the
    # plain/canonical form (e.g. "Chicken, broilers or fryers, breast, meat
    # only, cooked, roasted") often sits far down the BM25 order. The fill
    # must be big enough to include it so the re-ranker can promote it.
    fill = max(limit * 10, 200)

    def _collect(rows):
        for r in rows:
            if r["fdc_id"] in seen:
                continue
            seen.add(r["fdc_id"])
            candidates.append(r)

    # Pass 1: exact phrase (boosted above partial matches)
    phrase = _clean(q)
    pools = [None] if data_type else [False, True]  # branded split, else one pool
    if phrase:
        for pool in pools:
            # Leading matches first (guaranteed in the candidate set), then fill
            _collect(_fts_rows(f'"{phrase}"', data_type, 500, pool, True, prefix))
            _collect(_fts_rows(f'"{phrase}"', data_type, fill, pool))
    # Pass 2: token groups with synonym expansion
    token_match = _build_token_match(q)
    if token_match:
        for pool in pools:
            _collect(_fts_rows(token_match, data_type, 500, pool, True, prefix))
            _collect(_fts_rows(token_match, data_type, fill, pool))

    candidates.sort(key=lambda r: _rank_key(r, prefix, query_tokens))
    out = candidates[:limit]

    kcal = _kcal_map([r["fdc_id"] for r in out])
    results = []
    for r in out:
        item = {
            "fdc_id": r["fdc_id"],
            "description": r["description"],
            "data_type": r["data_type"],
            "brand_owner": r["brand_owner"],
            "brand_name": r["brand_name"],
            "calories_per_100g": kcal.get(r["fdc_id"]),
        }
        if r["data_type"] == "branded":
            item["note"] = BRANDED_NOTE
        results.append(item)
    return results


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------
class FoodSummary(BaseModel):
    fdc_id: int
    description: str
    data_type: str
    brand_owner: Optional[str] = None
    brand_name: Optional[str] = None
    calories_per_100g: Optional[float] = None
    note: Optional[str] = None  # branded disclaimer, branded items only


class NutrientAmount(BaseModel):
    nutrient_id: int
    name: str
    unit: str
    amount_per_100g: float
    daily_value: Optional[float] = None
    dv_unit: Optional[str] = None
    pct_daily_value: Optional[float] = None


class Portion(BaseModel):
    description: str
    gram_weight: float


class FoodDetail(BaseModel):
    fdc_id: int
    description: str
    data_type: str
    brand_owner: Optional[str] = None
    brand_name: Optional[str] = None
    gtin_upc: Optional[str] = None
    serving_size: Optional[float] = None
    serving_size_unit: Optional[str] = None
    household_serving: Optional[str] = None
    nutrients: list[NutrientAmount]
    portions: list[Portion]
    note: Optional[str] = None
    disclaimer: str = DISCLAIMER


class MealItem(BaseModel):
    fdc_id: int = Field(gt=0)
    grams: float = Field(gt=0, le=10000)


class MealEstimateRequest(BaseModel):
    items: list[MealItem] = Field(min_length=1, max_length=50)


class MealItemResult(BaseModel):
    fdc_id: int
    description: str
    grams: float
    kcal: Optional[float] = None
    protein_g: Optional[float] = None
    fat_g: Optional[float] = None
    carbs_g: Optional[float] = None


class MealEstimateResponse(BaseModel):
    summary: dict[str, Optional[float]]
    totals: list[NutrientAmount]
    items: list[MealItemResult]
    disclaimer: str = DISCLAIMER


class CompareEntry(BaseModel):
    fdc_id: int
    description: str
    data_type: str
    per_100g: dict[str, Optional[float]]
    pct_daily_value: dict[str, Optional[float]]


class NutrientInfo(BaseModel):
    nutrient_id: int
    name: str
    unit: str
    nutrient_nbr: Optional[str] = None
    daily_value: Optional[float] = None
    dv_unit: Optional[str] = None


class BrandHit(BaseModel):
    brand_owner: str
    food_count: int


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------
app = FastAPI(
    title="Trophe",
    version="1.0.0",
    description=(
        "Food & nutrition lookup over USDA FoodData Central (CC0 1.0 public "
        "domain). Search 2M+ foods, get per-100g nutrient profiles with % Daily "
        "Values, estimate whole meals, and compare foods side by side. "
        + DISCLAIMER
    ),
    openapi_version="3.1.0",
)


def _pct(amount: float, unit: str, dv: Optional[float], dv_unit: Optional[str]):
    if dv is None or dv == 0 or dv_unit is None:
        return None
    if unit == dv_unit:
        return round(amount / dv * 100, 1)
    return None


def _get_food_or_404(fdc_id: int):
    row = _db.execute("SELECT * FROM foods WHERE fdc_id = ?", (fdc_id,)).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail=f"No food with fdc_id {fdc_id}.")
    return row


def _nutrient_rows(fdc_id: int) -> list[NutrientAmount]:
    rows = _db.execute(
        "SELECT n.id, n.name, n.unit_name, fn.amount, d.daily_value, d.dv_unit"
        " FROM food_nutrients fn JOIN nutrients n ON n.id = fn.nutrient_id"
        " LEFT JOIN daily_values d ON d.nutrient_id = n.id"
        " WHERE fn.food_fdc_id = ? ORDER BY n.id",
        (fdc_id,),
    ).fetchall()
    return [
        NutrientAmount(
            nutrient_id=r["id"],
            name=r["name"],
            unit=r["unit_name"],
            amount_per_100g=r["amount"],
            daily_value=r["daily_value"],
            dv_unit=r["dv_unit"],
            pct_daily_value=_pct(r["amount"], r["unit_name"], r["daily_value"], r["dv_unit"]),
        )
        for r in rows
    ]


@app.get(
    "/v1/foods/search",
    response_model=list[FoodSummary],
    dependencies=[Depends(require_api_key)],
    summary="Search foods",
    description=(
        "Ranked food search (FTS5 + BM25 with porter stemming). Exact-phrase "
        "matches rank first; SR Legacy / Foundation / Survey results rank above "
        "branded for generic queries. Supports common synonyms "
        "(soda/pop, cilantro/coriander, ...). " + DISCLAIMER
    ),
)
def foods_search(
    q: str = Query(..., description="Search text, e.g. 'egg', 'chicken breast'"),
    type: Optional[str] = Query(
        default=None, description="Restrict to a data type: sr_legacy, foundation, survey, branded"
    ),
    limit: int = Query(default=20, ge=1, le=50),
):
    return search_foods(q, type, limit)


@app.get(
    "/v1/foods/compare",
    response_model=list[CompareEntry],
    dependencies=[Depends(require_api_key)],
    summary="Compare foods side by side",
    description=(
        "Per-100g macros and % Daily Values for 2-5 foods, e.g. "
        "'which has more protein?'. " + DISCLAIMER
    ),
)
def foods_compare(
    ids: str = Query(..., description="Comma-separated fdc_ids, e.g. '171287,171477'"),
):
    try:
        fdc_ids = [int(x.strip()) for x in ids.split(",") if x.strip()]
    except ValueError:
        raise HTTPException(status_code=400, detail="ids must be comma-separated integers.")
    if not 2 <= len(fdc_ids) <= 5:
        raise HTTPException(status_code=400, detail="Compare 2 to 5 foods (ids=a,b).")
    entries = []
    for fdc_id in fdc_ids:
        f = _get_food_or_404(fdc_id)
        nuts = {n.nutrient_id: n for n in _nutrient_rows(fdc_id)}

        def _amt(nid: int, fallback: Optional[int] = None):
            n = nuts.get(nid) or (nuts.get(fallback) if fallback else None)
            return round(n.amount_per_100g, 2) if n else None

        def _pctv(nid: int, fallback: Optional[int] = None):
            n = nuts.get(nid) or (nuts.get(fallback) if fallback else None)
            return n.pct_daily_value if n else None

        entries.append(
            CompareEntry(
                fdc_id=fdc_id,
                description=f["description"],
                data_type=f["data_type"],
                per_100g={
                    "kcal": _amt(N_KCAL),
                    "protein_g": _amt(N_PROTEIN),
                    "fat_g": _amt(N_FAT),
                    "carbs_g": _amt(N_CARBS),
                    "fiber_g": _amt(N_FIBER),
                    "sugars_g": _amt(N_SUGARS, N_SUGARS_NLEA),
                    "sodium_mg": _amt(N_SODIUM),
                },
                pct_daily_value={
                    "kcal": _pctv(N_KCAL),
                    "protein_g": _pctv(N_PROTEIN),
                    "fat_g": _pctv(N_FAT),
                    "carbs_g": _pctv(N_CARBS),
                    "fiber_g": _pctv(N_FIBER),
                    "sugars_g": _pctv(N_SUGARS, N_SUGARS_NLEA),
                    "sodium_mg": _pctv(N_SODIUM),
                },
            )
        )
    return entries


@app.get(
    "/v1/foods/{fdc_id}",
    response_model=FoodDetail,
    dependencies=[Depends(require_api_key)],
    summary="Food nutrient profile",
    description=(
        "Full per-100g nutrient profile with % Daily Values, household portions, "
        "and (for branded foods) label serving info. " + DISCLAIMER
    ),
)
def food_detail(fdc_id: int):
    f = _get_food_or_404(fdc_id)
    portions = [
        Portion(description=r["portion_description"], gram_weight=r["gram_weight"])
        for r in _db.execute(
            "SELECT portion_description, gram_weight FROM food_portions"
            " WHERE food_fdc_id = ? ORDER BY gram_weight",
            (fdc_id,),
        ).fetchall()
    ]
    return FoodDetail(
        fdc_id=f["fdc_id"],
        description=f["description"],
        data_type=f["data_type"],
        brand_owner=f["brand_owner"],
        brand_name=f["brand_name"],
        gtin_upc=f["gtin_upc"],
        serving_size=f["serving_size"],
        serving_size_unit=f["serving_size_unit"],
        household_serving=f["household_serving"],
        nutrients=_nutrient_rows(fdc_id),
        portions=portions,
        note=BRANDED_NOTE if f["data_type"] == "branded" else None,
    )


_KEY_MACROS = [
    ("kcal", N_KCAL),
    ("protein_g", N_PROTEIN),
    ("fat_g", N_FAT),
    ("carbs_g", N_CARBS),
    ("fiber_g", N_FIBER),
    ("sugars_g", N_SUGARS),
    ("sodium_mg", N_SODIUM),
]


def _scaled_nutrients(fdc_id: int, grams: float) -> dict[int, dict]:
    """All nutrients for a food, scaled to `grams` (amounts per 100g in DB)."""
    factor = grams / 100.0
    rows = _db.execute(
        "SELECT n.id, n.name, n.unit_name, fn.amount, d.daily_value, d.dv_unit"
        " FROM food_nutrients fn JOIN nutrients n ON n.id = fn.nutrient_id"
        " LEFT JOIN daily_values d ON d.nutrient_id = n.id"
        " WHERE fn.food_fdc_id = ?",
        (fdc_id,),
    ).fetchall()
    out: dict[int, dict] = {}
    for r in rows:
        scaled = r["amount"] * factor
        # Sugars fallback: prefer 2000 (Sugars, Total), else 1063 (NLEA)
        out[r["id"]] = {
            "name": r["name"],
            "unit": r["unit_name"],
            "amount": scaled,
            "daily_value": r["daily_value"],
            "dv_unit": r["dv_unit"],
        }
    return out


@app.post(
    "/v1/meals/estimate",
    response_model=MealEstimateResponse,
    dependencies=[Depends(require_api_key)],
    summary="Estimate a meal's nutrition",
    description=(
        "Sum nutrients across a list of {fdc_id, grams} items. Returns macro "
        "summary, full nutrient totals, and a per-item breakdown. " + DISCLAIMER
    ),
)
def meals_estimate(body: MealEstimateRequest):
    foods = {}
    for item in body.items:
        foods[item.fdc_id] = _get_food_or_404(item.fdc_id)

    totals: dict[int, dict] = {}
    item_results = []
    for item in body.items:
        scaled = _scaled_nutrients(item.fdc_id, item.grams)
        for nid, nut in scaled.items():
            t = totals.setdefault(
                nid,
                {"name": nut["name"], "unit": nut["unit"], "amount": 0.0,
                 "daily_value": nut["daily_value"], "dv_unit": nut["dv_unit"]},
            )
            t["amount"] += nut["amount"]
        get = lambda nid, fb=None: round(scaled.get(nid, {}).get("amount", 0.0)
                                         if nid in scaled else (scaled.get(fb, {}).get("amount", 0.0) if fb else 0.0), 2)
        item_results.append(
            MealItemResult(
                fdc_id=item.fdc_id,
                description=foods[item.fdc_id]["description"],
                grams=item.grams,
                kcal=get(N_KCAL),
                protein_g=get(N_PROTEIN),
                fat_g=get(N_FAT),
                carbs_g=get(N_CARBS),
            )
        )

    def _total(nid: int, fallback: Optional[int] = None) -> Optional[float]:
        if nid in totals:
            return round(totals[nid]["amount"], 2)
        if fallback is not None and fallback in totals:
            return round(totals[fallback]["amount"], 2)
        return None

    summary = {
        "kcal": _total(N_KCAL),
        "protein_g": _total(N_PROTEIN),
        "fat_g": _total(N_FAT),
        "carbs_g": _total(N_CARBS),
        "fiber_g": _total(N_FIBER),
        "sugars_g": _total(N_SUGARS, N_SUGARS_NLEA),
        "sodium_mg": _total(N_SODIUM),
    }
    totals_list = [
        NutrientAmount(
            nutrient_id=nid,
            name=t["name"],
            unit=t["unit"],
            amount_per_100g=round(t["amount"], 3),
            daily_value=t["daily_value"],
            dv_unit=t["dv_unit"],
            pct_daily_value=_pct(t["amount"], t["unit"], t["daily_value"], t["dv_unit"]),
        )
        for nid, t in sorted(totals.items())
    ]
    return MealEstimateResponse(summary=summary, totals=totals_list, items=item_results)


@app.get(
    "/v1/nutrients",
    response_model=list[NutrientInfo],
    dependencies=[Depends(require_api_key)],
    summary="Nutrient catalog",
    description="All nutrients with units and FDA daily reference values. " + DISCLAIMER,
)
def nutrients():
    rows = _db.execute(
        "SELECT n.id, n.name, n.unit_name, n.nutrient_nbr, d.daily_value, d.dv_unit"
        " FROM nutrients n LEFT JOIN daily_values d ON d.nutrient_id = n.id"
        " ORDER BY n.id"
    ).fetchall()
    return [
        NutrientInfo(
            nutrient_id=r["id"],
            name=r["name"],
            unit=r["unit_name"],
            nutrient_nbr=r["nutrient_nbr"],
            daily_value=r["daily_value"],
            dv_unit=r["dv_unit"],
        )
        for r in rows
    ]


@app.get(
    "/v1/brands/search",
    response_model=list[BrandHit],
    dependencies=[Depends(require_api_key)],
    summary="Search brand owners",
    description=(
        "Distinct branded-food brand owners matching the query, with food "
        "counts. " + DISCLAIMER
    ),
)
def brands_search(
    q: str = Query(..., description="Brand name text, e.g. 'kellogg'"),
    limit: int = Query(default=20, ge=1, le=50),
):
    q = (q or "").strip()
    if not q:
        raise HTTPException(status_code=400, detail="Query parameter 'q' is required.")
    rows = _db.execute(
        "SELECT brand_owner, COUNT(*) AS c FROM foods"
        " WHERE data_type = 'branded' AND brand_owner LIKE ?"
        " GROUP BY brand_owner ORDER BY c DESC LIMIT ?",
        (f"%{q}%", limit),
    ).fetchall()
    return [BrandHit(brand_owner=r["brand_owner"], food_count=r["c"]) for r in rows]


@app.get(
    "/health",
    summary="Liveness probe (no auth)",
    description="Service liveness. No API key required.",
)
def health():
    total = _db.execute("SELECT COUNT(*) AS c FROM foods").fetchone()["c"]
    return {"status": "ok", "version": "1.0.0", "foods": total}


# ---------------------------------------------------------------------------
# Public pages: landing, privacy policy, terms of service (no auth).
# ---------------------------------------------------------------------------
_PAGE_CSS = """
body{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
line-height:1.65;color:#1a1a1a;max-width:46rem;margin:0 auto;padding:2rem 1.25rem;}
h1{font-size:2rem;margin-bottom:.25rem;} h2{font-size:1.25rem;margin-top:2rem;}
.tag{color:#555;font-size:1.1rem;margin-top:0;}
table{border-collapse:collapse;width:100%;margin:1rem 0;}
th,td{text-align:left;padding:.5rem .75rem;border-bottom:1px solid #e2e2e2;vertical-align:top;}
code{background:#f4f4f4;padding:.15rem .4rem;border-radius:4px;font-size:.9em;}
pre{background:#f4f4f4;padding:1rem;border-radius:6px;overflow-x:auto;}
a{color:#0b5fff;} .fine{color:#666;font-size:.9rem;margin-top:2.5rem;border-top:1px solid #e2e2e2;padding-top:1rem;}
nav{margin:1.5rem 0;} nav a{margin-right:1.25rem;}
"""

_LANDING_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trophe - A Free Food &amp; Nutrition API</title>
<style>""" + _PAGE_CSS + """</style></head><body>
<h1>Trophe</h1>
<p class="tag">A free food &amp; nutrition API.</p>
<p>Look up nutrition for over 2 million foods from the USDA FoodData Central
database (public domain). Get per-100g nutrient profiles with % Daily Values,
estimate whole meals, and compare foods side by side.</p>
<nav><a href="/docs">Interactive API docs</a><a href="/privacy">Privacy policy</a>
<a href="/terms">Terms of service</a></nav>
<h2>Endpoints</h2>
<table>
<tr><th>Method</th><th>Path</th><th>Description</th></tr>
<tr><td><code>GET</code></td><td><code>/v1/foods/search?q=...</code></td><td>Full-text food search, ranked</td></tr>
<tr><td><code>GET</code></td><td><code>/v1/foods/{fdc_id}</code></td><td>Full nutrient profile for one food</td></tr>
<tr><td><code>POST</code></td><td><code>/v1/meals/estimate</code></td><td>Nutrient totals for a list of foods and gram amounts</td></tr>
<tr><td><code>GET</code></td><td><code>/v1/compare?ids=...</code></td><td>Side-by-side nutrient comparison</td></tr>
<tr><td><code>GET</code></td><td><code>/v1/brands/search?q=...</code></td><td>Search brand owners and brand names</td></tr>
<tr><td><code>GET</code></td><td><code>/v1/nutrients</code></td><td>Nutrient catalog with Daily Values</td></tr>
<tr><td><code>GET</code></td><td><code>/health</code></td><td>Service status (no auth required)</td></tr>
</table>
<h2>Authentication</h2>
<p>All <code>/v1/</code> endpoints require a single API key sent in the
<code>X-API-Key</code> request header. Requests without a valid key receive
HTTP 401.</p>
<pre>curl -H "X-API-Key: YOUR_KEY" \\
  "https://YOUR_HOST/v1/foods/search?q=chicken%20breast&amp;limit=3"</pre>
<p class="fine"><strong>Reference data only, not medical or dietary advice.</strong>
Branded values are manufacturer self-reported; verify against the package label.
Data: USDA FoodData Central, CC0 1.0 public domain. Operator contact:
dexiadigi@gmail.com</p>
</body></html>"""

_PRIVACY_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trophe - Privacy Policy</title>
<style>""" + _PAGE_CSS + """</style></head><body>
<h1>Trophe - Privacy Policy</h1>
<p class="fine" style="border:none;margin-top:0;padding-top:0">Effective 2026-09-24. Version 1.0.</p>
<p>Trophe is a free food &amp; nutrition API. This policy explains what
information Trophe handles when you use the API, and what it does not handle.
If you are the operator deploying this API, you are responsible for telling
your API users what extra logging your own deployment performs (operator
contact details are in the Contact section below).</p>
<h2>What Trophe does</h2>
<p>Trophe serves food and nutrient data from the USDA FoodData Central database
(released under CC0 1.0, public domain) via a local read-only SQLite index.
Every response is food or nutrient reference data. No user accounts exist, no
sign-up is possible, and no personal profiles are created. The API is
read-only: nothing you send is stored. Meal estimates are computed per request
and never saved.</p>
<h2>What is collected</h2>
<p><strong>The API key.</strong> Trophe authenticates requests with a single
shared key sent in the <code>X-API-Key</code> header. The key is compared in
constant time against the <code>TROPHE_API_KEY</code> environment variable on
the server. Because one key is shared, a request cannot be tied to a specific
person, account, or device. Trophe has no way to know who you are from the key
alone.</p>
<p><strong>Request logs.</strong> The Trophe application code itself does no
request logging. The server process that runs Trophe (for example, uvicorn)
may write access logs that include the client's IP address and request path.
The operator's intent is that any such logs are kept for no longer than 30
days and then rotated or deleted, and are visible only to the operator for
debugging and abuse prevention. (The hosting platform keeps its own platform
logs under its own policies, outside the operator's control.)</p>
<h2>What is NOT collected</h2>
<ul>
<li><strong>No user accounts.</strong> There is no registration, no login, no
passwords, and no profile data of any kind.</li>
<li><strong>No tracking cookies.</strong> Trophe is an API, not a website, and
sets no cookies.</li>
<li><strong>No analytics on users.</strong> Trophe does not track who you are,
what you look up, or how often. Food searches, nutrient profiles, meal
estimates, and comparisons leave no record on the server.</li>
<li><strong>No payment data.</strong> Trophe is free; no payment information is
ever requested or stored.</li>
<li><strong>No advertising.</strong> No ads are served and no data is collected
for advertising.</li>
</ul>
<h2>Data retention</h2>
<p>Trophe retains nothing about its users. There is no user-data store to
expire or delete. Any operator-kept access logs are intended to be kept no
longer than 30 days and then rotated or deleted, as described above.</p>
<h2>Security</h2>
<p>The API key travels in the <code>X-API-Key</code> request header. Users of a
hosted Trophe instance should confirm with the operator that the API is served
over HTTPS so the key is not transmitted in clear text. The server stores the
expected key in the <code>TROPHE_API_KEY</code> environment variable, never in
the codebase or database.</p>
<h2>Children's privacy</h2>
<p>Trophe collects no personal information from anyone, including children.
There are no accounts and no way to submit personal data through the API.</p>
<h2>International users</h2>
<p>Because Trophe collects no personal data, there is no personal data to
transfer, store, or process across borders. If the operator keeps access logs
containing IP addresses, the operator is responsible for complying with the
applicable rules of the relevant jurisdictions.</p>
<h2>Reference data disclaimer</h2>
<p>Trophe provides reference data only, not medical or dietary advice. Branded
food values are manufacturer self-reported; verify against the package label.</p>
<h2>Changes to this policy</h2>
<p>Material changes to this policy will be posted here with a new effective
date before they take effect.</p>
<h2>Contact</h2>
<p>Operator contact for privacy questions: dexiadigi@gmail.com</p>
</body></html>"""

_TERMS_HTML = """<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Trophe - Terms of Service</title>
<style>""" + _PAGE_CSS + """</style></head><body>
<h1>Trophe - Terms of Service</h1>
<p class="fine" style="border:none;margin-top:0;padding-top:0">Effective 2026-09-24. Version 1.0.</p>
<p>Trophe is a free food &amp; nutrition API. These terms govern your use of the
API and its content.</p>
<h2>1. The service is free</h2>
<p>Trophe is provided free of charge. There are no fees, usage tiers, or
paywalls. If a fee or subscription is introduced in the future, updated terms
will be published and users will be notified before any charge applies.</p>
<h2>2. Data license</h2>
<p>All food and nutrient data served by Trophe comes from the USDA FoodData
Central database, released by the U.S. Department of Agriculture under
<strong>CC0 1.0 Universal (public domain)</strong>. You may copy, redistribute,
republish, and build products on this data, including in commercial products,
with no permission needed and no attribution required. Source:
https://fdc.nal.usda.gov/download-datasets.html</p>
<h2>3. Reference data only</h2>
<p>Trophe provides reference data only, <strong>not medical or dietary
advice</strong>. Do not use Trophe as a substitute for professional medical or
nutritional guidance. Branded food values are manufacturer self-reported;
verify against the package label before relying on them.</p>
<h2>4. Prohibited uses</h2>
<ul>
<li>Do not use the Trophe name or branding in a way that implies endorsement
of your product without written permission.</li>
<li>Do not abuse the service: excessive automated requests that degrade
availability for others, attempts to circumvent access controls, or any use
that violates applicable law.</li>
</ul>
<h2>5. No warranty</h2>
<p>Trophe and all its content are provided "as is" without warranty of any
kind, express or implied, including but not limited to warranties of accuracy,
completeness, merchantability, or fitness for a particular purpose. Food data
is served as compiled from the listed sources; we make no claim that any value
is free of errors.</p>
<h2>6. Limitation of liability</h2>
<p>To the maximum extent permitted by law, the operators of Trophe are not
liable for any indirect, incidental, special, consequential, or punitive
damages arising from your use of the API, even if advised of the possibility
of such damages.</p>
<h2>7. Changes to these terms</h2>
<p>These terms may be updated. Material changes will be posted here with a new
effective date before they take effect. Continued use of the API after the
effective date constitutes acceptance of the updated terms.</p>
<h2>8. Contact</h2>
<p>Questions about these terms: dexiadigi@gmail.com</p>
</body></html>"""


@app.get("/", include_in_schema=False)
def landing():
    return HTMLResponse(_LANDING_HTML)


@app.get("/privacy", include_in_schema=False)
def privacy_policy():
    return HTMLResponse(_PRIVACY_HTML)


@app.get("/terms", include_in_schema=False)
def terms_of_service():
    return HTMLResponse(_TERMS_HTML)
