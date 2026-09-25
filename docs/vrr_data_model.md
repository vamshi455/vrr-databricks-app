# VRR data model — production reference, and how this port maps onto it

Reference for the VRR data model as implemented in **`CreateVRR/src/vrr_sql_builder.sql`**
(the Databricks/RMDE lineage), with the local Postgres equivalent beside each item. The
implementation lives in [`pipeline/schema.sql`](../src/vrr_agent_open/pipeline/schema.sql)
and [`pipeline/build.py`](../src/vrr_agent_open/pipeline/build.py); the physics in
[`core/physics.py`](../src/vrr_agent_open/core/physics.py).

> Correction history: the first cut of this port keyed daily volumes by
> `(pattern, completion, date)` and stored `factor` on the volume row. That is **wrong** —
> pattern membership comes from a *time-windowed* contribution factor, and pressure is
> also time-windowed. The schema and builder below follow the production model.

## 1 · Input tables

Volume source (by uom), keyed by **completion + date** — no pattern column:

| uom | Production table | Columns | Local table |
|---|---|---|---|
| OilField | `TRUSTED_DB.PRODUCTION_VOLUME.PRODUCTION_VOLUMES_DAILY_OILFIELD` | `EMSDB_PROD_COMPLETION_ID`, `PROD_DATE`, `ALLOC_OIL_VOL_STB`, `ALLOC_WATER_VOL_STB`, `ALLOC_WATER_INJ_VOL_STB`, `ALLOC_GAS_VOL_KSCF`, `ALLOC_GAS_INJ_VOL_KSCF` | `vrr_raw.production_volumes_daily` (`uom='OilField'`) |
| Metric | `…PRODUCTION_VOLUMES_DAILY_METRIC` | same with `…_SM3` | same table, `uom='Metric'` |
| HIB only | `UNION INGESTION_DB.EMSDB_CSU.DAILY_VOLUME` | `ID_COMPLETION`, `OPR_DATE`, `Amount_Type`, `OIL_VOLUME`, `WATER_VOLUME`, `GAS_VOLUME` (pivoted; KEPLER wins on overlap) | not ported (single-source demo) |

Reference tables (production: `{source_schema}` = `ENRICHMENT_DB.RMDE_<asset>`):

| Production table | Columns | Role | Local table |
|---|---|---|---|
| `PATTERN` | `ID_PATTERN`, `PATTERN_NAME` | pattern registry | `vrr_raw.pattern` (+ `asset`) |
| completion master | `ID_COMPLETION`, name, UWI | completion registry | `vrr_raw.completion` |
| `PATTERN_CONTRIBUTION_FACTOR` | `ID_COMPLETION`, `ID_PATTERN`, `FACTOR`, `EFFECT_DATE` | completion→pattern allocation (**time-windowed**) | `vrr_raw.pattern_contribution_factor` |
| `PATTERN_PRESSURE` | `ID_PATTERN`, `DATE`, `PRESSURE` | pattern datum pressure (**time-windowed**) | `vrr_raw.pattern_pressure` |
| `COMPLETION_PVT_CHARACTERISTICS` | `ID_COMPLETION`, `TEST_DATE`, `PRESSURE`, `OIL_FORMATION_VOLUME_FACTOR` (Bo), `GAS_FORMATION_VOLUME_FACTOR` (Bg), `WATER_FORMATION_VOLUME_FACTOR` (Bw), `INJECTED_GAS_FORMATION_VOLUME_FACTOR` (Bg_inj), `INJECTED_WATER_FORMATION_VOLUME_FACTOR` (Bw_inj), `SOLUTION_GAS_OIL_RATIO` (Rs), `VOLATIZED_OIL_GAS_RATIO` (Rv) | PVT, interpolated by pressure | `vrr_raw.completion_pvt_characteristics` (short column names, production names in comments) |

## 2 · Transformation pipeline (CTEs / checkpoints)

