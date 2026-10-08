"""
Lightweight photo post-processing editor for scanned frames.

Standalone Tkinter app: crop (single image or batch), brightness/contrast/
sharpness, a tone curve, rotate/flip, and one-click auto-contrast. Designed
for the "scan many frames, then grade them consistently" workflow:
adjustments dialed in on one frame can be stamped onto a batch of others.

Run directly:   python editor.py [folder] [initial_file]
Or launched by main.py after a scan completes.

All edits are written to an "edited" subfolder next to the source images;
originals are never overwritten.
"""

import copy
import os
import sys
import threading
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import ttk, filedialog, messagebox

import numpy as np
from PIL import Image, ImageEnhance, ImageOps, ImageTk

import theme

SUPPORTED_EXTENSIONS = (".png", ".jpg", ".jpeg")

PREVIEW_MAX_SIZE = 900   # max on-screen dimension; full-res only touched on save
CURVE_CANVAS_SIZE = 220
DEFAULT_CURVE = [(0, 0), (255, 255)]

ROTATE_TRANSPOSE_CW = {
    90: Image.ROTATE_270,
    180: Image.ROTATE_180,
    270: Image.ROTATE_90,
}


# ---------------------------------------------------------------------------
# Adjustment model + processing pipeline
# ---------------------------------------------------------------------------

@dataclass
class Adjustments:
    brightness: float = 1.0
    contrast: float = 1.0
    sharpness: float = 1.0
    auto_contrast: bool = False
    rotation: int = 0  # cumulative clockwise degrees: 0/90/180/270
    flip_h: bool = False
    flip_v: bool = False
    # Fractions (left, top, right, bottom), each 0..1, of the image *after*
    # rotation/flip. Fractional (not pixel) so the same crop can be reused
    # across a batch of frames that may differ slightly in resolution.
    crop_frac: tuple = None
    curve_points: list = field(default_factory=lambda: list(DEFAULT_CURVE))


def build_curve_lut(points):
    pts = sorted(points, key=lambda p: p[0])
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    lut = np.interp(range(256), xs, ys)
    return [int(max(0, min(255, round(v)))) for v in lut]


def apply_adjustments(img, adj):
    """Runs the full edit pipeline on img (a PIL Image) and returns a new
    image. Order: geometry (rotate/flip/crop) -> tone (auto-contrast/curve/
    brightness/contrast) -> sharpness last, since it should act on the
    final tonal result."""
    out = img.convert("RGB")

    if adj.rotation in ROTATE_TRANSPOSE_CW:
        out = out.transpose(ROTATE_TRANSPOSE_CW[adj.rotation])
    if adj.flip_h:
        out = out.transpose(Image.FLIP_LEFT_RIGHT)
    if adj.flip_v:
        out = out.transpose(Image.FLIP_TOP_BOTTOM)

    if adj.crop_frac:
        w, h = out.size
        left_f, top_f, right_f, bottom_f = adj.crop_frac
        left = max(0, min(w - 1, round(left_f * w)))
        top = max(0, min(h - 1, round(top_f * h)))
        right = max(left + 1, min(w, round(right_f * w)))
        bottom = max(top + 1, min(h, round(bottom_f * h)))
        out = out.crop((left, top, right, bottom))

    if adj.auto_contrast:
        out = ImageOps.autocontrast(out)

    if adj.curve_points != DEFAULT_CURVE:
        lut = build_curve_lut(adj.curve_points)
        out = out.point(lut * 3)

    if adj.brightness != 1.0:
        out = ImageEnhance.Brightness(out).enhance(adj.brightness)
    if adj.contrast != 1.0:
        out = ImageEnhance.Contrast(out).enhance(adj.contrast)
    if adj.sharpness != 1.0:
        out = ImageEnhance.Sharpness(out).enhance(adj.sharpness)

    return out


def save_processed(img, dest_path):
    if dest_path.lower().endswith((".jpg", ".jpeg")):
        img.save(dest_path, "JPEG", quality=92)
    else:
        img.save(dest_path, "PNG")


# ---------------------------------------------------------------------------
# Tone curve widget
# ---------------------------------------------------------------------------

