"""
detect_image.py
---------------
Quick test: run ANPR on a single image and print results.

Usage:
    python detect_image.py car.jpg
    python detect_image.py car.jpg --show          # pop up annotated window
    python detect_image.py car.jpg --save          # save annotated image
"""

import argparse
import json
from pathlib import Path
import cv2
from core.anpr_engine import ANPREngine


def main():
    parser = argparse.ArgumentParser(description="Run ANPR on a single image")
    parser.add_argument("image",  help="Path to vehicle image")
    parser.add_argument("--show", action="store_true", help="Display annotated image")
    parser.add_argument("--save", action="store_true", help="Save annotated image")
    args = parser.parse_args()

    path = Path(args.image)
    if not path.exists():
        print(f"[ERROR] File not found: {path}")
        return

    engine = ANPREngine()
    result = engine.process_image(path)

    print("\n" + "="*50)
    print("  ANPR Result")
    print("="*50)
    print(json.dumps(result.to_dict(), indent=2, ensure_ascii=False))
    print("="*50 + "\n")

    if args.show or args.save:
        img = cv2.imread(str(path))
        annotated = engine.draw_result(img, result)

        if args.save:
            out = path.parent / f"{path.stem}_annotated{path.suffix}"
            cv2.imwrite(str(out), annotated)
            print(f"[SAVED] {out}")

        if args.show:
            cv2.imshow("ANPR Result", annotated)
            print("[INFO] Press any key to close window.")
            cv2.waitKey(0)
            cv2.destroyAllWindows()


if __name__ == "__main__":
    main()
