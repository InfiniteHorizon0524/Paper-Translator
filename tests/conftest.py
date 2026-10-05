import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / ".deps"))
os.environ["PAPERREADER_DATA_DIR"] = str(ROOT / ".test-data" / "unit")