| CTE | Grain | Produces | Local implementation |
|---|---|---|---|
| `FactorsWithEndDate` | completion · pattern · effect_date | `FACTOR`, `PATTERN_NAME`, `END_DATE` = next `EFFECT_DATE` | `LEAD()` in `build._VOLUME_CONTEXT_SQL` |
| `PressureWithEndDate` | pattern · date | `PRESSURE`, `END_DATE` via `LEAD` | same CTE |
| `DailyVolumes` | completion · day | volumes clamped ≥ 0 via `GREATEST` | same CTE |
| `VolumeContext` | completion · day · pattern | volumes ⋈ factor window ⋈ pressure window; `Amount_Type = Production` if `OIL+WATER+GAS > 0` else `Injection` | same CTE (`amount_type` derived) |
| `UniquePVTNeeds → PVTTestDates → PVTWithEndDate → PVTAnalysis → PVTBounds → CalculatedPVT` | completion · day · pressure | interpolated/extrapolated FVFs; `Bg` rounded to 5 dp | `core.physics.pvt_lookup` (Python, and it **labels the method**: exact / interpolated / extrapolated / closest / none) |
| `VolumeWithPVT` | completion · day · pattern | VolumeContext + CalculatedPVT | materialised as `vrr_curated.completion_contrib` |
| `PatternData` | pattern · period | surface + reservoir volumes, volume-weighted avg FVFs, `HAVING` gate | `build._PATTERN_VRR_INSERT` |
| `FinalData` | pattern · period | + `PRODUCTION_VOLUME_RES_BBL`, `INJECTION_VOLUME_RES_BBL` | same statement |

**Join windows** (half-open, exactly as in production):
`DATE >= EFFECT_DATE AND DATE < END_DATE` for volume↔factor;
`DATE >= p.DATE AND DATE < p.END_DATE` for volume↔pressure;
PVT by pressure bracketing within the `TEST_DATE` window.

## 3 · Core formulas

Per completion, then summed to the pattern. `mult = 1000` (KSCF→SCF) for OilField, else 1.

```
oil_res       = FACTOR · OIL_VOL · Bo             (volatilization variant subtracts an Rs/Rv term)
water_res     = FACTOR · WATER_VOL · Bw
free_gas_res  = ((GAS_VOL·mult) − Rs·OIL_VOL) · FACTOR · Bg   -- producers, OIL_VOL>0; may be negative
water_inj_res = FACTOR · WATER_INJ_VOL · Bw_inj
gas_inj_res   = GAS_INJ_VOL · mult · FACTOR · Bg_inj

PROD_RES = Σ(oil_res + water_res + free_gas_res)
INJ_RES  = Σ(water_inj_res + gas_inj_res)
VRR      = COALESCE(INJ_RES / NULLIF(PROD_RES,0), 0)
```

`HAVING` (row kept if): `(Σ oil·Bo + Σ water·Bw) != 0` **OR** `(Σ water_inj + Σ gas_inj) > 0`.

**Known gap in this port:** the *volatile-oil* variant of `oil_res` (the Rs/Rv term) is
not implemented — `core.physics` computes the non-volatile form, and the synthetic field
sets `Rv = 0`, so the two agree there. Implementing it needs the exact production
expression; flagged rather than guessed.

## 4 · Output columns

Unit suffix `{u}` = BBL/M3; gas KSCF/M3; pressure PSIA/KPA; VRR BBLBBL/M3M3. Production
emits `DAILY_PATTERN_VRR` and `MONTHLY_PATTERN_VRR`; locally that is one table
`vrr_curated.pattern_vrr` with a `grain` column (`daily` | `monthly`), same column set.