class CurveEditor(tk.Canvas):
    """Draggable tone-curve editor. Click empty space to add a control
    point, drag a point to move it, double-click an interior point to
    remove it. Endpoints stay pinned at x=0 and x=255."""

    def __init__(self, master, on_change, size=CURVE_CANVAS_SIZE):
        super().__init__(
            master, width=size, height=size, bg=theme.COLOR_BG_TROUGH,
            highlightthickness=1, highlightbackground=theme.COLOR_FIELD_BG,
        )
        self.size = size
        self.on_change = on_change
        self.points = list(DEFAULT_CURVE)
        self.histogram = None
        self.drag_index = None

        self.bind("<Button-1>", self._on_click)
        self.bind("<B1-Motion>", self._on_drag)
        self.bind("<ButtonRelease-1>", self._on_release)
        self.bind("<Double-Button-1>", self._on_double_click)
        self._redraw()

    def set_points(self, points):
        self.points = list(points)
        self._redraw()

    def set_histogram(self, hist256):
        self.histogram = hist256
        self._redraw()

    def _to_canvas(self, x, y):
        return x / 255 * self.size, self.size - (y / 255 * self.size)

    def _to_value(self, cx, cy):
        x = max(0, min(255, cx / self.size * 255))
        y = max(0, min(255, (self.size - cy) / self.size * 255))
        return x, y

    def _nearest_point(self, cx, cy, threshold=10):
        best, best_dist = None, threshold
        for i, (x, y) in enumerate(self.points):
            px, py = self._to_canvas(x, y)
            dist = ((px - cx) ** 2 + (py - cy) ** 2) ** 0.5
            if dist < best_dist:
                best, best_dist = i, dist
        return best

    def _on_click(self, event):
        idx = self._nearest_point(event.x, event.y)
        if idx is not None:
            self.drag_index = idx
        else:
            x, y = self._to_value(event.x, event.y)
            self.points.append((x, y))
            self.points.sort(key=lambda p: p[0])
            self.drag_index = next(i for i, p in enumerate(self.points) if p == (x, y))
            self._notify()
        self._redraw()

    def _on_drag(self, event):
        if self.drag_index is None:
            return
        x, y = self._to_value(event.x, event.y)
        i = self.drag_index
        if i == 0:
            x = 0
        elif i == len(self.points) - 1:
            x = 255
        else:
            x = max(self.points[i - 1][0] + 1, min(self.points[i + 1][0] - 1, x))
        self.points[i] = (x, y)
        self._notify()
        self._redraw()

    def _on_release(self, _event):
        self.drag_index = None

    def _on_double_click(self, event):
        idx = self._nearest_point(event.x, event.y)
        if idx is not None and 0 < idx < len(self.points) - 1:
            del self.points[idx]
            self._notify()
            self._redraw()

    def _notify(self):
        if self.on_change:
            self.on_change(list(self.points))

    def _redraw(self):
        self.delete("all")
        if self.histogram:
            maxv = max(self.histogram) or 1
            for x in range(256):
                v = self.histogram[x] / maxv
                cx = x / 255 * self.size
                self.create_line(cx, self.size, cx, self.size - v * self.size, fill=theme.COLOR_FIELD_BG)
        for frac in (0.25, 0.5, 0.75):
            p = frac * self.size
            self.create_line(0, p, self.size, p, fill=theme.COLOR_FIELD_BG)
            self.create_line(p, 0, p, self.size, fill=theme.COLOR_FIELD_BG)
        self.create_line(0, self.size, self.size, 0, fill=theme.COLOR_FIELD_BG, dash=(2, 2))

        lut = build_curve_lut(self.points)
        coords = []
        for x in range(0, 256, 2):
            cx, cy = self._to_canvas(x, lut[x])
            coords.extend([cx, cy])
        if len(coords) >= 4:
            self.create_line(*coords, fill=theme.COLOR_ACCENT, width=2)
        for x, y in self.points:
            cx, cy = self._to_canvas(x, y)
            self.create_oval(cx - 4, cy - 4, cx + 4, cy + 4, fill=theme.COLOR_ACCENT, outline="")


# ---------------------------------------------------------------------------
# Main editor window
# ---------------------------------------------------------------------------

