# Dartboard Camera Focus Web App (Standalone)

A minimalist, zero-dependency, client-side web application for calibrating optical sharpness on dartboard USB cameras (Autodarts, Winmau, Scolia, DIY cams).

---

## ✨ Features

- **100% Client-Side:** Zero backend, runs purely in the browser using HTML5 Canvas and WebRTC.
- **Precision Focus Sharpness:** Normalized 0–100 sharpness score using Laplacian variance and Sobel edge gradients.
- **Peak Latching & Trend Tracking:** Retains your best score with real-time `Sharpening`, `At Peak`, and `Overshot` state cues.
- **2.5× Picture-in-Picture Loupe:** High-contrast zoom crop on the target segment with optional **Neon Green Focus Peaking**.
- **Interactive Target Reticle:** Click or drag anywhere on the video feed to reposition the target sweet spot.
- **Multi-Camera & Multi-Resolution Support:** Switch between USB video devices and resolutions (1280×960, 1920×1080, 1280×720, 640×480).
- **Silent & Minimalist:** No audio tones or distracting clutter.

---

## 🚀 How to Run Locally

Simply double-click [`index.html`](index.html) or open it in Google Chrome, Microsoft Edge, Mozilla Firefox, or Safari.

Or serve locally with Python:
```bash
cd tools/camera-focus-web
python3 -m http.server 8080
```
Then visit `http://localhost:8080` in your browser.

---

## 🌐 How to Host on GitHub Pages

### Option 1: In this Repository
1. Push `tools/camera-focus-web/index.html` to your GitHub repository.
2. Go to **Repository Settings** ➔ **Pages**.
3. Under **Build and deployment**, select **Deploy from a branch**.
4. Set branch to `main` and folder to `/docs` (or copy `index.html` to root/docs).

### Option 2: Standalone GitHub Repository
1. Create a new repository on GitHub (e.g. `dart-camera-focus`).
2. Copy [`index.html`](index.html) into the root of the new repo.
3. Enable **GitHub Pages** (Source: `Deploy from branch -> main / (root)`).
4. Your tool is live instantly at: `https://<your-username>.github.io/dart-camera-focus/`