| Group | Columns |
|---|---|
| Keys | `{id_alias}` = `CONCAT(ID_PATTERN, id_fmt)`, `RMDE_ID_PATTERN`, `PATTERN_NAME`, `DATE`, `PATTERN_PRESSURE_{PSIA\|KPA}` |
| Surface | `OIL_VOLUME_{u}`, `WATER_VOLUME_{u}`, `WATER_INJ_VOLUME_{u}`, `GAS_VOLUME_{KSCF\|M3}`, `GAS_INJ_VOLUME_{KSCF\|M3}` |
| Reservoir | `RES_OIL_VOLUME_{u}`, `RES_WATER_VOLUME_{u}`, `RES_WATER_INJ_VOLUME_{u}`, `RES_GAS_INJ_VOLUME_{u}`, `RES_FREE_GAS_VOLUME_{u}` |
| Totals + VRR | `RES_PRODUCTION_VOLUME_{u}`, `RES_INJECTION_VOLUME_{u}`, `VRR_{BBLBBL\|M3M3}` |
| PVT (vol-weighted avg) | `AVG_OIL_FVF_*`, `AVG_GAS_FVF_*`, `AVG_WATER_FVF_*`, `AVG_INJ_GAS_FVF_*`, `AVG_INJ_WATER_FVF_*`, `AVG_SOLUTION_GAS_OIL_RATIO_*`, `AVG_VOLATIZED_OIL_GAS_RATIO_*` |
| | `EXECUTION_TIME` |

Local additions used by the agent: `n_completions`, `any_extrapolated` (BOOL_OR over the
low-confidence PVT labels — the flag that vetoes valve recommendations), `run_id`.

**Cumulative** (`vrr_cumulative_calculator.sql` → `vrr_curated.pattern_vrr_cumulative`):
`RES_CUMULATIVE_OIL_PRODUCTION_VOLUME_{u}`, `…_WATER_PRODUCTION_…`,
`…_WATER_INJECTION_…`, `…_GAS_INJECTION_…`, `RES_CUMULATIVE_PRODUCTION_VOLUME_{u}`,
`RES_CUMULATIVE_INJECTION_VOLUME_{u}`, and
`CUMULATIVE_VRR_{BBLBBL|M3M3} = Σinj_res / Σprod_res` running per pattern by date.

## 4a · Allocation is many-to-many and time-windowed

The single most important structural point: `PATTERN_CONTRIBUTION_FACTOR` is an
**allocation table**, not a membership list. One completion can feed several patterns at
once, and its split can change over time. The local seed generates all four shapes so the
join is genuinely exercised:

| Shape | What it looks like | Why it matters |
|---|---|---|
| dedicated | one completion → one pattern, `FACTOR < 1` | partial participation is normal |
| static split | producer → 2–3 patterns, FACTORs summing ≤ 1 | a well between injectors serves both |
| split change | same completion, two windows with different FACTORs | re-allocation after a study |
| migration | window closes on pattern A (`FACTOR = 0`), opens on B | well reassigned mid-life |

Invariant the ingestion must hold (checked by the `DATA_QUALITY` tool): Σ FACTOR across
patterns for a completion in any window ≤ 1. IDs are 16-char uppercase hex surrogate keys
(`core/ids.py`), enforced by a `CHECK` constraint — nothing in the code may assume tidy
identifiers like `PAT-001`.

## 5 · Why this is the agent's lineage backbone

The model maps one-to-one onto the agent's audit chain (design §1–§2):

```
VRR (output)
  └ INJ_RES / PROD_RES                       ← vrr_curated.pattern_vrr
      └ per-completion *_res terms           ← vrr_curated.completion_contrib
          └ FACTOR × volume × FVF(pressure)  ← the four input tables
```

So `VRR_LINEAGE` walks down that chain and `VRR_AUDIT` recomputes it from the inputs —
redoing the windowed joins independently of the builder, which is what makes "is this
number right?" answerable rather than assertable.

## 6 · Deliberate local deviations

| Production | Local | Why |
|---|---|---|
| two output tables (DAILY/MONTHLY) | one `pattern_vrr` + `grain` | same columns; halves the DDL |
| unit suffixes in column names (`_BBL`/`_M3`) | BBL names + `uom` on the raw row | one demo asset; `uom` still drives `mult` |
| HIB `DAILY_VOLUME` union, KEPLER precedence | not ported | single synthetic source |
| PVT ladder in SQL | `core.physics.pvt_lookup` in Python | pure + unit-tested off-DB, and it labels the method (the confidence flag the agent needs) |
| `EXECUTION_TIME` only | `+ run_id` | ties every curated row to the build that wrote it |
