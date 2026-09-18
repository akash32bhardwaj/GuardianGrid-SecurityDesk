"""
resident_db.py — GuardianGrid Resident Vehicle Database
---------------------------------------------------------
Manages resident vehicle data for residential societies.

Features:
  - Import residents from Excel sheet
  - Lookup plate number → resident details instantly
  - Mark vehicles as known/unknown/blacklisted
  - Export resident data back to Excel

Excel format expected:
  | plate_number | resident_name | flat_number | block | phone | vehicle_type | vehicle_model | notes |
"""

import json
import csv
import logging
import os
import shutil
from pathlib import Path
from datetime import datetime
from dataclasses import dataclass, field, asdict
from typing import Optional

logger = logging.getLogger(__name__)

# ── Try importing openpyxl for Excel support ─────────────────────
try:
    import openpyxl
    EXCEL_AVAILABLE = True
except ImportError:
    EXCEL_AVAILABLE = False

# ── Data paths ───────────────────────────────────────────────────
# OCT-76. This was `Path("data/residents.json")` — a RELATIVE path, and
# that was a data-loss bug waiting for the first paying client.
#
# The app runs with its working directory at /app, so the registry was
# written to /app/data/residents.json: inside the container image, not on
# the /data volume. Images are replaced on every deploy. A society would
# import its entire vehicle list, and the next time you shipped anything
# the file would be gone — every plate silently back to UNKNOWN, with no
# error and nothing to restore from.
#
# The same relative path also resolved somewhere else whenever a script
# ran from another directory: the nightly reseed uses `-w /data`, which is
# why an empty /data/data/ appeared on 17 Sep. Two candidate registries,
# neither authoritative.
#
# It now resolves once, absolutely, onto the mounted volume, and can be
# pointed elsewhere for a site that stores data somewhere unusual.
DB_FILE = Path(os.environ.get("GG_RESIDENT_DB") or "/data/residents.json")
DB_FILE.parent.mkdir(parents=True, exist_ok=True)

# Anything written under the old relative path still belongs to a client.
# Move it across once, before a deploy takes the image layer with it.
_LEGACY_PATHS = (
    Path("data/residents.json"),          # relative to whatever cwd was
    Path("/app/data/residents.json"),     # where the app actually wrote it
    Path("/data/data/residents.json"),    # where a script with -w /data would
)

def _migrate_legacy_registry():
    if DB_FILE.exists():
        return
    for old_path in _LEGACY_PATHS:
        try:
            if old_path.resolve() == DB_FILE.resolve():
                continue
            if old_path.exists() and old_path.stat().st_size > 2:
                shutil.copy2(old_path, DB_FILE)
                print(f"[RESIDENT-DB] migrated registry {old_path} -> {DB_FILE}")
                return
        except OSError as exc:
            print(f"[RESIDENT-DB] could not migrate {old_path}: {exc}")

_migrate_legacy_registry()


# ── Resident dataclass ───────────────────────────────────────────
@dataclass
class Resident:
    plate_number:   str
    resident_name:  str
    flat_number:    str
    block:          str        = ""
    phone:          str        = ""
    vehicle_type:   str        = "Car"
    vehicle_model:  str        = ""
    vehicle_color:  str        = ""
    status:         str        = "KNOWN"   # KNOWN / BLACKLISTED / VISITOR
    notes:          str        = ""
    added_on:       str        = field(
        default_factory=lambda: datetime.now().strftime("%Y-%m-%d")
    )

    def to_dict(self):
        return asdict(self)

    @property
    def display_name(self):
        parts = [self.resident_name]
        if self.flat_number:
            parts.append(f"Flat {self.flat_number}")
        if self.block:
            parts.append(f"Block {self.block}")
        return " · ".join(parts)


