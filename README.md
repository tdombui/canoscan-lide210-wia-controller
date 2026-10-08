# CanoScan LiDE 210 WIA Controller

A standalone Windows desktop app that drives a Canon CanoScan LiDE 210 flatbed
scanner directly through **Windows Image Acquisition (WIA)** COM automation —
no Canon scanning software required.

## Why this exists

Canon never released CanoScan Toolbox / scanner software for Windows 11, and
the LiDE 210 is old enough that Canon's support page points you at drivers
that don't install cleanly (or at all) on modern Windows. If Windows still
recognizes the scanner as a device under **Settings → Bluetooth & devices →
Printers & scanners** (it usually does, via Windows' own inbox WIA driver),
this app talks to it directly through the WIA COM API and skips Canon's
software entirely.

It isn't hardcoded to the LiDE 210 specifically — it connects to the first
WIA scanner device Windows reports, so it should work with most WIA-compatible
flatbed scanners.

## Features

- DPI selection (75 / 150 / 300 / 600)
- Dimension presets (Full Letter, A4, 5x7, 4x6) or custom crop size
- PNG / JPEG / PDF output
- Start Scan, Quick Scan (hardcoded 150 DPI grayscale full-bed JPEG, tuned to
  minimize motor stepping, CIS sensor heat, and file size), and Stop Scan
- Archival file naming: `scan-yymmdd-xxxxx.ext`, auto-incrementing per day
  across all output folders so nothing is ever overwritten
- Auto-crop to content via Canny edge detection (not just a brightness
  threshold), so it holds up even when the scanned item is nearly the same
  brightness as the scanner lid's white backing
- Dark UI theme with a live scan-progress indicator
- A lightweight built-in photo editor (crop, brightness/contrast/sharpness,
  tone curve, rotate/flip, batch-apply across many frames) that can
  auto-launch after a scan finishes

## Requirements

- Windows 10/11
- Python 3.9+
- A WIA-compatible flatbed scanner (built and tested against the CanoScan
  LiDE 210)

## Installation

```
git clone https://github.com/tdombui/canoscan-lide210-wia-controller.git
cd canoscan-lide210-wia-controller
pip install -r requirements.txt
```

## Usage

```
python main.py
```

Pick a DPI, dimensions, and output type, then **Start Scan**. Use **Quick
Scan** for a fast low-ceremony capture, and **Stop Scan** to cancel — note
that WIA's transfer call is a single blocking operation with no public abort
API, so Stop can only take effect before a transfer starts or after it
finishes, not mid-transfer.

Scanned files land in `output/png`, `output/jpg`, or `output/pdf` depending
on the chosen format.

### Post-processing editor

Check "Open editor after scan" (on by default) to automatically launch the
built-in editor pointed at the folder you just scanned into, or run it
standalone:

```
python editor.py [folder] [initial_file]
```

Dial in brightness/contrast/sharpness/curve/crop on one frame, then use
**Apply Current Adjustments to Batch** to stamp the same look onto a
selection of frames or the whole folder — useful when digitizing many
frames (e.g. receipt-printer photo strips, film frames) that should share a
consistent look. Edits are always written to an `edited/` subfolder next to
the originals; source files are never overwritten.

## Known limitations

- **Stop Scan** can't interrupt an in-flight WIA transfer (see Usage above).
- The scan progress bar is an animated "busy" indicator, not a true
  percentage — the WIA automation layer used here doesn't expose per-byte
  transfer progress.
- Auto-crop's edge-detection thresholds are tuned for a typical flatbed scan
  against a light scanner lid; very low-contrast or multi-item scans may need
  the thresholds in `main.py` adjusted.
- The editor's rotate is lossless 90°-step only; there's no deskew for
  slightly rotated items.

## License

MIT — see [LICENSE](LICENSE).