class EditorApp(tk.Tk):
    def __init__(self, folder=None, initial_file=None):
        super().__init__()
        self.title("Scan Post-Processing Editor")
        self.geometry("1180x760")
        self.minsize(900, 600)
        theme.apply_dark_theme(self)

        self.folder = folder or os.getcwd()
        self.files = []
        self.current_index = -1
        self.current_image_full = None     # original, full resolution
        self.current_image_preview = None  # downscaled copy used for live edits
        self.preview_photo = None          # ImageTk.PhotoImage currently shown
        self.preview_offset = (0, 0)
        self.preview_disp_size = (0, 0)

        self.adj = Adjustments()

        self.crop_mode = False
        self.crop_start = None
        self.crop_rect_id = None
        self.pending_crop_frac = None

        self.batch_cancel_event = threading.Event()

        self._build_widgets()
        self._load_folder(self.folder)
        if initial_file and initial_file in self.files:
            self._select_file(self.files.index(initial_file))
        elif self.files:
            self._select_file(0)

    # -- layout --------------------------------------------------------------

    def _build_widgets(self):
        toolbar = ttk.Frame(self)
        toolbar.pack(side="top", fill="x", padx=6, pady=6)
        ttk.Button(toolbar, text="Open Folder...", command=self._choose_folder).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Refresh", command=lambda: self._load_folder(self.folder)).pack(side="left", padx=2)
        ttk.Button(toolbar, text="◀ Prev", command=self._prev_file).pack(side="left", padx=(12, 2))
        ttk.Button(toolbar, text="Next ▶", command=self._next_file).pack(side="left", padx=2)
        ttk.Button(toolbar, text="Save", command=self.save_current).pack(side="right", padx=2)
        ttk.Button(toolbar, text="Save As Copy...", command=self.save_as_copy).pack(side="right", padx=2)
        ttk.Button(toolbar, text="Reset Adjustments", command=self.reset_adjustments).pack(side="right", padx=(2, 12))

        body = ttk.Frame(self)
        body.pack(side="top", fill="both", expand=True, padx=6, pady=(0, 6))

        # Filmstrip (left)
        strip_frame = ttk.Frame(body, width=220)
        strip_frame.pack(side="left", fill="y")
        ttk.Label(strip_frame, text="Frames (ctrl/shift-click to multi-select for batch):").pack(anchor="w")
        list_container = ttk.Frame(strip_frame)
        list_container.pack(fill="both", expand=True)
        scrollbar = ttk.Scrollbar(list_container, orient="vertical")
        self.filmstrip = tk.Listbox(
            list_container, selectmode="extended", exportselection=False,
            bg=theme.COLOR_FIELD_BG, fg=theme.COLOR_FG,
            selectbackground=theme.COLOR_ACCENT, selectforeground="#1a1a1a",
            highlightthickness=0, borderwidth=0, yscrollcommand=scrollbar.set,
            width=28,
        )
        scrollbar.configure(command=self.filmstrip.yview)
        self.filmstrip.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.filmstrip.bind("<<ListboxSelect>>", self._on_filmstrip_select)

        # Preview canvas (center)
        canvas_frame = ttk.Frame(body)
        canvas_frame.pack(side="left", fill="both", expand=True, padx=8)
        self.canvas = tk.Canvas(canvas_frame, bg=theme.COLOR_BG_TROUGH, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<ButtonPress-1>", self._on_canvas_press)
        self.canvas.bind("<B1-Motion>", self._on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_canvas_release)
        self.canvas.bind("<Configure>", lambda _e: self.render_preview())

        # Adjustment panel (right)
        panel = ttk.Frame(body, width=300)
        panel.pack(side="left", fill="y")

        crop_frame = ttk.LabelFrame(panel, text="Crop")
        crop_frame.pack(fill="x", pady=(0, 8))
        self.crop_button = ttk.Button(crop_frame, text="Crop Tool: Off", command=self._toggle_crop_mode)
        self.crop_button.pack(fill="x", padx=4, pady=2)
        row = ttk.Frame(crop_frame)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Apply Crop", command=self.apply_crop).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Clear Crop", command=self.clear_crop).pack(side="left", expand=True, fill="x", padx=(4, 0))

        geo_frame = ttk.LabelFrame(panel, text="Orientation")
        geo_frame.pack(fill="x", pady=(0, 8))
        row = ttk.Frame(geo_frame)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Rotate ↺", command=lambda: self._rotate(-90)).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Rotate ↻", command=lambda: self._rotate(90)).pack(side="left", expand=True, fill="x", padx=(4, 0))
        row = ttk.Frame(geo_frame)
        row.pack(fill="x", padx=4, pady=2)
        ttk.Button(row, text="Flip H", command=self._flip_h).pack(side="left", expand=True, fill="x")
        ttk.Button(row, text="Flip V", command=self._flip_v).pack(side="left", expand=True, fill="x", padx=(4, 0))

        tone_frame = ttk.LabelFrame(panel, text="Tone")
        tone_frame.pack(fill="x", pady=(0, 8))
        self.brightness_var = self._make_slider(tone_frame, "Brightness", self._on_brightness)
        self.contrast_var = self._make_slider(tone_frame, "Contrast", self._on_contrast)
        self.sharpness_var = self._make_slider(tone_frame, "Sharpness", self._on_sharpness)
        self.auto_contrast_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            tone_frame, text="Auto Contrast", variable=self.auto_contrast_var,
            command=self._on_auto_contrast,
        ).pack(anchor="w", padx=4, pady=(2, 4))

        curve_frame = ttk.LabelFrame(panel, text="Curve (double-click a point to remove it)")
        curve_frame.pack(fill="x", pady=(0, 8))
        self.curve_editor = CurveEditor(curve_frame, on_change=self._on_curve_change)
        self.curve_editor.pack(padx=4, pady=4)

        batch_frame = ttk.LabelFrame(panel, text="Batch Apply")
        batch_frame.pack(fill="x", pady=(0, 8))
        self.batch_scope_var = tk.StringVar(value="selected")
        ttk.Radiobutton(batch_frame, text="Selected frames", value="selected", variable=self.batch_scope_var).pack(anchor="w", padx=4)
        ttk.Radiobutton(batch_frame, text="All frames in folder", value="all", variable=self.batch_scope_var).pack(anchor="w", padx=4)
        ttk.Button(batch_frame, text="Apply Current Adjustments to Batch", command=self.run_batch).pack(fill="x", padx=4, pady=4)
        row = ttk.Frame(batch_frame)
        row.pack(fill="x", padx=4, pady=(0, 4))
        self.batch_progress = ttk.Progressbar(row, orient="horizontal", mode="determinate")
        self.batch_progress.pack(side="left", fill="x", expand=True)
        self.batch_cancel_button = ttk.Button(row, text="Cancel", command=self.batch_cancel_event.set, state="disabled")
        self.batch_cancel_button.pack(side="left", padx=(4, 0))

        self.status_var = tk.StringVar(value="Ready.")
        status_label = ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w")
        status_label.pack(side="bottom", fill="x")

    def _make_slider(self, parent, label, on_change):
        ttk.Label(parent, text=label).pack(anchor="w", padx=4)
        var = tk.DoubleVar(value=1.0)
        scale = ttk.Scale(parent, from_=0.0, to=2.0, variable=var, command=lambda _v: on_change(var.get()))
        scale.pack(fill="x", padx=4, pady=(0, 4))
        return var

    # -- folder / file list ----------------------------------------------------

    def _choose_folder(self):
        folder = filedialog.askdirectory(initialdir=self.folder)
        if folder:
            self._load_folder(folder)
            if self.files:
                self._select_file(0)

    def _load_folder(self, folder):
        self.folder = folder
        try:
            names = os.listdir(folder)
        except OSError as e:
            messagebox.showerror("Open Folder", f"Could not read folder:\n{e}")
            names = []
        self.files = sorted(
            n for n in names
            if n.lower().endswith(SUPPORTED_EXTENSIONS) and os.path.isfile(os.path.join(folder, n))
        )
        self.filmstrip.delete(0, tk.END)
        for name in self.files:
            self.filmstrip.insert(tk.END, name)
        self.status_var.set(f"Loaded {len(self.files)} image(s) from {folder}")
        if self.current_index >= len(self.files):
            self.current_index = -1

    def _on_filmstrip_select(self, _event):
        active = self.filmstrip.index(tk.ACTIVE)
        if active is not None and 0 <= active < len(self.files):
            self._select_file(active, keep_selection=True)

    def _prev_file(self):
        if self.current_index > 0:
            self._select_file(self.current_index - 1)

    def _next_file(self):
        if self.current_index < len(self.files) - 1:
            self._select_file(self.current_index + 1)

    def _select_file(self, index, keep_selection=False):
        if index < 0 or index >= len(self.files):
            return
        self.current_index = index
        path = os.path.join(self.folder, self.files[index])
        try:
            img = Image.open(path).convert("RGB")
        except Exception as e:
            messagebox.showerror("Open Image", f"Could not open {path}:\n{e}")
            return

        self.current_image_full = img
        w, h = img.size
        scale = min(1.0, PREVIEW_MAX_SIZE / max(w, h))
        if scale < 1.0:
            self.current_image_preview = img.resize((round(w * scale), round(h * scale)), Image.LANCZOS)
        else:
            self.current_image_preview = img.copy()

        # Adjustments are per-currently-loaded frame and start fresh on
        # each selection (not persisted per file) -- this keeps the tool's
        # state simple: dial in a look, then use Batch Apply to stamp it
        # onto the rest rather than relying on hidden per-frame memory.
        self.adj = Adjustments()
        self._sync_controls_to_adj()
        self.curve_editor.set_histogram(self.current_image_preview.convert("L").histogram())

        if not keep_selection:
            self.filmstrip.selection_clear(0, tk.END)
            self.filmstrip.selection_set(index)
        self.filmstrip.see(index)
        self.status_var.set(f"{self.files[index]}  ({w}x{h})")
        self.render_preview()

    # -- adjustment controls ----------------------------------------------------

    def _sync_controls_to_adj(self):
        self.brightness_var.set(self.adj.brightness)
        self.contrast_var.set(self.adj.contrast)
        self.sharpness_var.set(self.adj.sharpness)
        self.auto_contrast_var.set(self.adj.auto_contrast)
        self.curve_editor.set_points(self.adj.curve_points)
        self.crop_mode = False
        self.pending_crop_frac = None
        self.crop_button.configure(text="Crop Tool: Off")

    def _on_brightness(self, value):
        self.adj.brightness = float(value)
        self.render_preview()

    def _on_contrast(self, value):
        self.adj.contrast = float(value)
        self.render_preview()

    def _on_sharpness(self, value):
        self.adj.sharpness = float(value)
        self.render_preview()

    def _on_auto_contrast(self):
        self.adj.auto_contrast = self.auto_contrast_var.get()
        self.render_preview()

    def _on_curve_change(self, points):
        self.adj.curve_points = points
        self.render_preview()

    def _rotate(self, delta):
        self.adj.rotation = (self.adj.rotation + delta) % 360
        self.render_preview()

    def _flip_h(self):
        self.adj.flip_h = not self.adj.flip_h
        self.render_preview()

    def _flip_v(self):
        self.adj.flip_v = not self.adj.flip_v
        self.render_preview()

    def reset_adjustments(self):
        self.adj = Adjustments()
        self._sync_controls_to_adj()
        self.render_preview()

    # -- crop tool ----------------------------------------------------------

    def _toggle_crop_mode(self):
        self.crop_mode = not self.crop_mode
        self.pending_crop_frac = None
        self.crop_button.configure(text="Crop Tool: On (drag on image)" if self.crop_mode else "Crop Tool: Off")
        self.render_preview()

    def apply_crop(self):
        if not self.pending_crop_frac:
            messagebox.showinfo("Crop", "Turn on the Crop Tool and drag a rectangle on the image first.")
            return
        self.adj.crop_frac = self.pending_crop_frac
        self.pending_crop_frac = None
        self.crop_mode = False
        self.crop_button.configure(text="Crop Tool: Off")
        self.render_preview()

    def clear_crop(self):
        self.adj.crop_frac = None
        self.pending_crop_frac = None
        self.render_preview()

    def _on_canvas_press(self, event):
        if not self.crop_mode:
            return
        self.crop_start = (event.x, event.y)
        if self.crop_rect_id:
            self.canvas.delete(self.crop_rect_id)
            self.crop_rect_id = None

    def _on_canvas_drag(self, event):
        if not self.crop_mode or not self.crop_start:
            return
        x0, y0 = self.crop_start
        if self.crop_rect_id:
            self.canvas.coords(self.crop_rect_id, x0, y0, event.x, event.y)
        else:
            self.crop_rect_id = self.canvas.create_rectangle(
                x0, y0, event.x, event.y, outline=theme.COLOR_ACCENT, width=2, dash=(4, 2),
            )

    def _on_canvas_release(self, event):
        if not self.crop_mode or not self.crop_start:
            return
        x0, y0 = self.crop_start
        x1, y1 = event.x, event.y
        self.crop_start = None

        ox, oy = self.preview_offset
        disp_w, disp_h = self.preview_disp_size
        if disp_w <= 0 or disp_h <= 0:
            return

        left = max(0, min(disp_w, min(x0, x1) - ox))
        right = max(0, min(disp_w, max(x0, x1) - ox))
        top = max(0, min(disp_h, min(y0, y1) - oy))
        bottom = max(0, min(disp_h, max(y0, y1) - oy))
        if right - left < 5 or bottom - top < 5:
            return  # ignore accidental clicks/tiny drags

        self.pending_crop_frac = (left / disp_w, top / disp_h, right / disp_w, bottom / disp_h)

    # -- rendering ------------------------------------------------------------

    def render_preview(self):
        self.canvas.delete("all")
        if self.current_image_preview is None:
            return

        # While actively choosing a crop, show the full (uncropped) frame
        # so the user can draw anywhere on it.
        show_adj = self.adj
        if self.crop_mode:
            show_adj = copy.copy(self.adj)
            show_adj.crop_frac = None

        img = apply_adjustments(self.current_image_preview, show_adj)
        canvas_w = max(1, self.canvas.winfo_width())
        canvas_h = max(1, self.canvas.winfo_height())
        w, h = img.size
        scale = min(canvas_w / w, canvas_h / h, 1.0)
        disp_w, disp_h = max(1, round(w * scale)), max(1, round(h * scale))
        disp_img = img.resize((disp_w, disp_h), Image.LANCZOS) if scale != 1.0 else img

        self.preview_photo = ImageTk.PhotoImage(disp_img)
        self.preview_disp_size = (disp_w, disp_h)
        self.preview_offset = ((canvas_w - disp_w) // 2, (canvas_h - disp_h) // 2)
        self.canvas.create_image(*self.preview_offset, anchor="nw", image=self.preview_photo)

    # -- save -----------------------------------------------------------------

    def _edited_dir(self):
        path = os.path.join(self.folder, "edited")
        os.makedirs(path, exist_ok=True)
        return path

    def save_current(self):
        if self.current_image_full is None:
            return
        out = apply_adjustments(self.current_image_full, self.adj)
        dest = os.path.join(self._edited_dir(), self.files[self.current_index])
        save_processed(out, dest)
        self.status_var.set(f"Saved to {dest}")

    def save_as_copy(self):
        if self.current_image_full is None:
            return
        name, ext = os.path.splitext(self.files[self.current_index])
        dest = filedialog.asksaveasfilename(
            initialdir=self.folder, initialfile=f"{name}-copy{ext}",
            defaultextension=ext,
            filetypes=[("PNG", "*.png"), ("JPEG", "*.jpg *.jpeg")],
        )
        if not dest:
            return
        out = apply_adjustments(self.current_image_full, self.adj)
        save_processed(out, dest)
        self.status_var.set(f"Saved to {dest}")

    # -- batch apply ------------------------------------------------------------

    def run_batch(self):
        if not self.files:
            return
        if self.batch_scope_var.get() == "all":
            targets = list(self.files)
        else:
            selected = self.filmstrip.curselection()
            targets = [self.files[i] for i in selected] if selected else [self.files[self.current_index]]
        if not targets:
            return

        proceed = messagebox.askyesno(
            "Apply to Batch",
            f"Apply the current adjustments to {len(targets)} file(s)?\n"
            f"Results are written to '{os.path.join(self.folder, 'edited')}'; originals are left untouched.",
        )
        if not proceed:
            return

        adj_snapshot = copy.deepcopy(self.adj)
        self.batch_cancel_event.clear()
        self.batch_progress["maximum"] = len(targets)
        self.batch_progress["value"] = 0
        self.batch_cancel_button.configure(state="normal")
        threading.Thread(target=self._batch_worker, args=(targets, adj_snapshot), daemon=True).start()

    def _batch_worker(self, targets, adj):
        edited_dir = self._edited_dir()
        errors = []
        total = len(targets)
        for i, name in enumerate(targets):
            if self.batch_cancel_event.is_set():
                break
            try:
                src = os.path.join(self.folder, name)
                with Image.open(src) as im:
                    out = apply_adjustments(im, adj)
                    save_processed(out, os.path.join(edited_dir, name))
            except Exception as e:
                errors.append(f"{name}: {e}")
            done = i + 1
            self.after(0, lambda d=done: self.batch_progress.configure(value=d))
            self.after(0, lambda d=done, t=total: self.status_var.set(f"Batch processing {d}/{t}..."))

        def finish():
            self.batch_cancel_button.configure(state="disabled")
            if self.batch_cancel_event.is_set():
                self.status_var.set("Batch cancelled.")
            elif errors:
                self.status_var.set(f"Batch finished with {len(errors)} error(s).")
                messagebox.showwarning("Batch Apply", "Some files failed:\n" + "\n".join(errors[:10]))
            else:
                self.status_var.set(f"Batch finished: {total} file(s) saved to {edited_dir}")

        self.after(0, finish)


def main():
    folder = sys.argv[1] if len(sys.argv) > 1 else None
    initial_file = sys.argv[2] if len(sys.argv) > 2 else None
    app = EditorApp(folder=folder, initial_file=initial_file)
    app.mainloop()


if __name__ == "__main__":
    main()
