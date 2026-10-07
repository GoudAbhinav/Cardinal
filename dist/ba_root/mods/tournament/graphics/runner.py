"""runs and generates the graphical images and sends to discord webhooks"""
import threading
import subprocess
import shutil
import os
import json
from server.storage import MODS_DIR

GRAPHICS_DIR = MODS_DIR / "tournament" / "graphics"

def run(data: dict) -> None:
    """runs the script"""
    uv = shutil.which("uv")

    # run the generation script using uv
    script_path = str(GRAPHICS_DIR / "generator.py")
    env = os.environ.copy()
    env["PYTHONPATH"] = str(MODS_DIR)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    process = subprocess.Popen([uv, "run", script_path, json.dumps(data)], env=env)
    # reap the process when it ends so it doesn't stay as a zombie.
    threading.Thread(target=process.wait, daemon=True).start()
