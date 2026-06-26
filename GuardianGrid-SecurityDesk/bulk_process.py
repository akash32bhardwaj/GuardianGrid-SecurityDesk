"""
bulk_process.py
---------------
Process an entire folder of vehicle images and export results to CSV + annotated images.

Usage:
    python bulk_process.py --input images/          # process folder
    python bulk_process.py --input images/ --save-annotated   # also save annotated images
    python bulk_process.py --input images/ --csv results.csv  # custom CSV path
    python bulk_process.py --input images/ --workers 4        # parallel processing

Output:
    logs/bulk_results_<timestamp>.csv
    output/annotated/<filename>_annotated.jpg  (if --save-annotated)
"""

import argparse
import csv
import time
import concurrent.futures
from pathlib import Path
from datetime import datetime

import cv2
from tqdm import tqdm

from core.anpr_engine import ANPREngine, PlateResult

SUPPORTED_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".tiff"}


def process_single(args) -> dict:
    """Worker function — process one image and return result dict."""
    engine, img_path, save_annotated, out_dir = args
    path = Path(img_path)
    result: PlateResult = engine.process_image(path)

    row = result.to_dict()
    row["filename"] = path.name
    row["filepath"] = str(path)

    if save_annotated and result.detected:
        out_dir.mkdir(parents=True, exist_ok=True)
        img = cv2.imread(str(path))
        annotated = engine.draw_result(img, result)
        out_path = out_dir / f"{path.stem}_annotated{path.suffix}"
        cv2.imwrite(str(out_path), annotated)
        row["annotated_path"] = str(out_path)
    else:
        row["annotated_path"] = ""

    return row


def bulk_process(
    input_dir: str,
    csv_path: str = None,
    save_annotated: bool = False,
    max_workers: int = 1,
    recursive: bool = False,
):
    input_path = Path(input_dir)
    if not input_path.exists():
        print(f"[ERROR] Input directory not found: {input_dir}")
        return

    # Collect images
    glob_fn = input_path.rglob if recursive else input_path.glob
    images = [p for p in glob_fn("*") if p.suffix.lower() in SUPPORTED_EXTENSIONS]
    if not images:
        print(f"[WARN] No supported images found in {input_dir}")
        return

    print(f"[INFO] Found {len(images)} images.")

    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    csv_out = Path(csv_path) if csv_path else Path(f"logs/bulk_results_{ts}.csv")
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    annot_dir = Path("output/annotated")

    # Single engine instance (EasyOCR is not thread-safe to init in workers)
    engine = ANPREngine(use_gpu=False)
    task_args = [(engine, img, save_annotated, annot_dir) for img in images]

    fieldnames = [
        "filename", "filepath", "detected", "plate_number", "plate_raw",
        "plate_type", "plate_label", "state_code", "state_name",
        "series", "confidence", "notes", "timestamp", "annotated_path"
    ]

    results = []
    start = time.time()

    # Use ProcessPoolExecutor only for workers>1, but EasyOCR state issues mean
    # sequential is safer; we use ThreadPoolExecutor as a progress-friendly wrapper.
    with open(csv_out, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()

        if max_workers <= 1:
            for args in tqdm(task_args, desc="Processing", unit="img"):
                row = process_single(args)
                writer.writerow(row)
                f.flush()
                results.append(row)
        else:
            # ThreadPoolExecutor: safe for I/O-bound stages
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as pool:
                futures = {pool.submit(process_single, a): a for a in task_args}
                for fut in tqdm(concurrent.futures.as_completed(futures),
                                total=len(futures), desc="Processing", unit="img"):
                    try:
                        row = fut.result()
                        writer.writerow(row)
                        f.flush()
                        results.append(row)
                    except Exception as e:
                        print(f"[ERROR] {e}")

    elapsed = time.time() - start
    detected = sum(1 for r in results if r.get("detected"))

    print(f"\n{'='*55}")
    print(f"  Bulk Processing Complete")
    print(f"{'='*55}")
    print(f"  Total images  : {len(images)}")
    print(f"  Plates found  : {detected}  ({detected/len(images)*100:.1f}%)")
    print(f"  Time elapsed  : {elapsed:.1f}s  ({elapsed/len(images):.2f}s/image)")
    print(f"  CSV saved     : {csv_out}")
    if save_annotated:
        print(f"  Annotated imgs: {annot_dir}")
    print(f"{'='*55}\n")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Bulk Indian ANPR folder processor")
    parser.add_argument("--input",           required=True, help="Input folder path")
    parser.add_argument("--csv",             default=None,  help="Output CSV file path")
    parser.add_argument("--save-annotated",  action="store_true", help="Save annotated images")
    parser.add_argument("--workers",         type=int, default=1, help="Parallel workers (default 1)")
    parser.add_argument("--recursive",       action="store_true", help="Search subfolders too")
    args = parser.parse_args()

    bulk_process(
        input_dir=args.input,
        csv_path=args.csv,
        save_annotated=args.save_annotated,
        max_workers=args.workers,
        recursive=args.recursive,
    )
