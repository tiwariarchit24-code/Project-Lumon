"""
Small true-colour PNG chips of staged scenes, used as before/after
evidence in the analyst panel.

The PNG is written with the standard library (zlib + struct), so no
imaging library is needed. Colours use a fixed reflectance stretch
(0 to 0.3) for EVERY scene, so brightness differences between chips are
real differences in the data, not different contrast settings.
"""

import struct
import zlib

import numpy as np
import rasterio
from rasterio.warp import transform as warp_transform
from rasterio.windows import from_bounds


STRETCH_MAX = 0.3  # reflectance mapped to full brightness


def _png_bytes(rgb: np.ndarray) -> bytes:
    """Encode an (H, W, 3) uint8 array as a PNG file."""
    height, width, _ = rgb.shape
    raw = b"".join(b"\x00" + rgb[row].tobytes() for row in range(height))  # filter type 0 per row

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)  # 8-bit RGB
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def render_chip(path: str, offset: float, bbox: list[float] | None = None, upscale: int = 1) -> bytes:
    """
    Render a true-colour PNG of a scene, optionally cut to a lon/lat bbox.
    Cloud/no-data pixels keep their real colours (clouds look white) so the
    analyst sees exactly what the sensor saw.
    """
    with rasterio.open(path) as source:
        window = None
        if bbox:
            xs, ys = warp_transform("EPSG:4326", source.crs, [bbox[0], bbox[2]], [bbox[1], bbox[3]])
            window = from_bounds(min(xs), min(ys), max(xs), max(ys), transform=source.transform).round_offsets().round_lengths()
        red, green, blue = (source.read(band, window=window, boundless=True, fill_value=0).astype("float32") for band in (3, 2, 1))
    channels = []
    for dn in (red, green, blue):
        reflectance = dn * 0.0001 + offset
        channels.append(np.clip(reflectance / STRETCH_MAX * 255, 0, 255).astype("uint8"))
    rgb = np.dstack(channels)
    if upscale > 1:
        rgb = rgb.repeat(upscale, axis=0).repeat(upscale, axis=1)
    return _png_bytes(rgb)
