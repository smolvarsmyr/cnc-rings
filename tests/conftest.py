import os
import sys
import tempfile
from pathlib import Path

# Keep presets written by the tests out of the working copy. Set before generate_sheets is imported.
os.environ["CNC_RINGS_DATA"] = tempfile.mkdtemp(prefix="cnc-rings-test-")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
