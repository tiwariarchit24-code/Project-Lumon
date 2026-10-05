# Learned change detection (BTC-B): evaluation (2026-10-05)

Everything the model produces is a **MODEL-GENERATED CANDIDATE CHANGE**.
There is no ground truth for the demo AOI, so **no accuracy figure is
claimed for Lumon's imagery**. The rule-based engine is compared as a
separate baseline, not as truth.

## 1. Is the port faithful? (OSCD test split)

The network was rebuilt for inference (`server/lumon/change_ml/btc_model.py`)
and the checkpoint loaded with every tensor matched (the 24 stored
relative-position indices equal the recomputed ones). It was then run on the
benchmark it was trained for, OSCD's own test split (385 lossless 96 px tile
pairs; `blaz-r/OSCD_RGB_Cropped_96`, CC BY-NC-SA, read in a scratch folder),
with the official test preprocessing (resize to 256, ImageNet normalisation,
sigmoid > 0.5, metrics pooled over all pixels):

| | Lumon port | Published BTC-B oscd96 (3 seeds) |
|---|---|---|
| F1 | **0.540** | 0.537–0.549 |
| Precision | 0.620 | 0.622–0.657 |
| Recall | 0.479 | 0.469–0.472 |
| IoU | 0.370 | 0.367–0.379 |

So the model behaves as published — which on its own benchmark means it
misses about half of the labelled change and about 4 in 10 flagged pixels
are not labelled change.

## 2. Domain gap to Lumon's archive

| | OSCD training tiles | Lumon archive |
|---|---|---|
| Product | Sentinel-2 L1C (top of atmosphere), 2015–2018 | Sentinel-2 L2A (surface reflectance), 2018–2026 |
| Labels | urban change only (buildings, roads) | — |
| RGB medians (R/G/B, 0–255) | 77 / 76 / 83 | 89 / 75 / 59 with Lumon's fixed 0–0.3 stretch |

Blue is notably darker in L2A (atmospheric correction removes haze).
No colour correction was invented to hide this; the existing fixed stretch
is used and the gap is recorded.

## 3. Archive pairs

63 usable scenes share one pixel grid. Lumon refuses pairs with measured
misregistration > 1.0 px, < 50 % pixels clear in both, a different grid, a
wrong date order, or a scene that is not usable. **499 pairs** pass the
stricter "suggested" rule (different years, ≤ 1 calendar month apart in
season, both ≥ 95 % clear, ≤ 0.5 px from a common reference).

## 4. Representative pairs (chosen to be inspectable by eye)

| Pair | Why chosen | Result |
|---|---|---|
| A 2018-01-03 → 2026-01-16 | longest span; the airport was built in between | 16.1 % of clear pixels flagged, 77 regions (+33 below 0.09 ha), 18 s |
| B 2023-01-22 → 2024-01-12 | one year, same season, construction ongoing | 4.1 %, 61 regions (+26); 11 regions flagged "not persistent" |
| C 2026-01-16 → 2026-02-25 | one month, airport complete: little real change expected | 0.3 %, 13 regions (+5) |
| D 2023-05-12 → 2023-10-09 | across the monsoon (5 months seasonal gap): stress test | 12.4 %, 111 regions (+40); 23 regions flagged seasonal vegetation and 23 water |
| E 2021-11-23 → 2022-11-08 | worst-registered scene (1.12 px) | **refused** before inference |
| Control: same image twice (9 tiles) | sanity | 0.000 % flagged, max score 0.000 |

### Successes (visual review)
- A, B: the new terminal building (white roof), aprons, buildings and roads
  of the airport are flagged with high scores (0.9+), and later same-season
  scenes look like the "after" image (persistent).
- C: almost nothing is flagged when little changed.

### False positives
- D: the strongest candidates follow the creek, where a bright white band in
  May 2023 is gone by October — a seasonal/tidal surface, not urban change.
  Pairs across seasons triple the flagged area (12.4 % vs 4.1 %); the pair
  check warns, and region flags mark them.
- Small (< 0.25 ha) regions near the threshold are common in every pair.

### False negatives
- A: large homogeneous changes — the runway strip and levelled ground — are
  largely NOT flagged although the change is obvious to the eye. The model
  was trained on urban structure labels; this matches its published recall.

### Unresolved
- Many regions in A and B lie on construction sites whose state alternates
  (earthworks, stockpiles); whether they are "change" depends on the
  question. Analysts can mark them plausible or reject them.

## 5. Comparison with the rule-based baseline (not ground truth)

| Pair | Rule-based accepted candidates dated inside the pair: covered > 30 % by ML | suppressed: covered > 30 % | ML pixels inside any rule-based polygon |
|---|---|---|---|
| A | 3 of 12 | 24 of 89 | 14.5 % |
| B | 1 of 6 | 4 of 33 | 16.4 % |

The two methods mostly flag different things: the model reacts to urban
structure between two images, the baseline to persistent land-class
transitions in yearly composites. Neither result says which is right.

## 6. Limitations
- Candidates only; never stated as built, demolished or any activity.
- Scores are model outputs (threshold 0.5), not calibrated probabilities.
- Tiles are resized to 256 px and back, which smooths scores: candidates
  extend about one pixel beyond the changed pixels, and a 2 x 2 px change
  becomes a ~12 px region (it is then flagged as near the resolution limit).
- Changes smaller than 9 px (0.09 ha) are counted but not reported.
- L1C/L2A colour gap; urban-only training labels; seasonal and tidal false
  alarms; one small AOI; no NIT Raipur imagery.
- Checkpoint licence unstated; trained on CC BY-NC-SA data: non-commercial
  research use only.
