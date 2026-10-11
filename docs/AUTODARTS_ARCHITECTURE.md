# Autodarts Desktop Internal Architecture & Decompiled Systems Reference

> **Target Version:** Autodarts Desktop `2.1.0-beta.1` (`linux-amd64`)  
> **Source Binary:** `autodarts-desktop` (26.7 MB ELF 64-bit LSB PIE)  
> **Analysis Engine:** Ghidra 12.1.4 (Base Address: `0x00100000`)  
> **Target Audience:** AI systems, reverse engineering agents, and automated analyzers.

---

## 1. System Overview & Technology Stack

```
┌─────────────────────────────────────────────────────────────┐
│                      Autodarts AppImage                     │
├──────────────────────────────┬──────────────────────────────┤
│     autodarts-desktop        │          UpdateNix           │
│   (Main C++/Rust Binary)     │   (Rust Squirrel Updater)    │
├──────────────────────────────┴──────────────────────────────┤
│                         libslint_cpp.so                     │
│                    (Slint UI Framework GUI)                 │
└─────────────────────────────────────────────────────────────┘
```

- **Core Engine:** C++20 / Rust with modern STL (`std::variant`, `std::shared_ptr`, `std::function`).
- **Computer Vision:** Statically linked OpenCV 5.0.0 (`cv::Mat`, `cv::HoughLinesP`, `cv::findHomography`, `cv::cartToPolar`).
- **UI & Presentation:** Slint UI Framework via `libslint_cpp.so` and Rust runtime bindings.
- **Embedded Web/IPC:** Custom embedded HTTP and WebSocket server (`httplib`, `ad::board::api::server::Server`).
- **Packaging:** Linux AppImage containing a Squirrel deployment manifest (`sq.version`).

---

## 2. Component Namespace Hierarchy

All core logic resides within the `ad::` (AutoDarts) C++ namespace:

| Namespace | Responsibility |
| :--- | :--- |
| `ad::board::model` | High-level application state, GUI data models, event dispatchers. |
| `ad::board::engine` | Core dartboard tracking engine, camera coordination, motion filters. |
| `ad::board::core` | Command definitions, event types (`Snapshot`, `Turn`, `Calibration`). |
| `ad::board::api` | Embedded REST API and WebSocket client/server endpoints. |
| `ad::camera::graph` | Multi-threaded video ingestion, YUV decoders, synchronized frame groups. |
| `ad::service` | `TrackingServer` and background dart hit triangulation service. |
| `ad::link` | Hardware peripheral interface (Autodarts Link, Wi-Fi cameras, LED rings). |

---

## 3. Computer Vision & Recognition Pipeline

Autodarts tracks dart hits using synchronized multi-camera feeds (typically 3 cameras mounted at 120° angles around the board perimeter).

```
Camera 1 ──> YUV Decode ──┐
Camera 2 ──> YUV Decode ──┼──> FrameGroup<cv::Mat> ──> Motion Filter ──> Hough Transform ──> Ray Intersection ──> Polar Mapping ──> Score Event
Camera 3 ──> YUV Decode ──┘
```

### 3.1 Video Ingestion & Frame Synchronization (`ad::camera::graph`)
- **Ingestion Sources:**
  - `ad::camera::graph::source::Camera`: Local USB UVC video capture.
  - `ad::camera::graph::source::Link`: Remote network camera streams (TCP/UDP/Wi-Fi).
- **Decoding:** Frames decoded via `CodecSource` from raw `RawPayload`/`YuvFrame` into `cv::Mat` instances managed by a memory-recycling pool (`MatPool`).
- **Synchronization:** Multi-camera frames are bundled into `FrameGroup<cv::Mat>` with matching millisecond timestamps to guarantee concurrent multi-angle analysis.

---

### 3.2 Motion Detection & Hand Rejection (`FUN_002fcfe0`)

Before running line detection, the engine filters frames to isolate dart hits from player movement and ambient noise.

