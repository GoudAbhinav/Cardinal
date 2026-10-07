"""utilities loader plugin."""

import importlib
import traceback
from pathlib import Path


def load():
    """automatically imports utility files in the directory."""
    package_dir = Path(__file__).parent
    for file in sorted(package_dir.glob("*.py")):
        if file.stem == "__init__":
            continue
        module_name = f"{__package__}.{file.stem}"
        try:
            importlib.import_module(module_name)
        except Exception:
            # one broken utility should not stop the others from loading.
            print(f"⚠️ Failed to load utility file {file.stem}")
            print(traceback.format_exc())
