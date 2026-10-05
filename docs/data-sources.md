# Data sources

Every external dataset, API and asset used by Project Lumon, why it was
chosen, what it is (and is not) good for, and the data-quality issues we
found while integrating it. Checksums and retrieval times of the copies
staged on a machine are in the generated [SOURCE_MANIFEST.md](SOURCE_MANIFEST.md).

Source policy: prefer official agencies and well-documented open datasets;
check licence, attribution, rate limits, India coverage and whether data
may be cached offline. A source listed in a public API directory is not
trusted just for being listed.

## India operating boundary

The operating area is **India land + India Exclusive Economic Zone**
(`config/geo/operating-area.json`). Nothing is described as "official"
unless its publisher says so.

| Dataset | Provider | Licence | Notes |
|---|---|---|---|
| Country polygon, India point of view (`ne_10m_admin_0_countries_ind`) | Natural Earth | Public domain | Natural Earth publishes boundaries as seen by several countries' official points of view; this is the India edition. Small-scale (1:10m) cartographic data, not survey-grade. |
| India EEZ, 200 NM (2 features: mainland, Andaman & Nicobar) | Marine Regions / VLIZ | CC BY 4.0 | Marine Regions states the data has no legal value. They ask that files are not redistributed, so the copy stays in `data/`. |
| States and union territories (36) | geoBoundaries, from DataMeet / ECI | CC BY 2.5 IN | Metadata says "2011", but the file already includes Ladakh (2019) and the merged Dadra and Nagar Haveli and Daman and Diu (2020). |
| Districts (735) | geoBoundaries, from Pathways Data / lgdirectory.gov.in | ODbL 1.0 | 2021 layout. The file has 735 features although the metadata says 736. |
| Populated places (214 in India) | Natural Earth | Public domain | Used for the gazetteer and city labels. |

## OSINT sources

| Source | Category | Evidence type | Licence / terms | Status |
|---|---|---|---|---|
| USGS FDSN earthquakes | Disasters | VERIFIED RECORD if reviewed, else OBSERVED | Public domain | Running |
| NASA EONET natural events | Disasters / environment / weather | OBSERVED | Public domain | Running (see issues) |
| GDACS alerts | Disasters / weather | INFERRED (model-based alert level) | Free with attribution | Running |
| Open-Meteo current weather (40 largest cities) | Weather | INFERRED (model analysis, not station readings) | CC BY 4.0; free tier non-commercial | Running |
| OpenSky Network state vectors | Aviation | OBSERVED (self-reported ADS-B) | Non-commercial research use, attribution | Running; coverage over India incomplete |
| IODA outage events (India, country level) | Connectivity | INFERRED (statistical anomaly) | Georgia Tech copyright; check terms | Running; no location (national scope) |
| NOAA SWPC planetary Kp | Space | OBSERVED | Public domain | Running; global scope |
| Launch Library 2 (Sriharikota, Kulasekarapattinam) | Space | VERIFIED RECORD (dates may be estimates; precision shown) | Free tier, 15 requests/hour | Running |
| CelesTrak GP elements (resource, weather, stations) | Space | INFERRED (SGP4 positions at download time) | Free; avoid excessive downloads | Running |
| NASA FIRMS VIIRS active fires | Environment | OBSERVED | NASA open data | Implemented; needs free `NASA_FIRMS_MAP_KEY` |
| OurAirports | Aviation | GIS-DERIVED | Public domain | Running |
| Natural Earth ports | Maritime | GIS-DERIVED | Public domain | Running (major ports only) |
| WRI Global Power Plant Database v1.3 | Infrastructure | GIS-DERIVED | CC BY 4.0 | Running; database last updated 2021 |
| Natural Earth rivers, lakes, roads, railways | Reference / infrastructure | GIS-DERIVED | Public domain | Running; 1:10m generalised |
| aisstream.io AIS vessels | Maritime | — | Key required | Planned (needs WebSocket client and key) |
| OpenAQ air quality | Environment | — | Key required | Planned |

### Evidence types

- **VERIFIED RECORD**: an authoritative record reviewed by its publisher.
- **OBSERVED**: a direct sensor observation or a curated observation.
- **INFERRED**: model output, statistical detection or computed values.
- **GIS-DERIVED**: reference geography and infrastructure datasets.
- **UNVERIFIABLE**: used when Lumon cannot establish a claim.

Lumon never turns a source's own quality flag (for example FIRMS
`confidence`) into a Lumon confidence number. Such flags are shown as
`source_confidence`.

## Satellite imagery

| Item | Value |
|---|---|
| Collection | Sentinel-2 Level-2A (`sentinel-2-l2a`) |
| Access | Earth Search by Element 84 STAC API; COGs on AWS Open Data (`sentinel-cogs`) |
| Licence | Copernicus Sentinel data terms (free, full and open) |
| Demo AOI | `demo-01`, Navi Mumbai (Ulwe / Panvel creek), about 4.9 × 4.6 km |
| Bands kept | B02, B03, B04, B08 (10 m), B11, SCL (20 m → 10 m nearest) |
| Selection | One scene per month with ≤ 10 % scene cloud, preferring the tile that fully covers the AOI |

## Data-quality issues found during integration

These were found by checking the data, not assumed.

1. **EONET flood polygons have latitude and longitude swapped.** Flood
   events relayed from GDACS arrive as `[lat, lon]`. Lumon does not "fix"
   them. A record is quarantined as *suspected latitude/longitude swap*
   when it is outside India as given, inside India when swapped, **and**
   its own text names India. The text condition matters: without it, real
   ports in Scandinavia would look like swapped points in the Indian Ocean.
2. **Earth Search `boa_offset_applied` semantics.** For processing baseline
   04.00+ products flagged `true`, the stored numbers have *already* had
   ESA's +1000 offset removed: the darkest pixels are near DN 0, which is
   impossible if the offset were present. The one product flagged `false`
   (2022-02-01) does contain the offset. Lumon decides each scene's offset
   from the flag, then checks it against the data with a dark-pixel test.
   Two 2025 scenes flagged `false` contradicted their data and were
   quarantined.
3. **Undocumented processing baseline `00.01`.** 55 catalogue entries over
   the AOI carry this value, which is not an ESA baseline. Those scenes are
   quarantined rather than guessed.
4. **Two overlapping Sentinel-2 tiles.** The AOI lies where tiles 43QBA
   and 43QBB overlap. 43QBA covers only about 20 % of it, so full-coverage
   tiles are preferred.
5. **District count mismatch** (735 vs 736) and **states metadata year**
   (see the boundary table above).

## Fonts, logo, models

- Map label glyphs: Noto Sans Regular (SIL OFL 1.1), PBF ranges bundled in
  `public/fonts/`.
- Logo: supplied by the project owner, `public/assets/project-lumon-logo.png`.
- Models: **none staged.** Semantic text-to-image search and a learned
  land-cover classifier need models. The system uses documented
  non-neural fallbacks and says so in the UI.
