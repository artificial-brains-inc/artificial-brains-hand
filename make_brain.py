"""Regenerate the public JSON and literal Python design from the neural generator."""
from pathlib import Path
import json
from aerohand_control.design import export_design

if __name__ == "__main__":
    print(json.dumps(export_design(Path(__file__).resolve().parent), indent=2))
