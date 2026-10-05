"""
Sentinel-2 Level-2A specifics: which bands we use, how to convert stored
numbers to reflectance, and what the Scene Classification Layer (SCL) means.

RADIOMETRIC NOTE (important for change detection):
Sentinel-2 L2A files store reflectance as whole numbers ("DN"). Since ESA
processing baseline 04.00 (January 2022) ESA adds +1000 to every DN, so
reflectance = DN * 0.0001 - 0.1 for those products. Older baselines have
no offset: reflectance = DN * 0.0001.

The Earth Search catalogue we use re-publishes the data. Its property
`earthsearch:boa_offset_applied = true` means the +1000 has ALREADY been
removed from the stored numbers. We verified this on the demo AOI: in such
scenes the darkest 1 % of pixels have DN close to 0, which would be
impossible if +1000 were still included, while the scene flagged `false`
(2022-02-01) has its darkest pixels above DN 1000.

So the offset we apply is:
    baseline < 04.00                          -> 0.0
    baseline >= 04.00 and offset applied      -> 0.0   (already removed)
    baseline >= 04.00 and offset NOT applied  -> -0.1  (remove it ourselves)
    anything else (unknown baseline / flag)   -> None  (quarantine)

and every scene is additionally checked with a dark-pixel test
(`dark_pixel_check`). If the metadata rule and the data disagree, the scene
is quarantined instead of being guessed at. Getting this wrong would make
every image after 2022 look 0.1 darker or brighter and create thousands of
fake "changes".
"""

# STAC asset key -> short band name. 10 m bands first, then 20 m bands.
BANDS = {
    "blue": "B02",
    "green": "B03",
    "red": "B04",
    "nir": "B08",
    "swir16": "B11",
    "scl": "SCL",
}
REFLECTANCE_BANDS = ["B02", "B03", "B04", "B08", "B11"]

# SCL values that mean "do not trust this pixel".
SCL_NAMES = {
    0: "no data", 1: "saturated or defective", 2: "dark area pixels", 3: "cloud shadow",
    4: "vegetation", 5: "not vegetated", 6: "water", 7: "unclassified",
    8: "cloud (medium probability)", 9: "cloud (high probability)", 10: "thin cirrus", 11: "snow or ice",
}
SCL_INVALID = {0, 1, 3, 8, 9, 10, 11}


def baseline_number(text: str | None) -> float | None:
    """'05.10' -> 5.1. Returns None when missing or not a number."""
    try:
        return float(text)
    except (TypeError, ValueError):
        return None


def reflectance_offset(processing_baseline: str | None, boa_offset_applied) -> float | None:
    """
    The offset to add after scaling DN by 0.0001 (see the module notes).
    Returns None when the metadata does not let us decide safely.
    ESA L2A baselines start at 02.xx; values such as '00.01' seen in some
    catalogue entries are not documented ESA baselines, so we refuse them.
    """
    number = baseline_number(processing_baseline)
    if number is None or number < 2.0:
        return None
    if number < 4.0:
        return 0.0
    if boa_offset_applied is True:
        return 0.0
    if boa_offset_applied is False:
        return -0.1
    return None


def dark_pixel_check(blue_dn, valid_mask) -> str:
    """
    Look at the darkest 1 % of clear blue-band pixels (water and shadowed
    vegetation are very dark in blue). Returns:
      "no-offset-in-data"  darkest DN below 600: values cannot include +1000
      "offset-in-data"     darkest DN above 1000: values include +1000
      "inconclusive"       in between (e.g. an AOI with no dark surfaces)
    """
    import numpy as np  # local import: this module is otherwise dependency-free

    values = blue_dn[valid_mask & (blue_dn > 0)]
    if values.size < 100:
        return "inconclusive"
    darkest = float(np.percentile(values, 1))
    if darkest < 600:
        return "no-offset-in-data"
    if darkest > 1000:
        return "offset-in-data"
    return "inconclusive"


def check_consistency(offset: float, dark_result: str) -> str | None:
    """
    Compare the metadata-derived offset with the dark-pixel test.
    Returns None when they agree (or the test is inconclusive), otherwise
    a quarantine reason.
    """
    if dark_result == "no-offset-in-data" and offset != 0.0:
        return "radiometric metadata says +1000 offset present, but the data's darkest pixels say it is not"
    if dark_result == "offset-in-data" and offset == 0.0:
        return "radiometric metadata says no offset, but the data's darkest pixels include +1000"
    return None