#### Configuration Keys & Parameter Semantics:
```c
// Decompiled from FUN_002fcfe0
struct MotionConfig {
    int    kernel;                      // Morphological blur/denoise kernel size
    int    threshold;                   // Pixel delta binarization threshold
    int    stable_num_frames;           // Stationary frame count required to confirm hit
    int    min_change;                  // Minimum aggregate pixel delta to trigger detector
    double min_contour_area;            // Minimum contour area
    int    min_dart_pixels;             // Minimum pixel mass for valid dart contour
    int    min_hand_foreground_pixels;  // Hand detection upper threshold
    int    min_hand_background_pixels;  // Background occlusion threshold for hand removal
    int    min_dart_frames;             // Minimum persistence frames
};
```

#### Hand vs. Dart Classification Rule:
$$\text{IsHand} = (\Delta_{\text{foreground}} > \text{min\_hand\_foreground\_pixels}) \lor (\Delta_{\text{background\_occlusion}} > \text{min\_hand\_background\_pixels})$$

- If $\text{IsHand}$ is `true`: State machine enters `PlayerRetrieval` mode and ignores hits.
- If $\text{IsHand}$ is `false` and contour remains stationary for $\ge \text{stable\_num\_frames}$: Hit processing is triggered.

---

### 3.3 Dart Shaft Line Extraction (Hough Transform)
- Edge detection is applied to the difference image.
- `cv::HoughLinesP` extracts linear segments matching the dart shaft:
  - `detection.kernel`: Blur/gradient kernel.
  - `detection.threshold`: Binarization cutoff.
  - `detection.hough_threshold`: Accumulator vote threshold for line candidate confirmation.

---

### 3.4 Camera Calibration & Homography Projection (`FUN_01173410`)

Each camera $C_i$ ($i \in \{1, 2, 3\}$) operates with:
1. **Lens Undistortion:** Intrinsic camera calibration profile (`homography_undistorted`).
2. **Homography Matrix $\mathbf{H}_i$ ($3 \times 3$):** Projects 2D camera image coordinates $(u, v)$ to the board plane $(X, Y)$:

$$\begin{bmatrix} x' \\ y' \\ w \end{bmatrix} = \mathbf{H}_i \begin{bmatrix} u \\ v \\ 1 \end{bmatrix}, \quad X = \frac{x'}{w}, \quad Y = \frac{y'}{w}$$

3. **Multi-Camera Triangulation:**
   - Each camera computes a ray vector $\vec{R}_i$ representing the dart body pointing toward the board surface.
   - The convergence point of $\vec{R}_1, \vec{R}_2, \vec{R}_3$ on the board plane $(Z = 0)$ yields the dart tip coordinate $(X, Y)$ in millimeters from the center $(0, 0)$.

---

### 3.5 Sector & Score Mapping (`FUN_00319de0` & `FUN_00cd04e0`)

Once $(X, Y)$ is calculated:

```
                  20 (Top)
             5        1
         12              18
       9                    4
     14                      13
    11          (0,0)         6
      8                      10
       16                   15
         7               2
            19        17
                  3 (Bottom)
```

#### 1. Angle Calculation $\theta$:
$$\theta = \left(\operatorname{atan2}(Y, X) \times \frac{180}{\pi} + 90^\circ + 360^\circ\right) \bmod 360^\circ$$

Each of the 20 sectors occupies an $18^\circ$ wedge (offset by $9^\circ$ so 20 is centered on top).

**Standard Clock Sector Array:**
```c
const int SECTORS[20] = {20, 1, 18, 4, 13, 6, 10, 15, 2, 17, 3, 19, 7, 16, 8, 11, 14, 9, 12, 5};
```

#### 2. Radius Calculation $R$ and Ring Multiplier:
$$R = \sqrt{X^2 + Y^2}$$

| Zone / Ring | Typical Radius Range ($mm$) | Multiplier | Points Formula |
| :--- | :--- | :---: | :--- |
| **Double Bull (Inner)** | $0 \le R < 6.35$ | $1\times$ | $50$ |
| **Single Bull (Outer)** | $6.35 \le R < 15.9$ | $1\times$ | $25$ |
| **Single Inner** | $15.9 \le R < 97.0$ | $1\times$ | $\text{Sector} \times 1$ |
| **Triple Ring** | $97.0 \le R < 107.0$ | $3\times$ | $\text{Sector} \times 3$ |
| **Single Outer** | $107.0 \le R < 162.0$ | $1\times$ | $\text{Sector} \times 1$ |
| **Double Ring** | $162.0 \le R < 170.0$ | $2\times$ | $\text{Sector} \times 2$ |
| **Miss / Off-board** | $R \ge 170.0$ | $0\times$ | $0$ |