# ── Database class ───────────────────────────────────────────────
class ResidentDatabase:
    def __init__(self):
        self._db: dict = {}    # plate_number -> Resident
        self._load()

    # ── Persistence ──────────────────────────────────────────
    def _load(self):
        if DB_FILE.exists():
            try:
                with open(DB_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for plate, record in data.items():
                    self._db[plate] = Resident(**record)
                logger.info(f"Loaded {len(self._db)} residents from database.")
            except Exception as e:
                logger.error(f"Could not load resident DB: {e}")

    def _save(self):
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump(
                {plate: r.to_dict() for plate, r in self._db.items()},
                f, indent=2, ensure_ascii=False
            )

    # ── Core operations ───────────────────────────────────────
    @staticmethod
    def _normalise(plate: str) -> str:
        return "".join(ch for ch in (plate or "").upper() if ch.isalnum())

    def lookup(self, plate: str) -> Optional[Resident]:
        """Look up a plate number. Returns Resident or None.

        OCT-78. This used to fall back to a two-way substring test:

            if clean in stored_plate or stored_plate in clean:
                return resident

        which identifies the wrong person in both directions. A stored
        plate contained anywhere in the queried one matched, so a stranger
        at the gate could come back carrying a resident's name and flat
        number — and a guard reading a name does not see a guess. It also
        handed one resident's details to whoever happened to be standing
        at the barrier, which is a DPDP problem as much as a security one.

        Identification is exact. For as-you-type behaviour, or for an OCR
        misread, use suggest() or fuzzy_lookup() and present the result as
        something the guard must confirm, never as a match.
        """
        clean = self._normalise(plate)
        return self._db.get(clean)

    def suggest(self, fragment: str, limit: int = 5) -> list:
        """Plates containing `fragment`, for search and type-ahead.

        Deliberately separate from lookup(): these are candidates for a
        human to choose between, not an identification. Returns dicts so a
        caller cannot mistake one for a confirmed Resident.
        """
        clean = self._normalise(fragment)
        if len(clean) < 3:
            return []
        out = []
        for stored_plate, resident in self._db.items():
            if clean in stored_plate:
                d = resident.to_dict()
                d["match"] = "suggestion"
                out.append(d)
                if len(out) >= limit:
                    break
        return out

    def fuzzy_lookup(self, plate: str, max_distance: int = 1) -> Optional[Resident]:
        """
        Find a resident whose plate is within `max_distance` edits
        of the given plate (same length only — handles single
        character OCR mix-ups like 0/O/D, B/8, etc).
        Returns the Resident if exactly one close match is found.
        """
        clean = plate.upper().replace(" ", "")
        if clean in self._db:
            return self._db[clean]

        candidates = []
        for stored_plate, resident in self._db.items():
            if len(stored_plate) != len(clean):
                continue
            dist = sum(1 for a, b in zip(clean, stored_plate) if a != b)
            if dist <= max_distance:
                candidates.append((dist, resident))

        if len(candidates) == 1:
            return candidates[0][1]
        if len(candidates) > 1:
            # Multiple close matches — too ambiguous, don't guess
            candidates.sort(key=lambda c: c[0])
            if candidates[0][0] < candidates[1][0]:
                return candidates[0][1]
        return None

    def add(self, resident: Resident):
        """Add or update a resident."""
        clean = resident.plate_number.upper().replace(" ", "")
        resident.plate_number = clean
        self._db[clean] = resident
        self._save()

    def remove(self, plate: str) -> bool:
        clean = plate.upper().replace(" ", "")
        if clean in self._db:
            del self._db[clean]
            self._save()
            return True
        return False

    def get_all(self) -> list:
        return [r.to_dict() for r in self._db.values()]

    def count(self) -> int:
        return len(self._db)

    def blacklist(self, plate: str, reason: str = ""):
        r = self.lookup(plate)
        if r:
            r.status = "BLACKLISTED"
            r.notes  = reason
            self._save()

    # ── Excel import ──────────────────────────────────────────
    def import_from_excel(self, filepath: str) -> dict:
        """
        Import residents from Excel file.
        Returns {"imported": N, "skipped": N, "errors": [...]}
        """
        if not EXCEL_AVAILABLE:
            return {"error": "openpyxl not installed. Run: pip install openpyxl"}

        path = Path(filepath)
        if not path.exists():
            return {"error": f"File not found: {filepath}"}

        try:
            wb   = openpyxl.load_workbook(path)
            ws   = wb.active
            rows = list(ws.iter_rows(values_only=True))
        except Exception as e:
            return {"error": f"Cannot read Excel file: {e}"}

        if not rows:
            return {"error": "Excel file is empty"}

        # Auto-detect headers
        headers = [str(h).lower().strip() if h else "" for h in rows[0]]

        # Header mapping — flexible column names
        col_map = {
            "plate_number":  ["plate", "plate_number", "vehicle_number",
                               "registration", "reg_no", "number_plate"],
            "resident_name": ["name", "resident_name", "owner", "resident",
                               "full_name", "owner_name"],
            "flat_number":   ["flat", "flat_number", "flat_no", "unit",
                               "apartment", "house_no", "unit_no"],
            "block":         ["block", "tower", "wing", "sector"],
            "phone":         ["phone", "mobile", "contact", "phone_number",
                               "mobile_number"],
            "vehicle_type":  ["type", "vehicle_type", "vehicle"],
            "vehicle_model": ["model", "vehicle_model", "car_model"],
            "vehicle_color": ["color", "colour", "vehicle_color"],
            "notes":         ["notes", "remarks", "comment"],
        }

        # Find column indices
        col_idx = {}
        for field_name, aliases in col_map.items():
            for alias in aliases:
                if alias in headers:
                    col_idx[field_name] = headers.index(alias)
                    break

        if "plate_number" not in col_idx:
            return {
                "error": "Could not find plate number column. "
                         "Please name it 'plate' or 'plate_number'."
            }

        imported = 0
        skipped  = 0
        errors   = []

        for row_num, row in enumerate(rows[1:], start=2):
            try:
                plate = str(row[col_idx["plate_number"]] or "").strip()
                if not plate or plate.lower() == "none":
                    skipped += 1
                    continue

                def get_col(field, default=""):
                    idx = col_idx.get(field)
                    if idx is not None and idx < len(row):
                        val = row[idx]
                        return str(val).strip() if val is not None else default
                    return default

                resident = Resident(
                    plate_number  = plate.upper().replace(" ", ""),
                    resident_name = get_col("resident_name", "Unknown"),
                    flat_number   = get_col("flat_number"),
                    block         = get_col("block"),
                    phone         = get_col("phone"),
                    vehicle_type  = get_col("vehicle_type", "Car"),
                    vehicle_model = get_col("vehicle_model"),
                    vehicle_color = get_col("vehicle_color"),
                    notes         = get_col("notes"),
                    status        = "KNOWN",
                )
                self.add(resident)
                imported += 1

            except Exception as e:
                errors.append(f"Row {row_num}: {e}")
                skipped += 1

        return {
            "imported": imported,
            "skipped":  skipped,
            "total":    len(rows) - 1,
            "errors":   errors,
        }

    # ── Excel export ──────────────────────────────────────────
    def export_to_excel(self, filepath: str = None):
        # Defaulted to a relative "data/..." path, which lands wherever the
        # caller happened to be started from — the same fault as OCT-76.
        filepath = filepath or str(DB_FILE.parent / "residents_export.xlsx")
        if not EXCEL_AVAILABLE:
            return {"error": "openpyxl not installed"}

        wb = openpyxl.Workbook()
        ws = wb.active
        ws.title = "Residents"

        headers = [
            "Plate Number", "Resident Name", "Flat Number", "Block",
            "Phone", "Vehicle Type", "Vehicle Model",
            "Vehicle Color", "Status", "Notes", "Added On"
        ]
        ws.append(headers)

        # Style header row
        from openpyxl.styles import Font, PatternFill
        for cell in ws[1]:
            cell.font = Font(bold=True, color="FFFFFF")
            cell.fill = PatternFill("solid", fgColor="1F4E79")

        for r in self._db.values():
            ws.append([
                r.plate_number, r.resident_name, r.flat_number,
                r.block, r.phone, r.vehicle_type, r.vehicle_model,
                r.vehicle_color, r.status, r.notes, r.added_on
            ])

        # Auto-width columns
        for col in ws.columns:
            max_len = max(len(str(c.value or "")) for c in col)
            ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

        wb.save(filepath)
        return {"exported": len(self._db), "file": filepath}

    # ── CSV import (fallback if no Excel) ─────────────────────
    def import_from_csv(self, filepath: str) -> dict:
        path = Path(filepath)
        if not path.exists():
            return {"error": f"File not found: {filepath}"}

        imported = 0
        skipped  = 0

        with open(path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                plate = (row.get("plate_number") or
                         row.get("plate") or "").strip().upper()
                if not plate:
                    skipped += 1
                    continue
                resident = Resident(
                    plate_number  = plate.replace(" ", ""),
                    resident_name = row.get("resident_name", "Unknown"),
                    flat_number   = row.get("flat_number", ""),
                    block         = row.get("block", ""),
                    phone         = row.get("phone", ""),
                    vehicle_type  = row.get("vehicle_type", "Car"),
                    vehicle_model = row.get("vehicle_model", ""),
                    vehicle_color = row.get("vehicle_color", ""),
                    notes         = row.get("notes", ""),
                )
                self.add(resident)
                imported += 1

        return {"imported": imported, "skipped": skipped}


# ── Global instance ───────────────────────────────────────────────
db = ResidentDatabase()


# ── Standalone test ───────────────────────────────────────────────
#
# This used to call db.add() on the module-level singleton, which writes
# straight through to the real registry. Running `python resident_db.py`
# on a server — to check the module imports, say — silently inserted
# three fabricated residents into a client's vehicle list, one of them
# BLACKLISTED with "Unpaid dues" against their name. A self-test must not
# be able to touch live data, so it gets its own file.
if __name__ == "__main__":
    import tempfile

    print("Testing Resident Database...")
    DB_FILE = Path(tempfile.mkdtemp(prefix="resident_db_test_")) / "residents.json"
    print(f"(self-test writes to {DB_FILE}, never the live registry)")
    db = ResidentDatabase()

    # Add sample residents
    db.add(Resident(
        plate_number="PB08EY5332", resident_name="Akash Singh",
        flat_number="302", block="B", phone="98765-43210",
        vehicle_type="Car", vehicle_model="Hyundai i20",
        vehicle_color="White"
    ))
    db.add(Resident(
        plate_number="PB10AB2025", resident_name="Gurpreet Kaur",
        flat_number="105", block="A", phone="98100-12345",
        vehicle_type="Car", vehicle_model="Maruti Swift",
        vehicle_color="Silver"
    ))
    db.add(Resident(
        plate_number="DL3CAB5678", resident_name="Rajesh Kumar",
        flat_number="201", block="C", phone="99999-88888",
        vehicle_type="Car", vehicle_model="Honda City",
        vehicle_color="Black", status="BLACKLISTED",
        notes="Unpaid dues"
    ))

    print(f"\nTotal residents: {db.count()}")

    # Test lookup
    result = db.lookup("PB08EY5332")
    if result:
        print(f"\nLookup PB08EY5332:")
        print(f"  Name  : {result.resident_name}")
        print(f"  Flat  : {result.display_name}")
        print(f"  Phone : {result.phone}")
        print(f"  Car   : {result.vehicle_color} {result.vehicle_model}")
        print(f"  Status: {result.status}")

    print("\nDatabase test complete! ✅")
    print(f"Data saved to: {DB_FILE}")
