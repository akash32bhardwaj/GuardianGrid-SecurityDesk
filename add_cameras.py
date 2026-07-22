#!/usr/bin/env python3
"""
add_cameras.py - the ONE on-site step for a Defender Octa demo.

Walks the operator through entering the client's RTSP camera links and
writes them into site_config.json safely (with a backup). No JSON editing,
no worrying about escaping passwords - just paste each stream URL.

Run it with:  Add RTSP Cameras.bat   (double-click)
"""

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

CONFIG = Path(__file__).with_name("site_config.json")
DEFAULT_MODE = "person+vehicle"


def load_config():
    if not CONFIG.exists():
        print(f"[ERROR] {CONFIG.name} not found next to this tool.")
        sys.exit(1)
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        print(f"[ERROR] {CONFIG.name} is not valid JSON: {e}")
        sys.exit(1)


def backup_config():
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    bak = CONFIG.with_name(f"site_config.backup-{stamp}.json")
    shutil.copy2(CONFIG, bak)
    return bak


def show_current(cams):
    if not cams:
        print("  (none configured yet)")
        return
    for i, c in enumerate(cams, 1):
        print(f"  {i}. {c.get('name','(unnamed)')}  ->  {c.get('url','')}")


def prompt(text, default=None):
    suffix = f" [{default}]" if default else ""
    val = input(f"{text}{suffix}: ").strip()
    return val or (default or "")


def enter_cameras():
    cams = []
    print("\nEnter each camera. Paste the full RTSP URL, then give it a name.")
    print("Press ENTER on a blank URL when you're done.\n")
    n = 1
    while True:
        url = input(f"Camera {n} - RTSP URL (blank to finish): ").strip()
        if not url:
            break
        if not url.lower().startswith("rtsp://"):
            ok = input("  That doesn't start with rtsp:// - add it anyway? (y/N): ").strip().lower()
            if ok != "y":
                continue
        name = prompt("  Name for this camera", f"Camera {n}")
        mode = prompt("  AI mode", DEFAULT_MODE)
        cams.append({"name": name, "url": url, "ai_mode": mode})
        print(f"  Added: {name}\n")
        n += 1
    return cams


def main():
    cfg = load_config()
    current = cfg.get("rtsp_cameras", [])

    print("=" * 55)
    print("  DEFENDER OCTA - Add RTSP Cameras")
    print("=" * 55)
    print("\nCurrent RTSP cameras in site_config.json:")
    show_current(current)

    print("\nWhat do you want to do?")
    print("  [R] Replace all with new cameras   (recommended for a new site)")
    print("  [A] Add more to the existing list")
    print("  [K] Keep as-is and quit")
    choice = input("Choose R / A / K: ").strip().lower()

    if choice == "k":
        print("No changes made.")
        return
    if choice not in ("r", "a"):
        print("Nothing chosen - no changes made.")
        return

    new_cams = enter_cameras()
    if not new_cams and choice == "r":
        confirm = input("You entered no cameras. Clear the list entirely? (y/N): ").strip().lower()
        if confirm != "y":
            print("No changes made.")
            return

    cfg["rtsp_cameras"] = new_cams if choice == "r" else (current + new_cams)

    bak = backup_config()
    CONFIG.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    print("\n" + "=" * 55)
    print("  Saved. Final camera list:")
    show_current(cfg["rtsp_cameras"])
    print(f"\n  Backup of the old config: {bak.name}")
    print("  Now (re)start Defender Octa to load the cameras.")
    print("=" * 55)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nCancelled - no changes made.")
