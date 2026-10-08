"""
CanoScan LiDE 210 Scanner Controller
Controls the scanner directly via Windows Image Acquisition (WIA) COM objects.
"""

import os
import re
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from datetime import datetime

import cv2
import numpy as np
import pywintypes
import win32com.client
from PIL import Image

import theme

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIRS = {
    "png": os.path.join(PROJECT_ROOT, "output", "png"),
    "jpg": os.path.join(PROJECT_ROOT, "output", "jpg"),
    "pdf": os.path.join(PROJECT_ROOT, "output", "pdf"),
}

# ---------------------------------------------------------------------------
# WIA constants
#
# WIA item property IDs are not exposed as named constants by pywin32 (they
# come from wiadef.h in the Windows SDK), so they are hardcoded here. This is
# the standard set used by WIA 2.0 flatbed scanners such as the LiDE 210.
# ---------------------------------------------------------------------------

WIA_IPS_CUR_INTENT = "6146"   # Color / Grayscale / B&W intent flags
WIA_IPS_XRES = "6147"        # Horizontal resolution (DPI)
WIA_IPS_YRES = "6148"        # Vertical resolution (DPI)
WIA_IPS_XPOS = "6149"        # Horizontal start position (pixels at current DPI)
WIA_IPS_YPOS = "6150"        # Vertical start position (pixels at current DPI)
WIA_IPS_XEXTENT = "6151"     # Horizontal extent / width (pixels at current DPI)
WIA_IPS_YEXTENT = "6152"     # Vertical extent / height (pixels at current DPI)

WIA_INTENT_COLOR = 1
WIA_INTENT_GRAYSCALE = 2
WIA_INTENT_TEXT_BW = 4

# All WIA scanner drivers are required to support BMP transfer, but PNG/JPEG
# support varies, so we always transfer as BMP and let Pillow handle the
# final format conversion (and PDF packaging).
WIA_FORMAT_BMP = "{B96B3CAB-0728-11D3-9D7B-0000F81EF32E}"

WIA_DEVICE_TYPE_SCANNER = 1

DPI_OPTIONS = [75, 150, 300, 600]

# width_in, height_in in inches; None means "custom" (user-supplied)
DIMENSION_OPTIONS = {
    "Full Letter (8.5 x 11 in)": (8.5, 11.0),
    "A4 (8.27 x 11.69 in)": (8.27, 11.69),
    "5 x 7 in": (5.0, 7.0),
    "4 x 6 in": (4.0, 6.0),
    "Custom": None,
}

OUTPUT_TYPES = ["PNG", "JPEG", "PDF"]

EXT_BY_OUTPUT_TYPE = {"PNG": "png", "JPEG": "jpg", "PDF": "pdf"}

SERIAL_PATTERN = re.compile(r"^scan-(\d{6})-(\d{5})\.")


# ---------------------------------------------------------------------------
# File naming
# ---------------------------------------------------------------------------

def ensure_output_dirs():
    for path in OUTPUT_DIRS.values():
        os.makedirs(path, exist_ok=True)


def next_serial_for_today(date_str):
    """Scan every output directory for scan-{date_str}-NNNNN.* files and
    return the next free serial number, so the sequence is shared across
    PNG/JPEG/PDF regardless of which folder a given scan lands in."""
    highest = 0
    for path in OUTPUT_DIRS.values():
        if not os.path.isdir(path):
            continue
        for name in os.listdir(path):
            match = SERIAL_PATTERN.match(name)
            if match and match.group(1) == date_str:
                highest = max(highest, int(match.group(2)))
    return highest + 1


def build_output_path(ext):
    date_str = datetime.now().strftime("%y%m%d")
    serial = next_serial_for_today(date_str)
    filename = f"scan-{date_str}-{serial:05d}.{ext}"
    return os.path.join(OUTPUT_DIRS[ext], filename)


# ---------------------------------------------------------------------------
# WIA scanning
# ---------------------------------------------------------------------------

class ScanCancelled(Exception):
    pass


def find_scanner_device(device_manager):
    for info in device_manager.DeviceInfos:
        try:
            device_type = info.Type
        except Exception:
            device_type = None
        if device_type == WIA_DEVICE_TYPE_SCANNER:
            return info
    return None


def configure_item(item, dpi, width_in, height_in, intent):
    props = item.Properties
    props(WIA_IPS_XRES).Value = dpi
    props(WIA_IPS_YRES).Value = dpi
    props(WIA_IPS_XPOS).Value = 0
    props(WIA_IPS_YPOS).Value = 0
    props(WIA_IPS_XEXTENT).Value = max(1, round(width_in * dpi))
    props(WIA_IPS_YEXTENT).Value = max(1, round(height_in * dpi))
    props(WIA_IPS_CUR_INTENT).Value = intent


