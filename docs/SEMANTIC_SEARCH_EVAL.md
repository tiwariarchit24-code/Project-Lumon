# Semantic image search: evaluation (2026-10-04)

Model: RemoteCLIP ViT-B-32 (`remoteclip:60014e395d930a3f`), CPU.
Archive: demo AOI (Navi Mumbai, Ulwe / Panvel creek), 63 usable Sentinel-2 L2A
scenes 2018–2026, cut into 2,118 chips (2.24 km and 1.12 km); 24 chips
excluded (>10 % cloud/shadow/no-data), 18 scenes excluded by quality status
(15 unrecognised processing baseline, 2 radiometric-offset conflicts,
1 unusable).

**No accuracy metric is reported.** There is no ground truth for this AOI.
What follows is (1) an automatic agreement check against simple spectral
indices where one exists, and (2) a visual review of the top and bottom
chips. Both can be redone: `npm run lumon -- semantic-eval` and the queries
in `config/ai/semantic_eval.json`.

## 1. Spectral-index agreement (automatic)

For water and vegetation queries, the mean share of water pixels
(MNDWI > 0) or green-vegetation pixels (NDVI > 0.4) is compared in the
top-10 ranked chips, the whole archive, and the bottom-10. The indices are
rules, not truth; this measures agreement only.

| Query | Index | Top 10 | Archive | Bottom 10 | Agrees |
|---|---|---|---|---|---|
| river | water | 0.272 | 0.091 | 0.016 | yes |
| water body | water | 0.321 | 0.093 | 0.005 | yes |
| dense green vegetation | vegetation | 0.578 | 0.190 | 0.304 | **no** (top is greener than average, but bottom is too) |
| green vegetation farmland | vegetation | 0.297 | 0.206 | 0.321 | **no** |

## 2. Visual review (top 8 vs bottom 8, done by eye)

| Query | Top chips | Bottom chips | Verdict |
|---|---|---|---|
| airport runway | all 8 show the airport runway / terminal under construction (2023–2026) | creek and scrub before construction (2019–2021) | relevant above irrelevant |
| river | creek and open water | airport, bare land | relevant above irrelevant |
| bare soil construction site | all 8 show cleared land and earthworks of the airport site (2022–2025) | vegetation, creek, villages | relevant above irrelevant |
| dense residential buildings | chips with some built-up corners, but mostly vegetation, plus the airport terminal | bare land, creek | mixed |
| green vegetation farmland | bare orange quarry / fill (2018–2019) ranked high | green vegetated chips ranked low | **failure** |
| snow covered mountains (negative control) | bright white patches (surfaces clipped by the fixed stretch; not identified) | dark creek / vegetation | **ranked results for something absent** |
| ships in a harbour (negative control) | open water and bright white patches; no ship is visible at 10 m (top score 0.310) | bare soil, villages | **ranked results for something absent** (matches water, not ships) |

## Failure cases and limitations

- **Scores do not indicate presence.** The negative controls get top scores
  (0.316, 0.310) close to genuine matches (0.33–0.36). Scores are only
  comparable within one query.
- **Vegetation / farmland queries fail** on this archive: bright bare soil
  ranks above green fields.
- **Resolution mismatch:** RemoteCLIP was trained mainly on sub-metre to
  few-metre imagery; Sentinel-2 is 10 m. Buildings, ships and roads are a
  few pixels wide, so those queries match texture and colour, not objects.
- **Repeated places:** the same location on many dates fills the top ranks
  (e.g. the runway chip across 2023–2026). Results are not de-duplicated by
  place.
- **Colour stretch:** a fixed 0–0.3 reflectance stretch keeps dates
  comparable but clips very bright surfaces to white.
- **Coverage:** one ~22 km² AOI. No NIT Raipur imagery is staged; pilot
  image searches return NOT STAGED.

## 3. Image-to-image similarity (added 2026-10-05)

Same model and the same cached chip embeddings; the query is an example
chip's own embedding, so no model inference runs at query time (about
10 ms over 500–2,000 chips). Only chips of the example's size are compared.
Scopes: OTHER PLACES (look-alike places, best date per place), SAME PLACE
(every date at the example's window), ALL.

| Check | Result |
|---|---|
| Same place over time: runway chip (2025-10-13, 2.24 km), 62 other dates | Similarity follows the airport construction: mean score 0.68–0.69 for 2018–2020, 0.72 (2021), 0.76 (2022), 0.84–0.87 for 2023–2026. Rank vs year: Spearman 0.81. Top 8 dates are 2023–2026, bottom 8 are 2018–2020. |
| Other places, water example (1.12 km creek chip, 25 % water by MNDWI) | Of 24 other places, the top 10 average 0.218 water vs 0.141 across all places and 0.057 in the bottom 10: agrees with the water index. |
| Other places, vegetation example (1.12 km, 35 % NDVI > 0.4) | Top 10: 0.202, all places 0.201, bottom 10: 0.184: **no agreement** (same weakness as the vegetation text queries). |
| Visual review, runway example, other places | #1–#5 are other parts of the airport complex (terminal, apron, earthworks); #6–#8 (scores 0.79–0.81) are creek/mangrove with construction fill: less alike, ranked lower. Only 8 places exist at 2.24 km in this AOI. |
| Visual review, water example, other places | Top results are creek chips but also **hazy dates** (2018-12, 2019-03) like the hazy example: atmospheric haze and date-specific colour influence similarity, not only land cover. |

Not done: no accuracy figure (no ground truth); results are within one
small AOI, so "other places" has few candidates (8 at 2.24 km, 24 at 1.12 km).
