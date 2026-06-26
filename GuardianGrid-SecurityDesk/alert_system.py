"""
alert_system.py
---------------
Popup alert + sound when a new vehicle plate is detected.
Also handles Entry/Exit tracking using the same camera.

Logic:
  - First time a plate is seen = ENTRY
  - Same plate seen again after 5 minutes = EXIT
  - Same plate seen within 5 minutes = ignored (avoid duplicate alerts)
"""

import tkinter as tk
from tkinter import font as tkfont
import threading
import time
import csv
import winsound
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass
from typing import Optional

# ── Config ──────────────────────────────────────────────────────
EXIT_THRESHOLD_MINUTES = 5      # same plate after this = EXIT
LOG_FILE = Path("logs/entry_exit_log.csv")
POPUP_DURATION_SECONDS = 6      # how long popup stays on screen

# ── Colors ──────────────────────────────────────────────────────
COLOR_ENTRY = "#00c896"          # green
COLOR_EXIT  = "#ff6b6b"          # red
COLOR_BG    = "#0d1117"
COLOR_CARD  = "#161b22"
COLOR_TEXT  = "#e6edf3"
COLOR_MUTED = "#8b949e"


@dataclass
class VehicleEvent:
    plate: str
    event_type: str          # "ENTRY" or "EXIT"
    timestamp: datetime
    state_name: str
    plate_label: str
    confidence: float
    snapshot_path: str = ""


