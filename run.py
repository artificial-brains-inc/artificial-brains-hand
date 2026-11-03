"""Run the hand example from this checkout; no hardware is opened on import."""
from pathlib import Path
import sys

if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from aerohand_control.app import main
    raise SystemExit(main())
