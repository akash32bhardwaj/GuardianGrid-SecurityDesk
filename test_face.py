import cv2
from face_engine import recognise
cap = cv2.VideoCapture(0)
print("Looking for faces. Press Q to quit.")
while True:
    ret, frame = cap.read()
    if not ret: break
    annotated, results = recognise(frame)
    for r in results:
        print(r["name"], r["flat"], "score:", r["score"])
    cv2.imshow("Face test", annotated)
    if cv2.waitKey(1) & 0xFF == ord("q"): break
cap.release(); cv2.destroyAllWindows()