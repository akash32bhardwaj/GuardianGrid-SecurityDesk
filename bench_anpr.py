"""
bench_anpr.py — measure plate accuracy against ground truth.  OCT-179.

    python bench_anpr.py                    # the 91 captures in output/webcam
    python bench_anpr.py --folder some/dir  # anywhere else
    python bench_anpr.py --crops            # also score tight plate crops,
                                            # which is what the tablet sends

GROUND TRUTH IS NOT THE FILENAME.  output/webcam names each capture after
what the July engine read, and about a third of those are wrong -- counting
filename agreement measures self-consistency and calls it accuracy, which is
how this register quoted 77% for three months (OCT-169).  The table below
was established by OPENING the photographs and reading the plates.

Seven vehicles, not twelve: the engine had been inventing new ones.
"""
import argparse
import collections
import sys
import time
from pathlib import Path

TRUTH_BY_PREFIX = {
    "DL3CAB5678": "DL3CAB5678", "DL5CAB5078": "DL3CAB5678",
    "HR98AA7777": "HR98AA7777", "HR9BA4777": "HR98AA7777",
    "RP8AA7777": "HR98AA7777", "HP8AA7777": "HR98AA7777",
    "MH12BP8265": "MH47BP8265", "MH62BP8265": "MH47BP8265",
    "MH17BP8265": "MH47BP8265",
    "RJ11CV0002": "RJ14CV0002", "RJ16CV0002": "RJ14CV0002",
    "RJ16CY0002": "RJ14CV0002", "RJ16CTUUU1": "RJ14CV0002",
    "RJ11CYUUU2": "RJ14CV0002", "RJ16CYUUU2": "RJ14CV0002",
    "ZZ8HG5174": "22BH6517A", "LZ8HG5174": "22BH6517A",
    "UP53OV9006": "UP53DV9006", "UP53OV9005": "UP53DV9006",
    "OF53OV9006": "UP53DV9006",
    "HR26DO5551": "HR26DQ5551", "MHR26DO5554": "HR26DQ5551",
}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--folder", default="output/webcam")
    ap.add_argument("--crops", action="store_true",
                    help="also score tight plate crops (the tablet's input)")
    args = ap.parse_args()

    import cv2
    from detect_image import get_engine, read_plate

    engine = get_engine()
    if engine is None:
        raise SystemExit("[ERROR] ANPR engine unavailable")

    folder = Path(args.folder)
    images = sorted(p for p in folder.iterdir()
                    if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    unknown = sorted({p.stem.split("_")[0].upper() for p in images}
                     - set(TRUTH_BY_PREFIX))
    if unknown:
        print(f"[warn] no ground truth for: {unknown}\n"
              f"       those images are skipped -- add them to TRUTH_BY_PREFIX\n"
              f"       only after LOOKING at them.")
    images = [p for p in images if p.stem.split("_")[0].upper() in TRUTH_BY_PREFIX]
    if not images:
        raise SystemExit(f"no scorable images in {folder}")

    per = collections.defaultdict(lambda: [0, 0, 0])
    errs = collections.Counter()
    t0 = time.time()
    for p in images:
        truth = TRUTH_BY_PREFIX[p.stem.split("_")[0].upper()]
        out = read_plate(p)
        got = (out or {}).get("plate")
        if not got:
            per[truth][2] += 1
        elif got == truth:
            per[truth][0] += 1
        else:
            per[truth][1] += 1
            if len(got) == len(truth):
                for a, b in zip(truth, got):
                    if a != b:
                        errs[f"{a}->{b}"] += 1
    n = len(images)
    ok = sum(v[0] for v in per.values())
    wrong = sum(v[1] for v in per.values())
    ref = sum(v[2] for v in per.values())
    dt = time.time() - t0
    print(f"\n{n} images, {dt/n:.2f}s each")
    print(f"  {ok}/{n} correct = {100*ok/n:.1f}%   wrong {wrong}   refused {ref}")
    for truth, (a, b, c) in sorted(per.items(), key=lambda kv: -sum(kv[1])):
        m = a + b + c
        print(f"    {truth:12} {m:3} reads  {a:3} right {b:3} wrong {c:3} refused"
              f"   {100*a/m:5.1f}%")
    if errs:
        print(f"\n  character substitutions: {dict(errs.most_common(8))}")
    print("\n  Reference, measured 22 Oct on this corpus:")
    print("    EasyOCR alone, as it shipped ........ 56%")
    print("    + plate-trained models, voted ....... 93%")
    print("    + matching against the resident list  95%")
    print("  A number far below that means a reader is missing, not that the")
    print("  photographs got harder -- check the log for 'fast-plate-ocr")
    print("  unavailable', which degrades silently to EasyOCR alone.")


if __name__ == "__main__":
    main()