def run_scan(dpi, width_in, height_in, intent, cancel_event, status_cb):
    """Performs one full scan-and-save cycle. Runs on a background thread.
    Returns the saved file path."""

    if cancel_event.is_set():
        raise ScanCancelled()

    status_cb("Connecting to scanner...")
    device_manager = win32com.client.Dispatch("WIA.DeviceManager")
    info = find_scanner_device(device_manager)
    if info is None:
        raise RuntimeError("No WIA scanner device found. Is the LiDE 210 connected and powered on?")

    device = info.Connect()
    item = device.Items[0]

    if cancel_event.is_set():
        raise ScanCancelled()

    status_cb("Warming up / configuring scan area...")
    configure_item(item, dpi, width_in, height_in, intent)

    if cancel_event.is_set():
        raise ScanCancelled()

    status_cb("Scanning...")
    # Transfer() blocks until the scan completes; WIA offers no public API to
    # abort an in-flight transfer from another thread, so cancellation past
    # this point is not possible. We only honor cancel_event before/after.
    image = item.Transfer(WIA_FORMAT_BMP)

    if cancel_event.is_set():
        raise ScanCancelled()

    return image


# Margin kept around the detected content so a crop doesn't clip edge pixels.
AUTO_CROP_MARGIN_IN = 0.05
# Contours smaller than this fraction of the full scan area are treated as
# sensor noise / lint rather than the actual scanned item.
AUTO_CROP_MIN_AREA_FRACTION = 0.003


def auto_crop(img, dpi):
    """Crops img to the item's true edges via Canny edge detection + contour
    finding, rather than a brightness threshold. This still works when the
    item (e.g. thermal receipt paper) is nearly the same brightness as the
    scanner lid's padded white backing, because Canny reacts to the local
    edge/shadow gradient at the paper boundary, not the fill brightness.
    Returns img unchanged if no sufficiently large contour is found."""
    gray = np.array(img.convert("L"))

    # CLAHE boosts *local* contrast so a faint paper-edge shadow stands out
    # even when the global brightness difference between item and
    # background is almost zero.
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8, 8))
    enhanced = clahe.apply(gray)
    blurred = cv2.GaussianBlur(enhanced, (5, 5), 0)

    # Low thresholds to catch faint edges; a wide closing then bridges small
    # gaps in the outline so it forms one continuous contour.
    edges = cv2.Canny(blurred, 15, 50)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (9, 9))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel, iterations=3)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return img

    largest = max(contours, key=cv2.contourArea)
    min_area = AUTO_CROP_MIN_AREA_FRACTION * gray.shape[0] * gray.shape[1]
    if cv2.contourArea(largest) < min_area:
        return img

    x, y, w, h = cv2.boundingRect(largest)
    margin = round(AUTO_CROP_MARGIN_IN * dpi)
    left = max(0, x - margin)
    top = max(0, y - margin)
    right = min(img.width, x + w + margin)
    bottom = min(img.height, y + h + margin)
    return img.crop((left, top, right, bottom))


def save_image(wia_image, output_type, dpi, auto_crop_enabled):
    ext = EXT_BY_OUTPUT_TYPE[output_type]
    dest_path = build_output_path(ext)

    temp_bmp_path = dest_path + ".tmp.bmp"
    try:
        wia_image.SaveFile(temp_bmp_path)
        with Image.open(temp_bmp_path) as img:
            if auto_crop_enabled:
                img = auto_crop(img, dpi)
            if output_type == "JPEG":
                img.convert("RGB").save(dest_path, "JPEG", quality=90)
            elif output_type == "PDF":
                img.convert("RGB").save(dest_path, "PDF")
            else:
                img.save(dest_path, "PNG")
    finally:
        if os.path.exists(temp_bmp_path):
            os.remove(temp_bmp_path)

    return dest_path


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

EDITOR_SCRIPT = os.path.join(PROJECT_ROOT, "editor.py")


class ScannerApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CanoScan LiDE 210 Controller")
        self.resizable(False, False)

        self.cancel_event = threading.Event()
        self.scan_thread = None
        self.editor_process = None

        theme.apply_dark_theme(self)
        self._build_widgets()

    def _build_widgets(self):
        pad = {"padx": 8, "pady": 6}

        frame = ttk.Frame(self)
        frame.grid(row=0, column=0, sticky="nsew", **pad)

        # DPI
        ttk.Label(frame, text="DPI:").grid(row=0, column=0, sticky="w")
        self.dpi_var = tk.IntVar(value=DPI_OPTIONS[1])
        dpi_combo = ttk.Combobox(
            frame, textvariable=self.dpi_var, values=DPI_OPTIONS,
            state="readonly", width=10,
        )
        dpi_combo.grid(row=0, column=1, sticky="w")

        # Dimensions
        ttk.Label(frame, text="Dimensions:").grid(row=1, column=0, sticky="w")
        self.dimension_var = tk.StringVar(value="Full Letter (8.5 x 11 in)")
        dim_combo = ttk.Combobox(
            frame, textvariable=self.dimension_var,
            values=list(DIMENSION_OPTIONS.keys()), state="readonly", width=24,
        )
        dim_combo.grid(row=1, column=1, columnspan=3, sticky="w")
        dim_combo.bind("<<ComboboxSelected>>", self._on_dimension_change)

        ttk.Label(frame, text="Custom W (in):").grid(row=2, column=0, sticky="w")
        self.custom_w_var = tk.StringVar(value="8.5")
        self.custom_w_entry = ttk.Entry(frame, textvariable=self.custom_w_var, width=8, state="disabled")
        self.custom_w_entry.grid(row=2, column=1, sticky="w")

        ttk.Label(frame, text="Custom H (in):").grid(row=2, column=2, sticky="w")
        self.custom_h_var = tk.StringVar(value="11")
        self.custom_h_entry = ttk.Entry(frame, textvariable=self.custom_h_var, width=8, state="disabled")
        self.custom_h_entry.grid(row=2, column=3, sticky="w")

        # Output type
        ttk.Label(frame, text="Output Type:").grid(row=3, column=0, sticky="w")
        self.output_type_var = tk.StringVar(value="PNG")
        type_frame = ttk.Frame(frame)
        type_frame.grid(row=3, column=1, columnspan=3, sticky="w")
        for i, label in enumerate(OUTPUT_TYPES):
            ttk.Radiobutton(
                type_frame, text=label, value=label, variable=self.output_type_var,
            ).grid(row=0, column=i, sticky="w", padx=(0, 10))

        # Color mode (for Start Scan; Quick Scan always uses grayscale)
        ttk.Label(frame, text="Color Mode:").grid(row=4, column=0, sticky="w")
        self.color_mode_var = tk.StringVar(value="Color")
        mode_frame = ttk.Frame(frame)
        mode_frame.grid(row=4, column=1, columnspan=3, sticky="w")
        ttk.Radiobutton(mode_frame, text="Color", value="Color", variable=self.color_mode_var).grid(row=0, column=0, padx=(0, 10))
        ttk.Radiobutton(mode_frame, text="Grayscale", value="Grayscale", variable=self.color_mode_var).grid(row=0, column=1)

        # Auto-crop
        self.auto_crop_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            frame, text="Auto-crop to content (trim background outside scanned item)",
            variable=self.auto_crop_var,
        ).grid(row=5, column=0, columnspan=4, sticky="w", pady=(4, 0))

        # Auto-open editor
        self.auto_open_editor_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frame, text="Open editor after scan (reuses an already-open editor window)",
            variable=self.auto_open_editor_var,
        ).grid(row=6, column=0, columnspan=4, sticky="w")

        # Buttons
        button_frame = ttk.Frame(frame)
        button_frame.grid(row=7, column=0, columnspan=4, pady=(10, 4))

        self.start_button = ttk.Button(button_frame, text="Start Scan", command=self.start_scan)
        self.start_button.grid(row=0, column=0, padx=4)

        self.quick_button = ttk.Button(button_frame, text="Quick Scan", command=self.quick_scan)
        self.quick_button.grid(row=0, column=1, padx=4)

        self.stop_button = ttk.Button(button_frame, text="Stop Scan", command=self.stop_scan, state="disabled")
        self.stop_button.grid(row=0, column=2, padx=4)

        self.open_editor_button = ttk.Button(button_frame, text="Open Editor", command=lambda: self.open_editor())
        self.open_editor_button.grid(row=0, column=3, padx=4)

        # Progress bar: indeterminate (animated) rather than a true percentage,
        # since the WIA automation layer's Transfer() call is a single
        # blocking operation with no progress callback exposed to us.
        self.progress_bar = ttk.Progressbar(frame, orient="horizontal", mode="indeterminate")
        self.progress_bar.grid(row=8, column=0, columnspan=4, sticky="we", pady=(8, 0))

        # Status label
        self.status_var = tk.StringVar(value="Ready.")
        status_label = ttk.Label(frame, textvariable=self.status_var, relief="sunken", anchor="w")
        status_label.grid(row=9, column=0, columnspan=4, sticky="we", pady=(8, 0))

    def _on_dimension_change(self, event=None):
        is_custom = self.dimension_var.get() == "Custom"
        state = "normal" if is_custom else "disabled"
        self.custom_w_entry.configure(state=state)
        self.custom_h_entry.configure(state=state)

    # -- status / UI helpers -------------------------------------------------

    def set_status(self, text):
        self.after(0, lambda: self.status_var.set(text))

    def _set_running_state(self, running):
        self.start_button.configure(state="disabled" if running else "normal")
        self.quick_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        if running:
            self.progress_bar.start(15)
        else:
            self.progress_bar.stop()
            self.progress_bar["value"] = 0

    # -- actions --------------------------------------------------------------

    def start_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return

        dpi = int(self.dpi_var.get())
        dim_choice = self.dimension_var.get()
        if dim_choice == "Custom":
            try:
                width_in = float(self.custom_w_var.get())
                height_in = float(self.custom_h_var.get())
            except ValueError:
                messagebox.showerror("Invalid Dimensions", "Custom width/height must be numbers.")
                return
            if width_in <= 0 or height_in <= 0:
                messagebox.showerror("Invalid Dimensions", "Custom width/height must be positive.")
                return
        else:
            width_in, height_in = DIMENSION_OPTIONS[dim_choice]

        output_type = self.output_type_var.get()
        intent = WIA_INTENT_COLOR if self.color_mode_var.get() == "Color" else WIA_INTENT_GRAYSCALE

        self._launch_scan(dpi, width_in, height_in, intent, output_type, self.auto_crop_var.get())

    def quick_scan(self):
        if self.scan_thread and self.scan_thread.is_alive():
            return
        # Hardcoded: 150 DPI, grayscale (minimizes data volume and thus motor
        # stepping time, CIS sensor heat, and file size), full letter bed, JPEG.
        # Auto-crop still honors the checkbox since it's a post-process toggle,
        # not one of the scan parameters Quick Scan is meant to bypass.
        self._launch_scan(150, 8.5, 11.0, WIA_INTENT_GRAYSCALE, "JPEG", self.auto_crop_var.get())

    def stop_scan(self):
        self.cancel_event.set()
        self.set_status("Stop requested (will cancel before/after the active transfer only)...")

    def _launch_scan(self, dpi, width_in, height_in, intent, output_type, auto_crop_enabled):
        self.cancel_event.clear()
        self._set_running_state(True)
        self.scan_thread = threading.Thread(
            target=self._scan_worker,
            args=(dpi, width_in, height_in, intent, output_type, auto_crop_enabled),
            daemon=True,
        )
        self.scan_thread.start()

    def _scan_worker(self, dpi, width_in, height_in, intent, output_type, auto_crop_enabled):
        try:
            image = run_scan(dpi, width_in, height_in, intent, self.cancel_event, self.set_status)
            self.set_status("Saving file...")
            dest_path = save_image(image, output_type, dpi, auto_crop_enabled)
            self.set_status(f"Saved to {dest_path}")
            if self.auto_open_editor_var.get():
                self.after(0, lambda: self.open_editor(os.path.dirname(dest_path), os.path.basename(dest_path)))
        except ScanCancelled:
            self.set_status("Scan cancelled.")
        except pywintypes.com_error as e:
            self.set_status("Scanner error (device busy or locked).")
            self.after(0, lambda: messagebox.showerror("WIA Error", f"A COM error occurred talking to the scanner:\n{e}"))
        except Exception as e:
            self.set_status("Error during scan.")
            self.after(0, lambda: messagebox.showerror("Scan Error", str(e)))
        finally:
            self.after(0, lambda: self._set_running_state(False))

    def open_editor(self, folder=None, filename=None):
        """Launches the post-processing editor as a separate process (so an
        editor crash can't take down the scanner, and it stays usable
        standalone). Reuses an already-running editor instead of stacking
        up a new window per scan; that window won't auto-jump to the new
        file, but its Refresh button will pick it up."""
        if self.editor_process is not None and self.editor_process.poll() is None:
            self.set_status("Editor already open; use its Refresh button to see the latest scan.")
            return

        args = [sys.executable, EDITOR_SCRIPT]
        if folder:
            args.append(folder)
            if filename:
                args.append(filename)
        self.editor_process = subprocess.Popen(args)


def main():
    ensure_output_dirs()
    app = ScannerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
