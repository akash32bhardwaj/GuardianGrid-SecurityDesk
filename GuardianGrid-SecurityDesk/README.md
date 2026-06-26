# 🚗 Indian ANPR — License Plate Recognition System

A complete Python toolkit for detecting and reading Indian vehicle number plates.
Supports **private (white)**, **commercial (yellow)**, **EV (green)**, **taxi (black-on-yellow)** plates and the **BH series**.

---

## 📁 Project Structure

```
indian_anpr/
├── core/
│   ├── __init__.py
│   └── anpr_engine.py       ← Core detection + OCR engine
├── detect_image.py          ← Single image test
├── webcam_live.py           ← Live webcam feed
├── bulk_process.py          ← Batch folder processing + CSV
├── api_server.py            ← Flask REST API
├── requirements.txt
├── logs/                    ← CSV logs saved here
└── output/                  ← Annotated images saved here
```

---

## ⚙️ Installation (Windows)

**Step 1 — Python 3.10 or 3.11 recommended**
Download from https://www.python.org/downloads/

**Step 2 — Create a virtual environment**
```cmd
cd indian_anpr
python -m venv venv
venv\Scripts\activate
```

**Step 3 — Install dependencies**
```cmd
pip install -r requirements.txt
```

> **First run:** EasyOCR will download its English model (~100 MB). This happens once automatically.

> **GPU (optional):** If you have an NVIDIA GPU, uncomment the torch line in `requirements.txt` for much faster processing.

---

## 🚀 Usage

### 1. Single Image
```cmd
python detect_image.py car.jpg
python detect_image.py car.jpg --show        # pop-up window
python detect_image.py car.jpg --save        # save annotated image
```

### 2. Live Webcam Feed
```cmd
python webcam_live.py
python webcam_live.py --camera 1             # external webcam
python webcam_live.py --save-log             # log detections to CSV
python webcam_live.py --save-log --save-frames   # also save frames
```

**Webcam controls:**
| Key | Action |
|-----|--------|
| Q / ESC | Quit |
| S | Save current frame |
| R | Reset overlay |

### 3. Bulk Folder Processing
```cmd
python bulk_process.py --input C:\my_images\
python bulk_process.py --input C:\my_images\ --save-annotated
python bulk_process.py --input C:\my_images\ --csv results.csv --recursive
```

CSV columns: `filename, detected, plate_number, plate_type, plate_label, state_code, state_name, series, confidence, notes, timestamp`

### 4. Flask REST API
```cmd
python api_server.py
python api_server.py --port 8080 --debug
```

Open `http://localhost:5000` in your browser to see the API docs.

#### API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/health` | Server health check |
| POST | `/api/detect` | Upload image file → JSON |
| POST | `/api/detect/base64` | Base64 JSON body → JSON |
| POST | `/api/detect/annotated` | Upload image → annotated JPEG |
| GET | `/api/logs?limit=20` | Recent detections |

#### Example — curl
```bash
curl -X POST http://localhost:5000/api/detect \
     -F "image=@car.jpg"
```

#### Example — Python requests
```python
import requests

with open("car.jpg", "rb") as f:
    r = requests.post("http://localhost:5000/api/detect", files={"image": f})
print(r.json())
```

#### Example — JavaScript fetch (for web app)
```js
const formData = new FormData();
formData.append("image", fileInput.files[0]);
const res = await fetch("http://localhost:5000/api/detect", {
  method: "POST", body: formData
});
const data = await res.json();
console.log(data.plate_number, data.state_name, data.confidence);
```

---

## 📊 JSON Response Format

```json
{
  "detected": true,
  "plate_number": "MH 12 AB 1234",
  "plate_raw": "MH12AB1234",
  "plate_type": "white",
  "plate_label": "Private",
  "state_code": "MH",
  "state_name": "Maharashtra",
  "series": "Old format",
  "confidence": 91.5,
  "bbox": [120, 340, 280, 80],
  "timestamp": "2025-06-07T14:22:11",
  "notes": "",
  "processing_time_ms": 320.4
}
```

---

## 🔧 Troubleshooting (Windows)

| Problem | Fix |
|---------|-----|
| `cv2` not found | `pip install opencv-python` |
| EasyOCR slow on first run | It's downloading models (~100 MB), wait once |
| Camera not opening | Try `--camera 1` or install VLC for DirectShow support |
| Low confidence scores | Ensure good lighting; try a clearer/closer image |
| `flask-cors` error | `pip install flask-cors` |

---

## 🗺️ Supported State Codes

All 36 Indian states and UTs are supported, including:
`MH` Maharashtra · `DL` Delhi · `KA` Karnataka · `TN` Tamil Nadu · `UP` Uttar Pradesh · `GJ` Gujarat · `RJ` Rajasthan · `PB` Punjab · `HR` Haryana · `WB` West Bengal · `BH` (Bharat Series) and all others.