---

## 4. State Machine & Event Model

The engine operates on an event-driven loop via `ad::board::core::event`:

```
┌──────────────┐     Dart Thrown      ┌──────────────┐
│  WAIT_THROW  │ ───────────────────> │ HIT_DETECTED │
└──────────────┘                      └──────┬───────┘
       ▲                                     │
       │                                     ▼
       │  Throw < 3                   ┌──────────────┐
       └───────────────────────────── │ SCORE_UPDATE │
                                      └──────┬───────┘
                                             │ Throw == 3 or Hand Detected
                                             ▼
                                      ┌──────────────┐
                                      │ RETRIEVAL    │
                                      └──────┬───────┘
                                             │ Hand Cleared
                                             ▼
                                      ┌──────────────┐
                                      │  NEXT_TURN   │
                                      └──────────────┘
```

### Core Event Structs:
- **`Snapshot`:** Multi-camera image state and calibrated debug overlay.
- **`Turn`:** Active player turn, array of up to 3 throws (`points`, `multiplier`, `segment`, `coords_x`, `coords_y`).
- **`Status`:** Hardware and board connectivity state (`Idle`, `Tracking`, `Calibrating`, `Error`).
- **`Motion` / `MotionHistory`:** Real-time motion detection energy levels.
- **`Calibration`:** Homography matrices, ellipse parameters, and camera health metrics.

---

## 5. Local REST & WebSocket API Routes

The embedded server (`ad::board::api::server`) exposes routes for IPC, GUI frontends, and companion extensions:

### Endpoints
| Route | Method | Payload / Description |
| :--- | :---: | :--- |
| `/api/v1/state` | `GET` | Current game state, board status, active player. |
| `/api/v1/start` | `POST` | Start tracking engine. |
| `/api/v1/stop` | `POST` | Pause/stop tracking engine. |
| `/api/v1/reset` | `POST` | Reset current turn / clear throws. |
| `/api/v1/calibrate` | `POST` | Trigger automatic spider ellipse calibration. |
| `/api/v1/config` | `GET`/`PATCH` | Read or update motion/detection thresholds. |
| `/api/v1/devices` | `GET` | List connected USB and network camera devices. |
| `/api/v1/light` | `POST` | Set peripheral smart LED ring color / animation (`LinkSetLight`). |
| `/events` | `WebSocket` | Real-time broadcast stream of all `ad::board::core::event` payloads. |

---

## 6. Key Decompiled Functions Index

| Address | Function | Role & Implementation |
| :--- | :--- | :--- |
| `0x0023e3f0` | `main` | Entry point, CLI flag parsing, Slint UI bootstrap, runtime engine init. |
| `0x002fcfe0` | `FUN_002fcfe0` | Vision pipeline parameter parsing (`motion.*`, `detection.*`). |
| `0x00315830` | `FUN_00315830` | Dynamic configuration update handler for vision thresholds. |
| `0x00319de0` | `FUN_00319de0` | Dart coordinate $(X, Y) \to (R, \theta)$ sector and multiplier resolver. |
| `0x00cd04e0` | `FUN_00cd04e0` | Multiplier segment code dispatcher (Single, Double, Triple, Bull). |
| `0x00ca9b60` | `FUN_00ca9b60` | Dart hit event generator (`Snapshot` $\to$ `Turn` event transition). |
| `0x00cab560` | `FUN_00cab560` | Main state machine event loop (Throw indexing, turn rotation). |
| `0x01173410` | `FUN_01173410` | Homography perspective matrix transformation & lens undistortion. |

---

## 7. Exported Source Files Reference

The full 100% decompiled C source code is indexed on disk:
- **Unified Single-File Source:** [autodarts_decompiled/autodarts_all.c](file:///home/iterathor/ghidra/autodarts_decompiled/autodarts_all.c)
- **Modular Function Directory:** [autodarts_decompiled/src/](file:///home/iterathor/ghidra/autodarts_decompiled/src/) (15,088 `.c` files)
