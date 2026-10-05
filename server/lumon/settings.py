"""
Project-wide settings for the Lumon backend.

Everything that depends on the environment (folders, operating mode, API
keys) is read here, once, so the rest of the code never has to guess.

Settings come from environment variables. For convenience we also read a
`.env.local` file in the project root (ignored by Git) using a tiny parser,
so no extra dependency is needed.
"""

import os
from pathlib import Path

# The repository root: server/lumon/settings.py -> three levels up.
ROOT_DIR = Path(__file__).resolve().parents[2]

# Folder layout. `data/` is ignored by Git: it holds downloaded snapshots,
# staged boundaries, imagery and the local database.
DATA_DIR = ROOT_DIR / "data"
CONFIG_DIR = ROOT_DIR / "config"
RAW_DIR = DATA_DIR / "raw"  # untouched downloads, exactly as received
BOUNDARY_DIR = DATA_DIR / "boundaries"  # processed boundary GeoJSON
REFERENCE_DIR = DATA_DIR / "reference"  # processed static layers (airports, ports, ...)
SNAPSHOT_DIR = DATA_DIR / "snapshots"  # raw API responses from source refreshes
IMAGERY_DIR = DATA_DIR / "imagery"  # staged satellite imagery
EXPORT_DIR = DATA_DIR / "exports"
DATABASE_PATH = DATA_DIR / "lumon.db"


def _read_env_file(path: Path) -> dict:
    """
    Read simple KEY=VALUE lines from a file such as `.env.local`.

    Blank lines and lines starting with '#' are ignored. Values are not
    printed anywhere, because they may contain API keys.
    Returns a dictionary of the values found (empty if the file is missing).
    """
    values = {}
    if not path.exists():
        return values
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip()
    return values


# Real environment variables win over the file, so a single command can
# override a setting, e.g. `LUMON_MODE=airgapped python -m lumon.cli ...`.
_file_values = _read_env_file(ROOT_DIR / ".env.local")


def get_setting(name: str, default: str = "") -> str:
    """Return a setting from the environment, then .env.local, then the default."""
    return os.environ.get(name) or _file_values.get(name) or default


def operating_mode() -> str:
    """
    Return "connected" or "airgapped".

    AIR-GAPPED is the safe default: in that mode the backend makes no
    external network requests at all (see lumon/net.py). CONNECTED mode is
    only needed to download or refresh data.
    """
    mode = get_setting("LUMON_MODE", "airgapped").lower()
    return "connected" if mode == "connected" else "airgapped"


def ensure_data_folders() -> None:
    """Create the data folders if they do not exist yet."""
    for folder in [DATA_DIR, RAW_DIR, BOUNDARY_DIR, REFERENCE_DIR, SNAPSHOT_DIR, IMAGERY_DIR, EXPORT_DIR]:
        folder.mkdir(parents=True, exist_ok=True)