class AlertSystem:
    def __init__(self):
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

        # plate_number -> last seen datetime
        self._last_seen: dict = {}
        # plate_number -> entry datetime (for duration calc)
        self._entry_times: dict = {}

        self._popup_lock = threading.Lock()
        self._active_popup: Optional[tk.Tk] = None

        # Init CSV log
        if not LOG_FILE.exists():
            with open(LOG_FILE, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=[
                    "event_type", "plate_number", "state_name",
                    "plate_label", "confidence", "timestamp",
                    "duration_minutes", "snapshot_path"
                ])
                writer.writeheader()

    # ----------------------------------------------------------------
    def process_detection(self, plate: str, state_name: str = "",
                          plate_label: str = "", confidence: float = 0.0,
                          snapshot_path: str = "") -> Optional[VehicleEvent]:
        """
        Call this every time a plate is detected.
        Returns a VehicleEvent if it's a new ENTRY or EXIT, else None.
        """
        if not plate:
            return None

        now = datetime.now()
        last = self._last_seen.get(plate)

        # Seen within debounce window — ignore
        if last and (now - last).total_seconds() < 30:
            return None

        self._last_seen[plate] = now

        # Determine ENTRY or EXIT
        entry_time = self._entry_times.get(plate)
        if entry_time is None:
            # Never seen before — ENTRY
            event_type = "ENTRY"
            self._entry_times[plate] = now
            duration = 0.0
        else:
            minutes_since_entry = (now - entry_time).total_seconds() / 60
            if minutes_since_entry >= EXIT_THRESHOLD_MINUTES:
                # Seen again after threshold — EXIT
                event_type = "EXIT"
                duration   = round(minutes_since_entry, 1)
                # Reset for next entry
                del self._entry_times[plate]
            else:
                # Seen again too soon — still inside, ignore
                return None

        event = VehicleEvent(
            plate         = plate,
            event_type    = event_type,
            timestamp     = now,
            state_name    = state_name,
            plate_label   = plate_label,
            confidence    = confidence,
            snapshot_path = snapshot_path,
        )

        # Log to CSV
        self._log_event(event, duration if event_type == "EXIT" else 0.0)

        # Show popup + play sound in background thread
        threading.Thread(
            target=self._show_alert,
            args=(event,),
            daemon=True
        ).start()

        return event

    # ----------------------------------------------------------------
    def _log_event(self, event: VehicleEvent, duration: float):
        with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=[
                "event_type", "plate_number", "state_name",
                "plate_label", "confidence", "timestamp",
                "duration_minutes", "snapshot_path"
            ])
            writer.writerow({
                "event_type":       event.event_type,
                "plate_number":     event.plate,
                "state_name":       event.state_name,
                "plate_label":      event.plate_label,
                "confidence":       round(event.confidence, 1),
                "timestamp":        event.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "duration_minutes": duration,
                "snapshot_path":    event.snapshot_path,
            })

    # ----------------------------------------------------------------
    def _show_alert(self, event: VehicleEvent):
        """Show a popup window with plate info and play a beep sound."""
        # Play sound
        try:
            if event.event_type == "ENTRY":
                # Two short beeps for entry
                winsound.Beep(1000, 200)
                time.sleep(0.1)
                winsound.Beep(1000, 200)
            else:
                # One long lower beep for exit
                winsound.Beep(600, 500)
        except Exception:
            pass

        # Show popup (must run in main thread on Windows)
        with self._popup_lock:
            try:
                self._create_popup(event)
            except Exception as e:
                print(f"[ALERT] {event.event_type}: {event.plate} "
                      f"({event.state_name}) — popup failed: {e}")

    # ----------------------------------------------------------------
    def _create_popup(self, event: VehicleEvent):
        root = tk.Tk()
        root.overrideredirect(True)        # no title bar
        root.attributes("-topmost", True)  # always on top
        root.attributes("-alpha", 0.95)
        root.configure(bg=COLOR_BG)

        # ── Position: bottom-right corner ──
        sw = root.winfo_screenwidth()
        sh = root.winfo_screenheight()
        w, h = 360, 180
        x = sw - w - 20
        y = sh - h - 60
        root.geometry(f"{w}x{h}+{x}+{y}")

        is_entry = event.event_type == "ENTRY"
        accent   = COLOR_ENTRY if is_entry else COLOR_EXIT
        icon     = "🚗  VEHICLE ENTRY" if is_entry else "🚗  VEHICLE EXIT"

        # ── Top color bar ──
        top_bar = tk.Frame(root, bg=accent, height=4)
        top_bar.pack(fill="x")

        # ── Main card ──
        card = tk.Frame(root, bg=COLOR_CARD, padx=20, pady=14)
        card.pack(fill="both", expand=True, padx=2, pady=(0,2))

        # Event type label
        tk.Label(card, text=icon,
                 bg=COLOR_CARD, fg=accent,
                 font=("Segoe UI", 11, "bold")).pack(anchor="w")

        # Plate number — big
        tk.Label(card, text=event.plate,
                 bg=COLOR_CARD, fg=COLOR_TEXT,
                 font=("Courier New", 22, "bold")).pack(anchor="w", pady=(4,0))

        # State + type
        detail = f"{event.state_name}  ·  {event.plate_label}  ·  {event.confidence:.0f}% conf"
        tk.Label(card, text=detail,
                 bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w")

        # Time
        tk.Label(card, text=event.timestamp.strftime("%d %b %Y  %H:%M:%S"),
                 bg=COLOR_CARD, fg=COLOR_MUTED,
                 font=("Segoe UI", 9)).pack(anchor="w", pady=(2,0))

        # Close button
        tk.Button(card, text="✕  Dismiss",
                  bg=COLOR_BG, fg=COLOR_MUTED,
                  font=("Segoe UI", 8), relief="flat",
                  cursor="hand2",
                  command=root.destroy).pack(anchor="e", pady=(6,0))

        # Auto-close after N seconds
        root.after(POPUP_DURATION_SECONDS * 1000, root.destroy)

        # Slide-in animation
        for alpha in range(1, 11):
            root.attributes("-alpha", alpha / 10)
            root.update()
            time.sleep(0.02)

        root.mainloop()


# ── Standalone test ─────────────────────────────────────────────
if __name__ == "__main__":
    print("Testing alert system...")
    alerts = AlertSystem()

    # Simulate an ENTRY
    alerts.process_detection(
        plate         = "PB 08 EY 5332",
        state_name    = "Punjab",
        plate_label   = "Private",
        confidence    = 78.0,
        snapshot_path = ""
    )
    time.sleep(3)

    # Simulate another vehicle
    alerts.process_detection(
        plate         = "MH 12 AB 1234",
        state_name    = "Maharashtra",
        plate_label   = "Private",
        confidence    = 91.0,
    )
    time.sleep(8)
    print("Test complete! Check logs/entry_exit_log.csv")
