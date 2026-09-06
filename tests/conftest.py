"""Put src/ on the path once for every test module, so a single file
runs on its own (previously only the alphabetically earlier modules
inserted it, and later ones rode on that)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
