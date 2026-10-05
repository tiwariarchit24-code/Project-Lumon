# Change detection

How Lumon turns a stack of Sentinel-2 images into reviewable change events,
and what it deliberately refuses to claim. Code: `server/lumon/imagery/`
and `server/lumon/change/engine.py`.

## 1. Per-image tile observations (`imagery/features.py`)

For each usable scene:

1. **Reflectance** = DN × 0.0001 + offset. The offset is decided and
   data-checked at ingest (`scenes.radiometric_offset`; see
   `imagery/sentinel2.py`).
2. **Masking.** Pixels flagged by the scene's Scene Classification Layer
   as cloud, cirrus, shadow, snow, saturated or no data are invalid.
3. **Indices.** NDVI (vegetation), MNDWI (open water), NDBI (built/bare).
4. **Pixel classes.** Water (MNDWI > 0.1 and NDVI < 0.2), vegetation
   (NDVI ≥ 0.35), otherwise open land.
5. **Tiles.** 100 m × 100 m (10 × 10 pixels). Each tile stores its share
   of clear pixels, its class shares and a feature vector. Tiles with less
   than 80 % clear pixels get no class: they are masked, not guessed.

Results are cached in `tile_observations`, so a new scene only processes
that scene.

## 2. Why yearly dry-season composites

On the demo AOI, single-image classes swing with the monsoon (vegetation
covers about 600 tiles in the dry season and about 1,400 just after the
rains) and with the tide (visible water in creeks varies a lot from image
to image). Comparing images directly would report thousands of seasonal
"changes".

So for every tile and every year, Lumon takes the clear January–May
observations (the dry, least cloudy season) and uses the **median** class
shares. One hazy image, one high tide or one late harvest cannot move a
median of several images. A composite needs at least 2 clear looks.

Composite classes: **water**, **vegetation**, and open land split into
**built** or **soil** by colour. Built means greyer than the AOI's typical
open land **in the same year** (red/blue ratio at least 0.10 below the
year's median). Comparing within a year cancels haze and
atmospheric-correction differences between years. This colour rule is a
**heuristic that has not been validated against ground truth**.

## 3. Persistent transitions

A tile changes from class A to class B when:

- at least 2 composite years show A before, and at least 2 show B after;
- each side agrees with its class in at least 75 % of its years;
- the first year after the split already shows B, and the latest year
  still shows B.

## 4. Candidate events

Neighbouring tiles (8-connected) with the same A → B transition, starting
within one year of each other, form one candidate. Area is reported as a
range: the changed-pixel share (minimum) up to the full tile area
(maximum).

| Transition | Change class | Direction |
|---|---|---|
| anything → water | water-extent | expansion |
| water → anything | water-extent | contraction |
| anything → built | construction (road-development if long and thin) | appearance |
| vegetation → soil | clearance | disappearance |
| other | other | appearance / disappearance |

## 5. Earliest supported observation

For each event, Lumon reports:

- **last clear before**: the last clear dry-season image still showing A;
- **earliest supported after**: the first clear dry-season image showing B.

The change happened somewhere in between. Missing observations are never
interpolated. For example, construction seen first on 2023-01-22 with the
last clear "before" image on 2022-04-02 means the change happened during
that window, which includes the monsoon gap.

## 6. False-alarm gates

Each candidate is tested. Failing **any** gate suppresses it, and the
reasons stay visible (Changes → SHOW SUPPRESSED).

| Gate | Test | Guards against |
|---|---|---|
| 1 quality | ≥ 2 composite years before and after, each from ≥ 2 clear looks | cloud, shadow, missing data |
| 2 geometry | class share rose ≥ 30 points (or red/blue fell ≥ 0.25 for soil → built) | sub-pixel mis-registration, which can only affect a thin edge of a tile |
| 3 radiometric | new state seen by ≥ 2 Sentinel-2 satellites; all scenes passed the dark-pixel offset check | single-sensor calibration or processing artefacts |
| 4 season | compared within the same season of different years; new state lasts ≥ 2 dry seasons | monsoon, crop cycles |
| 5 size/shape | ≥ 2 tiles (2 ha); elongated shapes are relabelled as roads, not rejected | speckle, single noisy tiles |
| 6 persistence | new class holds in ≥ 75 % of later years, including the latest | temporary disturbance |

Snow, view-angle and illumination effects are handled only indirectly
(SCL snow mask, dry-season composites, multi-satellite agreement).
Explicit BRDF or terrain illumination correction is not implemented.

## 7. Confidence

The number shown is an **UNCALIBRATED SCORE, not a probability**:

```
score = mean( agreement before,
              agreement after,
              min(1, years the new class has held / 3),
              min(1, rise in class share / 0.6) )      # or colour drop / 0.5 for soil -> built
```

It ranks the review queue. It becomes a probability only after
calibration on analyst-labelled examples ([evaluation.md](evaluation.md)).

## 8. Results on the demo AOI (measured on 2026-10-04)

63 usable scenes (2018-01 to 2026-10), composite years 2018–2026,
2,160 tiles. 220 tiles had a persistent transition. They grouped into
109 candidates: **12 accepted, 97 suppressed** (most by the size and
geometry gates). The largest accepted construction candidate (6 ha, soil →
built) was checked by eye against its before/after chips. April 2022 shows
graded laterite; January 2023 shows the first grey pavement; 2026 imagery
shows terminal and apron structures. This is one visual check, **not** a
validation of the method.

## 9. Similarity ("more like these")

No neural embedding model is staged. Each tile is described by an
explainable vector: band means, NDVI/MNDWI/NDBI, texture, plus how each
changed between the tile's earliest and latest clear observation. Search
standardises the features, averages the positive examples, moves away
from rejected examples, ranks by cosine similarity, removes results within
300 m of a better one, and explains each match by its closest features.
Results are **spectral and change-history similarity, not semantic
labels**.
