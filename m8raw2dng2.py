#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
m8raw2dng2 - a refined, clean-room reimplementation of Arvid's m8raw2dng v1.20,
the converter that turns the Leica M8's uncompressed "button-dance" .RAW files into
Adobe DNG.

This version writes the DNG with a small purpose-built little-endian TIFF/DNG writer
(rather than a generic TIFF library) so that the output is byte-for-byte faithful to
the original tool: identical IFD0 (47 tags) and a real EXIF sub-IFD (21 tags) with the
camera's MakerNote copied verbatim, the exact Leica colour matrices, the measured
focal-plane / crop tags and the camera serial pulled from the JPG's MakerNote.

The sensor subsystem (-s / -sd) reproduces the original's behaviour exactly:
  * -sd  writes one LevelCorrection value per image column (= column_mean - target),
         defect columns are written as 0, output is 6-decimal CRLF text.
  * -s   subtracts the stored LevelCorrection from each column (a per-column constant)
         then repairs any Line defects.

Everything the original does is matched to the byte except FNumber, which the M8 does
not record for an un-coded lens and which the original *estimates* (poorly) from image
brightness. Here the aperture is instead recovered from the camera's two light meters --
MeasuredLV minus ExternalSensorBrightnessValue, plus a one-time per-(body,lens) offset
(--calibrate-fnumber) -- which reads the true aperture directly; -A/--aperture still
forces an exact value and yields a bit-identical DNG.

Refinements over the original (all opt-in; bare -v / -s stay byte-identical):
  * -sd accepts a whole folder of dark frames and auto-selects the clean, fast,
    low-ISO ones, averaging them for a less noisy darkfield (slow / high-ISO frames
    are skipped and logged).
  * optional defect-column detection for -sd via --auto-lines: bright/defect columns
    are zeroed in LevelCorrection and recorded as Line entries, which -s then repairs
    by neighbour interpolation (off by default; -sd alone writes the darkfield only).
  * -p embeds the full-resolution camera JPEG as the DNG preview (instead of a tiny
    320x240 thumbnail).
  * parallel conversion, recursive folders, dry-run, probe, and a Tk GUI front-end.
"""
from __future__ import annotations

import dataclasses
import glob
import logging
import math
import os
import struct
import sys
from dataclasses import dataclass, field

import numpy as np

try:
    import tifffile
    HAVE_TIFFFILE = True
except Exception:
    HAVE_TIFFFILE = False

try:
    from PIL import Image  # noqa: F401
    HAVE_PIL = True
except Exception:
    HAVE_PIL = False

__version__ = "2.13.1b0"
VERSION_DISPLAY = "2.13.1 beta"
PROG = "m8raw2dng2"
log = logging.getLogger(PROG)

RAW_W, RAW_H = 3968, 2646
RAW_HEADER_SAMPLES = 54
RAW_ROW_STRIDE = 3976
RAW_TRAILING_ROWS = 1
RAW_BYTES = (RAW_HEADER_SAMPLES + RAW_H * RAW_ROW_STRIDE
             + RAW_TRAILING_ROWS * RAW_ROW_STRIDE) * 2

CROP = 2
DNG_W, DNG_H = RAW_W - 2 * CROP, RAW_H - 2 * CROP
WHITE_DEFAULT = 16383
BLACK_DEFAULT = 92

CFA_PATTERNS = {
    "RGGB": (0, 1, 1, 2),
    "BGGR": (2, 1, 1, 0),
    "GRBG": (1, 0, 2, 1),
    "GBRG": (1, 2, 0, 1),
}
CFA_BYTES = {
    "RGGB": b"\x00\x01\x01\x02",
    "BGGR": b"\x02\x01\x01\x00",
    "GRBG": b"\x01\x00\x02\x01",
    "GBRG": b"\x01\x02\x00\x01",
}

EXIF_MAKE = "Leica Camera AG"
MODEL_M8 = "M8 Digital Camera"
MODEL_M8RAW = "M8RAW Digital Camera"
UNIQUE_MODEL = "M8 Digital Camera"

COLORMATRIX1_M8 = [(625, 597), (-110, 207), (16, 125), (-138, 319), (761, 625),
                   (112, 463), (-150, 1693), (229, 926), (179, 250)]
COLORMATRIX2_M8 = [(307, 400), (-439, 2000), (-14, 459), (-293, 500), (2345, 1661),
                   (187, 1007), (-97, 400), (115, 287), (544, 827)]
COLORMATRIX1_M9 = [(107, 125), (-335, 1647), (-2, 303), (-53, 125), (34, 25),
                   (73, 250), (-37, 500), (144, 583), (449, 500)]
COLORMATRIX2_M9 = [(313, 500), (-59, 579), (-29, 617), (-210, 563), (229, 200),
                   (193, 1000), (-72, 511), (59, 200), (621, 1000)]
CAMERA_CALIBRATION = [(1, 1), (0, 1), (0, 1), (0, 1), (1, 1),
                      (0, 1), (0, 1), (0, 1), (1, 1)]
AS_SHOT_NEUTRAL = [(16384, 31315), (16384, 16384), (16384, 20850)]
CALIB_ILLUM1, CALIB_ILLUM2 = 17, 21
FOCALPLANE_X, FOCALPLANE_Y = (3729, 1), (3764, 1)

SENSDB_TARGET_FACTOR = 0.8406
DEFECT_ABS_FLOOR = 3.0
DEFECT_SIGMA_K = 8.0
AUTO_REPAIR_WARN = 12

APERTURE_CALIB = 18.7
REF_EXTBV = 3.59
FNUMBER_COMPRESS = 0.69
FNUMBER_CALIB_ORIG = 13.75
FNUMBER_DEN = 65536

SELFCAL_MIN_FRAMES = 8
BLACKFRAME_SIGNAL_FLOOR = 32

XCHECK_THRESHOLD_STOPS = 2.5

T_BYTE, T_ASCII, T_SHORT, T_LONG, T_RATIONAL = 1, 2, 3, 4, 5
T_UNDEFINED, T_SLONG, T_SRATIONAL = 7, 9, 10


@dataclass
class Options:
    inputs: list = field(default_factory=list)
    out_dir: "str | None" = None
    db_dir: "str | None" = None
    verbose: bool = False
    refresh: bool = False
    preview: bool = False
    color_m9: bool = False
    black: int = BLACK_DEFAULT
    set_black: bool = False
    lens: bool = False
    lens_code: "str | None" = None
    sensor: bool = False
    sensor_darkfield_create: bool = False
    sensor_test: bool = False
    recursive: bool = False
    jobs: int = 1
    dry_run: bool = False
    probe: "str | None" = None
    cfa: str = "RGGB"
    white: int = WHITE_DEFAULT
    no_crop: bool = False
    aperture: "float | None" = None
    mimic_fnumber: bool = False
    legacy_fnumber: bool = False
    selfcal: bool = False
    auto_lines: bool = False
    auto_repair: bool = False
    raw_offset: "int | None" = None
    raw_endian: str = "little"
    legacy_preview: bool = False
    preview_size: int = 1024
    preview_uncompressed: bool = False
    verify: bool = False
    calibrate_fnumber: "str | None" = None
    meter_offset_map: dict = field(default_factory=dict)


def _app_base_dir() -> str:
    # Where lensdb.ini / sensdb.ini live.
    # Bundled (PyInstaller): the folder that CONTAINS the .app / .exe, so the
    # INIs sit beside the app and stay user-editable.
    # Loose script: the folder holding this .py.
    if getattr(sys, "frozen", False):
        exe_dir = os.path.dirname(os.path.abspath(sys.executable))
        app_dir = exe_dir
        for _ in range(3):
            if os.path.basename(app_dir).endswith(".app"):
                return os.path.dirname(app_dir)
            app_dir = os.path.dirname(app_dir)
        return os.path.dirname(exe_dir)
    return os.path.dirname(os.path.abspath(__file__))


def _db_path(opts: Options, name: str) -> str:
    base = opts.db_dir or _app_base_dir()
    return os.path.join(base, name)


def parse_lensdb(path: str) -> dict:
    """Return {sixbitcode: {Maker, Model, SerialNo, FocalLength, Apertures[list]}}."""
    out = {}
    if not os.path.isfile(path):
        return out
    cur = None
    for raw in open(path, "r", encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith((";", "#", "%")):
            continue
        if line.startswith("[") and line.endswith("]"):
            cur = line[1:-1].strip()
            out[cur] = {"Maker": "", "Model": "", "SerialNo": "",
                        "FocalLength": None, "Apertures": []}
            continue
        if cur is None or "=" not in line:
            continue
        key, val = (s.strip() for s in line.split("=", 1))
        val = val.split("%", 1)[0].strip()
        if key == "Aperture":
            try:
                out[cur]["Apertures"].append(float(val))
            except ValueError:
                pass
        elif key == "FocalLength":
            try:
                out[cur]["FocalLength"] = float(val)
            except ValueError:
                pass
        elif key in ("Maker", "Model", "SerialNo"):
            out[cur][key] = val
    return out


def parse_sensdb(path: str) -> dict:
    """Return {serial: {'lines': [(x1,y1,x2,y2),...], 'levels': np.float32[] | None}}."""
    out = {}
    if not os.path.isfile(path):
        return out
    cur = None
    lines_buf, levels_buf, offsets_buf, imgcal_buf = [], [], {}, {}

    def flush():
        if cur is None:
            return
        lv = np.asarray(levels_buf, dtype=np.float64) if levels_buf else None
        grp = [tuple(lines_buf[i:i + 4]) for i in range(0, len(lines_buf) - 3, 4)]
        out[cur] = {"lines": grp, "levels": lv, "meter_offsets": dict(offsets_buf),
                    "image_calibs": dict(imgcal_buf)}

    for raw in open(path, "r", encoding="utf-8", errors="replace"):
        line = raw.strip()
        if not line or line.startswith((";", "#", "%")):
            continue
        if line.startswith("[") and line.endswith("]"):
            flush()
            cur = line[1:-1].strip()
            lines_buf, levels_buf, offsets_buf, imgcal_buf = [], [], {}, {}
            continue
        if cur is None or "=" not in line:
            continue
        key, val = (s.strip() for s in line.split("=", 1))
        val = val.split("%", 1)[0].strip()
        if key == "Line":
            for p in val.replace(",", " ").split():
                try:
                    lines_buf.append(int(float(p)))
                except ValueError:
                    pass
        elif key == "LevelCorrection":
            try:
                levels_buf.append(float(val))
            except ValueError:
                pass
        elif key.lower().startswith("meteroffset"):
            lc = key.split(".", 1)[1].strip() if "." in key else ""
            try:
                offsets_buf[lc] = float(val)
            except ValueError:
                pass
        elif key.lower().startswith("imagecalib"):
            lc = key.split(".", 1)[1].strip() if "." in key else ""
            try:
                imgcal_buf[lc] = float(val)
            except ValueError:
                pass
    flush()
    return out


def write_sensdb(path: str, serial: str, levels: np.ndarray, lines=None) -> None:
    """Create/replace the [serial] block (LevelCorrection + optional Line), CRLF text,
    preserving any other camera sections already present in the file."""
    sections, order = {}, []
    if os.path.isfile(path):
        cur = None
        for raw in open(path, "r", encoding="utf-8", errors="replace"):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1].strip()
                if cur not in sections:
                    sections[cur] = []
                    order.append(cur)
            elif cur is not None:
                sections[cur].append(raw.rstrip("\r\n"))
    if serial not in order:
        order.append(serial)
    keep = [ln for ln in sections.get(serial, [])
            if ln.split("=", 1)[0].strip().lower().startswith("meteroffset")]
    body = []
    for (x1, y1, x2, y2) in (lines or []):
        body.append(f"Line = {int(x1)} {int(y1)} {int(x2)} {int(y2)}")
    body += [f"LevelCorrection = {float(v):.6f}" for v in np.asarray(levels).tolist()]
    body += keep
    sections[serial] = body
    with open(path, "w", encoding="utf-8", newline="") as f:
        for sec in order:
            f.write(f"[{sec}]\r\n")
            for ln in sections.get(sec, []):
                f.write(ln + "\r\n")


def write_meter_offset(path: str, serial: str, code: str, offset: float) -> None:
    """Add/replace 'MeterOffset.<code> = <offset>' in the [serial] block, CRLF text,
    leaving every other line (Line, LevelCorrection, other lens codes) untouched."""
    sections, order = {}, []
    if os.path.isfile(path):
        cur = None
        for raw in open(path, "r", encoding="utf-8", errors="replace"):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1].strip()
                if cur not in sections:
                    sections[cur] = []
                    order.append(cur)
            elif cur is not None:
                sections[cur].append(raw.rstrip("\r\n"))
    if serial not in order:
        order.append(serial)
        sections.setdefault(serial, [])
    key = f"MeterOffset.{code}"
    body = [ln for ln in sections.get(serial, [])
            if ln.split("=", 1)[0].strip().lower() != key.lower()]
    body.append(f"{key} = {float(offset):.4f}")
    sections[serial] = body
    with open(path, "w", encoding="utf-8", newline="") as f:
        for sec in order:
            f.write(f"[{sec}]\r\n")
            for ln in sections.get(sec, []):
                f.write(ln + "\r\n")


def write_image_calib(path: str, serial: str, code: str, value: float) -> None:
    """Add/replace 'ImageCalib.<code> = <value>' in the [serial] block, CRLF text,
    leaving every other line (Line, LevelCorrection, MeterOffset, other lens codes)
    untouched. The meter-free image constant for the automatic cross-check."""
    sections, order = {}, []
    if os.path.isfile(path):
        cur = None
        for raw in open(path, "r", encoding="utf-8", errors="replace"):
            s = raw.strip()
            if s.startswith("[") and s.endswith("]"):
                cur = s[1:-1].strip()
                if cur not in sections:
                    sections[cur] = []
                    order.append(cur)
            elif cur is not None:
                sections[cur].append(raw.rstrip("\r\n"))
    if serial not in order:
        order.append(serial)
        sections.setdefault(serial, [])
    key = f"ImageCalib.{code}"
    body = [ln for ln in sections.get(serial, [])
            if ln.split("=", 1)[0].strip().lower() != key.lower()]
    body.append(f"{key} = {float(value):.4f}")
    sections[serial] = body
    with open(path, "w", encoding="utf-8", newline="") as f:
        for sec in order:
            f.write(f"[{sec}]\r\n")
            for ln in sections.get(sec, []):
                f.write(ln + "\r\n")


def calibrate_fnumber_offset(jobs, opts, sensdb_path) -> int:
    """--calibrate-fnumber: derive the per-(body,lens) meter offset from known-aperture
    frames and store it in sensdb.ini. The apertures (comma-separated) must match the
    input frames in filename order. Offset = median(Av_known + (MeasuredLV - ExtBv)),
    slope locked at -1; a handful of frames suffices (see README).

    Also derives the meter-free image constant ImageCalib = median(Av_known - (-Bv+Sv-Tv))
    from the same frames (using the same sensor-corrected green_mean the converter uses
    at runtime), and stores it alongside the offset. This is what the automatic
    golden-average cross-check compares the meter path against."""
    import statistics
    try:
        aps = [float(x) for x in str(opts.calibrate_fnumber).replace(";", ",").split(",") if x.strip()]
    except ValueError:
        log.error("--calibrate-fnumber: could not parse aperture list %r.", opts.calibrate_fnumber)
        return 2
    jobs_sorted = sorted(jobs, key=lambda j: os.path.basename(j[0]))
    if len(aps) != len(jobs_sorted):
        log.error("--calibrate-fnumber: %d apertures given (%s) but %d input frame(s) found; "
                  "the apertures must match the frames in filename order.",
                  len(aps), ",".join("%g" % a for a in aps), len(jobs_sorted))
        return 2
    sdb_all = parse_sensdb(sensdb_path)
    serial, code, samples, img_samples, rows = None, opts.lens_code, [], [], []
    for (raw, jpg, bia), ap in zip(jobs_sorted, aps):
        jm = read_jpeg_meta(jpg) if jpg and os.path.isfile(jpg) else None
        if not jm or jm.get("measured_lv") is None or jm.get("ext_brightness") is None:
            log.error("frame %s has no MeasuredLV/ExternalSensorBrightnessValue in its "
                      "MakerNote; cannot calibrate from it.", os.path.basename(raw))
            return 2
        serial = serial or jm.get("serial")
        if not code and jm.get("lens_code_raw"):
            code = str(jm.get("lens_code_raw"))
        d = float(jm["measured_lv"]) - float(jm["ext_brightness"])
        av = 2.0 * math.log2(ap)
        samples.append(av + d)
        try:
            cfa = read_raw(raw, opts)
            blk = float(opts.black)
            if bia and os.path.isfile(bia):
                cfa = np.clip(cfa - read_raw(bia, opts), 0, opts.white)
                blk = 0.0
            sdb_e = sdb_all.get(jm.get("serial"))
            if opts.sensor and not opts.sensor_test and sdb_e is not None:
                lv = sdb_e.get("levels")
                if lv is not None:
                    g = _iso_gain(jm.get("iso"))
                    cfa = apply_levels(cfa, lv * g if g != 1 else lv, opts.white)
                cfa = repair_lines(cfa, list(sdb_e.get("lines") or []), opts.white,
                                   test_mode=opts.sensor_test)
            gm = _green_mean(cfa, opts.cfa)
            et = jm.get("exposure_time"); et = (et[0] / et[1]) if et else None
            bvv = math.log2(max(gm - blk, 1.0))
            svv = math.log2(max(float(jm.get("iso") or 160), 1.0) / 100.0)
            tvv = math.log2(1.0 / (et if et and et > 0 else 1.0))
            img_samples.append(av - (-bvv + svv - tvv))
        except Exception as e:
            log.warning("    (could not read RAW %s for image-constant calibration: %s)",
                        os.path.basename(raw), e)
        rows.append((os.path.basename(raw), ap, d, av + d))
    if not serial:
        log.error("--calibrate-fnumber: no camera serial found in the frames.")
        return 2
    if not code:
        log.error("--calibrate-fnumber: no lens code -- pass -l <code> (uncoded lenses "
                  "have no code in the file).")
        return 2
    offset = statistics.median(samples)
    sd = statistics.pstdev(samples) if len(samples) > 1 else 0.0
    write_meter_offset(sensdb_path, str(serial), str(code), offset)
    log.info("Calibrated aperture-meter offset for body %s + lens %s: %.4f "
             "(median over %d frame(s), spread %.3f st) -> %s",
             serial, code, offset, len(samples), sd, sensdb_path)
    for nm, ap, d, c in rows:
        log.info("    %-18s f/%-5g  MeasLV-ExtBV = %+.3f  ->  offset %.3f", nm, ap, d, c)
    if len(samples) < 5:
        log.info("    (5-10 frames give a tighter offset; %d is usable but a touch noisy.)",
                 len(samples))
    if img_samples:
        image_calib = statistics.median(img_samples)
        isd = statistics.pstdev(img_samples) if len(img_samples) > 1 else 0.0
        write_image_calib(sensdb_path, str(serial), str(code), image_calib)
        log.info("Calibrated meter-free image constant ImageCalib for body %s + lens %s: %.4f "
                 "(median over %d frame(s), spread %.3f st). Used by the automatic cross-check to settle "
                 "frames with a corrupt external-meter reading.", serial, code, image_calib,
                 len(img_samples), isd)
        if isd > 0.5:
            log.info("    (ImageCalib spread is wide -- the calibration frames span a range of "
                     "scene luminance; the constant is most accurate near the calibration light.)")
    return 0


def resolve_meter_offsets(jobs, opts, sensdb) -> dict:
    """Batch self-calibration: for (body,lens) groups in this run that have NO stored
    MeterOffset, derive one from the camera's own ApproximateFNumber when the batch is
    large enough (>= SELFCAL_MIN_FRAMES). Returns {(serial,code): offset}. Stored offsets
    and -A/--legacy/--mimic short-circuit this (no self-cal needed/wanted)."""
    if opts.legacy_fnumber or opts.mimic_fnumber or opts.aperture:
        return {}
    import statistics
    groups = {}
    warn_unmatched = {}
    for (raw, jpg, bia) in jobs:
        jm = read_jpeg_meta(jpg) if jpg and os.path.isfile(jpg) else None
        if not jm:
            continue
        ml, eb, af = jm.get("measured_lv"), jm.get("ext_brightness"), jm.get("approx_fnumber")
        if ml is None or eb is None:
            continue
        serial = jm.get("serial")
        code = opts.lens_code or (str(jm.get("lens_code_raw")) if jm.get("lens_code_raw") else None)
        stored = (sensdb.get(serial) or {}).get("meter_offsets") if serial else None
        if stored:
            ckey = str(code) if code else None
            if ckey is not None and ckey in stored:
                continue
            if serial not in warn_unmatched:
                warn_unmatched[serial] = (ckey, tuple(sorted(stored.keys())))
        if not af or af <= 0 or not code:
            continue
        groups.setdefault((str(serial) if serial else None, str(code)), []).append(
            2.0 * math.log2(af) + (float(ml) - float(eb)))
    for serial, (ckey, avail) in warn_unmatched.items():
        log.warning("Body %s has a calibrated aperture offset (MeterOffset for lens %s) but this run "
                    "resolved lens code %r, which does not match it -- FNumber is using the image-"
                    "brightness fallback instead. Pass -l %s (with the same --db-dir) to apply the "
                    "calibration.", serial, "/".join(avail), ckey, avail[0])
    out = {}
    for key, vals in groups.items():
        if len(vals) >= SELFCAL_MIN_FRAMES:
            out[key] = statistics.median(vals)
            log.info("Self-calibrated aperture-meter offset %.4f for (body %s, lens %s) from "
                     "%d frames via the camera's ApproxF (no stored MeterOffset). Run "
                     "--calibrate-fnumber once for full precision.", out[key], key[0], key[1], len(vals))
        else:
            log.info("(body %s, lens %s): only %d frame(s) without a stored MeterOffset -- too few "
                     "to self-calibrate the aperture meter (need >=%d); falling back to the "
                     "image-brightness estimate. --calibrate-fnumber gives exact apertures.",
                     key[0], key[1], len(vals), SELFCAL_MIN_FRAMES)
    return out


def read_raw(path: str, opts: Options) -> np.ndarray:
    """Read an M8 .RAW into a float64 (RAW_H, RAW_W) array.

    Layout: RAW_HEADER_SAMPLES header samples, then RAW_H rows of RAW_ROW_STRIDE
    samples (only the first RAW_W per row are image data; the rest are discarded),
    then one trailing stride-row that is ignored. Values are clamped to 16383.
    """
    dt = "<u2" if opts.raw_endian != "big" else ">u2"
    data = np.fromfile(path, dtype=dt)
    header = RAW_HEADER_SAMPLES if opts.raw_offset is None else int(opts.raw_offset)
    need = header + RAW_H * RAW_ROW_STRIDE
    if data.size < need:
        raise ValueError(f"{os.path.basename(path)}: {data.size} samples < {need} "
                         f"required for one M8 frame.")
    img = data[header:need].reshape(RAW_H, RAW_ROW_STRIDE)[:, :RAW_W]
    img = np.minimum(img, WHITE_DEFAULT)
    return img.astype(np.float64)


def read_dng_cfa(path: str):
    """Load the CFA plane + tags from a standard M8 DNG (for re-processing)."""
    if not HAVE_TIFFFILE:
        raise RuntimeError("tifffile is required to read a DNG back in.")
    with tifffile.TiffFile(path) as tf:
        pages = []
        for p in tf.pages:
            pages.append(p)
            for sub in (getattr(p, "pages", None) or []):
                pages.append(sub)
        cfa = [p for p in pages if int(getattr(p, "photometric", 0)) == 32803]
        pool = cfa or [p for p in pages if len(p.shape) == 2]
        if not pool:
            raise ValueError("No CFA/2-D image found in DNG.")
        page = max(pool, key=lambda p: int(np.prod(p.shape)))
        data = page.asarray()
        tags = {t.name: t.value for t in page.tags}
    return data.astype(np.float64), tags


def _exif_signed_rat(value):
    """EXIF SRATIONAL fields (ShutterSpeedValue, ExposureBiasValue) are signed, but
    a reader can hand back an unsigned 32-bit numerator. Reinterpret as signed so
    e.g. a 2 s exposure's APEX ShutterSpeedValue 0xFFFF0000 reads as -65536
    (= -1.0 = -log2(2 s)) rather than 4294901760."""
    if value is None:
        return None
    def s(x):
        x = int(x)
        return x - (1 << 32) if x >= (1 << 31) else x
    return (s(value[0]), s(value[1]))


def read_jpeg_meta(path: str) -> "dict | None":
    """Parse the JPG sidecar's EXIF. Returns a dict of the fields the DNG needs
    (each value already in the form the writer wants), plus the verbatim MakerNote
    bytes and the camera serial. Returns None if no usable EXIF is found."""
    if not path or not os.path.isfile(path):
        return None
    try:
        d = open(path, "rb").read()
    except Exception:
        return None
    i = d.find(b"Exif\x00\x00")
    if i < 0:
        return None
    tb = i + 6
    if d[tb:tb + 2] not in (b"II", b"MM"):
        return None
    bo = "<" if d[tb:tb + 2] == b"II" else ">"

    def u16(o): return struct.unpack(bo + "H", d[tb + o:tb + o + 2])[0]
    def u32(o): return struct.unpack(bo + "I", d[tb + o:tb + o + 4])[0]

    def entries(ifd_off):
        try:
            n = u16(ifd_off)
        except Exception:
            return []
        out, p = [], ifd_off + 2
        for _ in range(n):
            tag, typ, cnt = struct.unpack(bo + "HHI", d[tb + p:tb + p + 8])
            out.append((tag, typ, cnt, d[tb + p + 8:tb + p + 12]))
            p += 12
        return out

    sizes = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 7: 1, 9: 4, 10: 8, 11: 4, 12: 8}

    def raw_of(typ, cnt, vo):
        sz = sizes.get(typ, 1) * cnt
        if sz <= 4:
            return vo[:sz]
        off = struct.unpack(bo + "I", vo)[0]
        return d[tb + off:tb + off + sz]

    def rationals(typ, cnt, vo):
        sz = sizes[typ] * cnt
        off = struct.unpack(bo + "I", vo)[0] if sz > 4 else None
        base = (tb + off) if off is not None else None
        fmt = "II" if typ == 5 else "ii"
        res = []
        for k in range(cnt):
            if base is not None:
                res.append(struct.unpack(bo + fmt, d[base + 8 * k:base + 8 * k + 8]))
            else:
                res.append(struct.unpack(bo + fmt, vo[8 * k:8 * k + 8]))
        return res

    ifd0 = entries(u32(4))
    exif_off = None
    jpeg_software = None
    for tag, typ, cnt, vo in ifd0:
        if tag == 34665:
            exif_off = struct.unpack(bo + "I", vo)[0]
        elif tag == 305 and typ in (2, 7):
            try:
                jpeg_software = raw_of(typ, cnt, vo).split(b"\x00")[0].decode("latin1").strip()
            except Exception:
                jpeg_software = None
    ex = entries(exif_off) if exif_off else []

    m = {}
    makernote = None
    for tag, typ, cnt, vo in ex:
        if tag == 37500:
            off = struct.unpack(bo + "I", vo)[0] if (sizes.get(typ, 1) * cnt) > 4 else None
            makernote = d[tb + off:tb + off + cnt] if off is not None else vo[:cnt]
            continue
        if typ in (5, 10):
            m[tag] = rationals(typ, cnt, vo)
        elif typ == 3:
            vals = struct.unpack(bo + "%dH" % cnt, raw_of(typ, cnt, vo)[:2 * cnt])
            m[tag] = vals[0] if cnt == 1 else vals
        elif typ in (2, 7):
            m[tag] = raw_of(typ, cnt, vo)
        elif typ == 4:
            vals = struct.unpack(bo + "%dI" % cnt, raw_of(typ, cnt, vo)[:4 * cnt])
            m[tag] = vals[0] if cnt == 1 else vals

    def as_str(b):
        return b.split(b"\x00")[0].decode("latin1") if isinstance(b, (bytes, bytearray)) else str(b)

    serial, lens_code = None, 0
    wb = {}
    ext_bv = None
    meas_lv = None
    approx_fnumber = None
    if makernote and makernote[:5] == b"LEICA":
        try:
            n = struct.unpack("<H", makernote[8:10])[0]
            for k in range(n):
                o = 10 + k * 12
                tg, ty, ct = struct.unpack("<HHI", makernote[o:o + 8])
                val = struct.unpack("<I", makernote[o + 8:o + 12])[0]
                if tg == 0x0303:
                    serial = val
                elif tg == 0x0310:
                    lens_code = val
                elif tg in (0x0322, 0x0323, 0x0324) and ty == 5:
                    roff = 8 + val
                    num, den = struct.unpack("<II", makernote[roff:roff + 8])
                    wb[tg] = (int(num), int(den))
                elif tg == 0x0311 and ty == 5:
                    roff = 8 + val
                    num, den = struct.unpack("<II", makernote[roff:roff + 8])
                    num = num - (1 << 32) if num >= (1 << 31) else num
                    ext_bv = (num / den) if den else None
                elif tg == 0x0312 and ty == 5:
                    roff = 8 + val
                    num, den = struct.unpack("<II", makernote[roff:roff + 8])
                    num = num - (1 << 32) if num >= (1 << 31) else num
                    meas_lv = (num / den) if den else None
                elif tg == 0x0313 and ty == 5:
                    roff = 8 + val
                    num, den = struct.unpack("<II", makernote[roff:roff + 8])
                    approx_fnumber = (num / den) if den else None
        except Exception:
            pass
    as_shot_neutral = None
    if all(t in wb for t in (0x0322, 0x0323, 0x0324)):
        as_shot_neutral = [wb[0x0322], wb[0x0323], wb[0x0324]]

    out = {
        "exposure_time": m.get(33434, [None])[0] if isinstance(m.get(33434), list) else None,
        "fnumber": (m.get(33437, [None])[0] if isinstance(m.get(33437), list) else None),
        "iso": m.get(34855),
        "exif_version": m.get(36864, b"0220"),
        "datetime_original": as_str(m.get(36867, b"")),
        "datetime_digitized": as_str(m.get(36868, b"")),
        "shutter_speed": _exif_signed_rat(m.get(37377, [None])[0]) if isinstance(m.get(37377), list) else None,
        "exposure_bias": _exif_signed_rat(m.get(37380, [None])[0]) if isinstance(m.get(37380), list) else None,
        "max_aperture": (m.get(37381, [None])[0] if isinstance(m.get(37381), list) else None),
        "metering_mode": m.get(37383, 2),
        "light_source": m.get(37384, 0),
        "flash": m.get(37385, 0),
        "focal_length": (m.get(37386, [None])[0] if isinstance(m.get(37386), list) else None),
        "exposure_program": m.get(34850, 1),
        "white_balance": m.get(41987, 1),
        "digital_zoom": (m.get(41988, [None])[0] if isinstance(m.get(41988), list) else None),
        "focal_length_35": m.get(41989, 0),
        "scene_capture": m.get(41990, 0),
        "image_unique_id": as_str(m.get(42016, b"")),
        "makernote": bytes(makernote) if makernote else None,
        "serial": str(serial) if serial is not None else None,
        "software": jpeg_software or None,
        "lens_code_raw": lens_code,
        "as_shot_neutral": as_shot_neutral,
        "ext_brightness": ext_bv,
        "measured_lv": meas_lv,
        "approx_fnumber": approx_fnumber,
    }
    return out


def _local_median(profile: np.ndarray, half: int = 8) -> np.ndarray:
    n = profile.size
    out = np.empty(n)
    for c in range(n):
        a = max(0, c - half)
        b = min(n, c + half + 1)
        out[c] = np.median(profile[a:b])
    return out


def detect_defect_columns(colmean: np.ndarray) -> np.ndarray:
    """Return indices of columns deviating strongly from the local trend."""
    dev = colmean - _local_median(colmean, 8)
    mad = np.median(np.abs(dev - np.median(dev))) or 1.0
    sigma = 1.4826 * mad
    thr = max(DEFECT_ABS_FLOOR, DEFECT_SIGMA_K * sigma)
    return np.where(np.abs(dev) > thr)[0]


def compute_levels(colmean: np.ndarray, zero_defects: bool = False):
    """-sd core: LevelCorrection[c] = max(0, colmean[c] - T), with
    T = SENSDB_TARGET_FACTOR * mean(colmean).

    This reproduces the reference tool for ~99.95% of columns (the reference
    applies the plain formula to every column).  Detected defect columns are
    still returned so the caller can emit Line repair entries; they are only
    forced to 0 here when ``zero_defects`` is set (used together with
    --auto-lines, where such columns are instead fixed by neighbour
    interpolation and so must not also carry a large LevelCorrection)."""
    defects = detect_defect_columns(colmean)
    target = SENSDB_TARGET_FACTOR * float(colmean.mean())
    levels = np.maximum(0.0, colmean - target)
    if zero_defects:
        levels[defects] = 0.0
    return levels.astype(np.float64), defects


def _iso_gain(iso, base_iso: int = 160) -> int:
    """Analog gain of an M8 frame relative to base ISO 160, quantised to whole
    stops.  The camera's real ISO steps (160/320/640/1250/2500) are successive
    doublings, so the gain is 2**round(log2(ISO/160)) = 1, 2, 4, 8, 16.

    When a base-ISO sensor database is applied to a higher-ISO frame under -s,
    m8raw2dng2 scales BOTH the darkfield LevelCorrection AND (when -b is also
    given) the written BlackLevel by this factor:
    out == round(raw - gain*LevelCorrection) and BlackLevel == base_black*gain
    (e.g. 92 -> 1472 at gain x16).  Under plain -b (no -s) m8raw2dng2 writes a
    flat BlackLevel.  The original writes no BlackLevel on a bare conversion, but
    under -b it writes base_black*gain (ISO-scaled, tag-only); so the two agree
    under -s -b and diverge under plain -b at high ISO.
    Returns 1 for base ISO or unknown ISO, so the base-ISO path (a raw
    passthrough, byte-identical to the original except FNumber) is untouched."""
    try:
        iso = float(iso)
    except (TypeError, ValueError):
        return 1
    if iso <= 0:
        return 1
    return int(2 ** round(math.log2(iso / float(base_iso))))


def apply_levels(cfa: np.ndarray, levels: np.ndarray, white: int) -> np.ndarray:
    """-s core: subtract the per-column LevelCorrection (a constant per column),
    round and clamp to [0, white]."""
    if levels is None or levels.size != cfa.shape[1]:
        return cfa
    out = cfa - levels[None, :]
    np.round(out, out=out)
    np.clip(out, 0, white, out=out)
    log.info("Applied sensor darkfield: subtracted LevelCorrection from %d columns.",
             cfa.shape[1])
    return out


def repair_lines(cfa: np.ndarray, lines, white: int, test_mode: bool = False) -> np.ndarray:
    """Repair vertical defect columns from same-Bayer-colour neighbours (x-2, x+2),
    or paint them white in test mode."""
    if not lines:
        return cfa
    h, w = cfa.shape
    for (x1, y1, x2, y2) in lines:
        x = int(x1)
        ylo, yhi = sorted((int(y1), int(y2)))
        ylo, yhi = max(0, ylo), min(h - 1, yhi)
        if not (0 <= x < w):
            continue
        if test_mode:
            cfa[ylo:yhi + 1, x] = white
            continue
        if 0 <= x - 2 and x + 2 < w:
            cfa[ylo:yhi + 1, x] = 0.5 * (cfa[ylo:yhi + 1, x - 2] + cfa[ylo:yhi + 1, x + 2])
        elif 0 <= x - 1 and x + 1 < w:
            cfa[ylo:yhi + 1, x] = 0.5 * (cfa[ylo:yhi + 1, x - 1] + cfa[ylo:yhi + 1, x + 1])
        log.info("Repaired vertical line at column x=%d (rows %d-%d).", x, ylo, yhi)
    return cfa


def estimate_fnumber(green_mean: float, black: float, exposure_time, iso,
                     mimic_original: bool = False, ext_bv: "float | None" = None,
                     meas_lv: "float | None" = None,
                     meter_offset: "float | None" = None,
                     image_calib: "float | None" = None) -> float:
    """Estimate the aperture from image brightness via the APEX exposure relation
    Av = -Bv + Sv - Tv + C, with C a sensor constant calibrated so a correctly
    exposed frame recovers the aperture actually used.

    This is a best-effort fallback, used only when neither -A nor a coded-lens
    FNumber is available. It is NOT byte-identical to the original tool, whose
    FNumber is a compressed brightness heuristic that does not recover the true
    aperture (it reports, e.g., f/16 as ~f/8) and whose constant is unrecoverable.
    For an exact value, pass -A.

    When ext_bv is supplied (the camera's external light-meter reading, Leica
    MakerNote ExternalSensorBrightnessValue), it is used as the scene-luminance
    term -- Av = ExtBv - Bv + Sv - Tv + (APERTURE_CALIB - REF_EXTBV) -- instead of
    folding an *assumed* luminance into the constant. The meter reads the scene
    independently of the lens, so this resolves the fundamental ambiguity of the
    meter-free estimate (a dim scene wide-open vs a bright scene stopped down look
    identical in image brightness alone). It is anchored so a correctly-exposed
    frame lands on exactly the meter-free answer; only off-luminance scenes move.
    Measured on 26 uncoded-lens M8 frames: 1.17 -> 0.32 stops mean error.

    With mimic_original=True the original's compressed heuristic is reproduced
    instead (~0.69 factor on the brightness/shutter terms); on base-ISO scenes
    this lands on the original's value about two thirds of the time, but it is
    less accurate than either path above and does not hold across scenes or ISO."""
    s = max(float(green_mean) - float(black), 1.0)
    bv = math.log2(s)
    sv = math.log2(max(float(iso or 160), 1.0) / 100.0)
    t = float(exposure_time) if exposure_time and exposure_time > 0 else 1.0
    tv = math.log2(1.0 / t)
    if mimic_original:
        av = FNUMBER_COMPRESS * (-bv + sv - tv) + FNUMBER_CALIB_ORIG
    elif meas_lv is not None and ext_bv is not None and meter_offset is not None:
        av = -(float(meas_lv) - float(ext_bv)) + float(meter_offset)
    elif ext_bv is not None and s >= BLACKFRAME_SIGNAL_FLOOR:
        av = float(ext_bv) - bv + sv - tv + (APERTURE_CALIB - REF_EXTBV)
    else:
        av = -bv + sv - tv + (float(image_calib) if image_calib is not None else APERTURE_CALIB)
    return min(max(2.0 ** (av / 2.0), 1.0), 45.0)


def snap_aperture(value, apertures):
    if not apertures or value is None or value <= 0:
        return value
    return min(apertures, key=lambda a: abs(math.log2(value) - math.log2(a)))


def _pack_value(typ: int, value):
    """Return (raw_bytes, count) for a tag value."""
    def _s32(x):
        x = int(x)
        return x - (1 << 32) if x >= (1 << 31) else x
    if typ == T_ASCII:
        b = (value if isinstance(value, str) else str(value)).encode("latin1") + b"\x00"
        return b, len(b)
    if typ in (T_BYTE, T_UNDEFINED):
        b = bytes(value)
        return b, len(b)
    if typ in (T_SHORT, T_LONG, T_SLONG):
        vals = value if isinstance(value, (list, tuple)) else (value,)
        if typ == T_SLONG:
            vals = [_s32(v) for v in vals]
        fmt = {T_SHORT: "H", T_LONG: "I", T_SLONG: "i"}[typ]
        return struct.pack("<%d%s" % (len(vals), fmt), *vals), len(vals)
    if typ in (T_RATIONAL, T_SRATIONAL):
        fmt = "I" if typ == T_RATIONAL else "i"
        flat = []
        for nd in value:
            if typ == T_SRATIONAL:
                flat += [_s32(nd[0]), _s32(nd[1])]
            else:
                flat += [int(nd[0]), int(nd[1])]
        return struct.pack("<%d%s" % (len(flat), fmt), *flat), len(value)
    raise ValueError("unsupported TIFF type %r" % typ)


def _prepare_ifd(entries):
    """entries: list of (tag, type, value); value may be a sentinel '@...' for a
    pointer (resolved later as a LONG). Returns (sorted_entries, items, pool_bytes,
    rel_offsets, ifd_size)."""
    entries = sorted(entries, key=lambda e: e[0])
    items, pool, rel = [], bytearray(), {}
    for idx, (tag, typ, value) in enumerate(entries):
        if isinstance(value, str) and value.startswith("@"):
            items.append((tag, typ, "ptr", value))
            continue
        raw, count = _pack_value(typ, value)
        if len(raw) <= 4:
            items.append((tag, typ, "inline", (count, raw)))
        else:
            if len(pool) % 2:
                pool += b"\x00"
            rel[idx] = len(pool)
            pool += raw
            items.append((tag, typ, "pool", (count, idx)))
    if len(pool) % 2:
        pool += b"\x00"
    size = 2 + 12 * len(entries) + 4
    return entries, items, bytes(pool), rel, size


def build_dng_bytes(ifd0, exif, strip, preview_ifd=None, preview_strip=None) -> bytes:
    """Assemble a little-endian DNG. Layout mirrors the original exactly:
    header, IFD0, IFD0-pool, ExifIFD, ExifIFD-pool, [PreviewIFD, pool,] strip,
    [preview strip]. Pointer sentinels: @EXIF, @STRIP, @SUBIFD, @PREVSTRIP."""
    e0, it0, pool0, rel0, sz0 = _prepare_ifd(ifd0)
    eE, itE, poolE, relE, szE = _prepare_ifd(exif)

    ifd0_off = 8
    ifd0_pool_off = ifd0_off + sz0
    exif_off = ifd0_pool_off + len(pool0)
    exif_pool_off = exif_off + szE
    cur = exif_pool_off + len(poolE)

    prev_off = prev_pool_off = None
    eP = itP = poolP = relP = szP = None
    if preview_ifd is not None:
        eP, itP, poolP, relP, szP = _prepare_ifd(preview_ifd)
        prev_off = cur
        prev_pool_off = prev_off + szP
        cur = prev_pool_off + len(poolP)
    if cur % 2:
        cur += 1
    strip_off = cur
    prev_strip_off = None
    after = strip_off + len(strip)
    if preview_strip is not None:
        if after % 2:
            after += 1
        prev_strip_off = after

    sentinels = {"@EXIF": exif_off, "@STRIP": strip_off,
                 "@SUBIFD": prev_off, "@PREVSTRIP": prev_strip_off}

    def emit(entries, items, pool_off, rel):
        b = bytearray(struct.pack("<H", len(entries)))
        for idx, (tag, typ, kind, payload) in enumerate(items):
            if kind == "ptr":
                b += struct.pack("<HHII", tag, typ, 1, sentinels[payload])
            elif kind == "inline":
                count, raw = payload
                b += struct.pack("<HHI", tag, typ, count) + raw + b"\x00" * (4 - len(raw))
            else:
                count, i = payload
                b += struct.pack("<HHII", tag, typ, count, pool_off + rel[i])
        b += struct.pack("<I", 0)
        return bytes(b)

    buf = bytearray(b"II" + struct.pack("<HI", 42, ifd0_off))
    buf += emit(e0, it0, ifd0_pool_off, rel0) + pool0
    buf += emit(eE, itE, exif_pool_off, relE) + poolE
    if preview_ifd is not None:
        buf += emit(eP, itP, prev_pool_off, relP) + poolP
    if len(buf) % 2:
        buf += b"\x00"
    assert len(buf) == strip_off, (len(buf), strip_off)
    buf += strip
    if preview_strip is not None:
        if len(buf) % 2:
            buf += b"\x00"
        assert len(buf) == prev_strip_off
        buf += preview_strip
    return bytes(buf)


def _rat_from(value):
    """(num,den) tuple -> (num,den); None -> (0,1)."""
    if value is None:
        return (0, 1)
    return (int(value[0]), int(value[1]))


def build_ifd0(opts, jm, serial, fnumber):
    """Construct the IFD0 entry list (with pointer sentinels)."""
    model = MODEL_M8RAW if opts.color_m9 else MODEL_M8
    cm1 = COLORMATRIX1_M9 if opts.color_m9 else COLORMATRIX1_M8
    cm2 = COLORMATRIX2_M9 if opts.color_m9 else COLORMATRIX2_M8
    if not (jm and jm.get("as_shot_neutral")):
        asn = AS_SHOT_NEUTRAL
    else:
        asn = jm["as_shot_neutral"]
    e = [
        (254, T_LONG, 0),
        (256, T_LONG, RAW_W),
        (257, T_LONG, RAW_H),
        (258, T_SHORT, 16),
        (259, T_SHORT, 1),
        (262, T_SHORT, 32803),
        (271, T_ASCII, EXIF_MAKE),
        (272, T_ASCII, model),
        (273, T_LONG, "@STRIP"),
        (274, T_SHORT, 1),
        (277, T_SHORT, 1),
        (278, T_LONG, RAW_H),
        (279, T_LONG, RAW_W * RAW_H * 2),
        (282, T_RATIONAL, [(300, 1)]),
        (283, T_RATIONAL, [(300, 1)]),
        (284, T_SHORT, 1),
        (296, T_SHORT, 2),
        *([(305, T_ASCII, jm["software"])] if (jm and jm.get("software")) else []),
        (315, T_ASCII, ""),
        (33421, T_SHORT, (2, 2)),
        (33422, T_BYTE, CFA_BYTES.get(opts.cfa, CFA_BYTES["RGGB"])),
        (33432, T_ASCII, ""),
        (34859, T_SHORT, 0),
        (37390, T_RATIONAL, [FOCALPLANE_X]),
        (37391, T_RATIONAL, [FOCALPLANE_Y]),
        (37392, T_SHORT, 2),
        (37398, T_BYTE, b"\x00\x00\x00\x01"),
        (50706, T_BYTE, b"\x01\x04\x00\x00"),
        (50708, T_ASCII, UNIQUE_MODEL),
        (50717, T_LONG, int(opts.white)),
        (50719, T_SHORT, ((0, 0) if opts.no_crop else (CROP, CROP))),
        (50720, T_SHORT, ((RAW_W, RAW_H) if opts.no_crop else (DNG_W, DNG_H))),
        (50721, T_SRATIONAL, cm1),
        (50722, T_SRATIONAL, cm2),
        (50723, T_SRATIONAL, CAMERA_CALIBRATION),
        (50724, T_SRATIONAL, CAMERA_CALIBRATION),
        (50728, T_RATIONAL, asn),
        (50731, T_RATIONAL, [(1, 1)]),
        (50732, T_RATIONAL, [(1, 1)]),
        (50733, T_LONG, 500),
        (50738, T_RATIONAL, [(0, 1)]),
        (50741, T_SHORT, 1),
        (50778, T_SHORT, CALIB_ILLUM1),
        (50779, T_SHORT, CALIB_ILLUM2),
    ]
    if jm is not None:
        e.append((34665, T_LONG, "@EXIF"))
        e.append((36867, T_ASCII, jm.get("datetime_original") or ""))
        if serial:
            e.append((50735, T_ASCII, str(serial)))
    if opts.set_black:
        e.append((50713, T_SHORT, (1, 1)))
        e.append((50714, T_SHORT, int(opts.black)))
    if opts.preview:
        e.append((330, T_LONG, "@SUBIFD"))
    return e


def build_exif(opts, jm, fnumber, lens_info):
    """Construct the ExifIFD entry list."""
    bias = jm.get("exposure_bias")
    if bias and bias[1]:
        bias_thirds = (int(round((bias[0] / bias[1]) * 3)), 3)
    else:
        bias_thirds = (0, 3)
    shutter = _rat_from(jm.get("shutter_speed"))
    focal = _rat_from(jm.get("focal_length"))
    max_ap = _rat_from(jm.get("max_aperture"))
    fl35 = int(jm.get("focal_length_35") or 0)

    if lens_info:
        if lens_info.get("FocalLength"):
            f = float(lens_info["FocalLength"])
            focal = (int(round(f * 1000)), 1000)
            fl35 = int(round(f * 1.33))
        if lens_info.get("Apertures"):
            wide = min(lens_info["Apertures"])
            max_ap = (int(2.0 * math.log2(wide) * 1000.0 - 1e-6), 1000)

    fnum_rat = (int(round(fnumber * FNUMBER_DEN)), FNUMBER_DEN)

    e = [
        (33434, T_RATIONAL, [_rat_from(jm.get("exposure_time"))]),
        (33437, T_RATIONAL, [fnum_rat]),
        (34850, T_SHORT, int(jm.get("exposure_program") or 1)),
        (34855, T_SHORT, int(jm.get("iso") or 160)),
        (36864, T_UNDEFINED, jm.get("exif_version") or b"0220"),
        (36868, T_ASCII, jm.get("datetime_digitized") or ""),
        (37377, T_SRATIONAL, [shutter]),
        (37380, T_SRATIONAL, [bias_thirds]),
        (37381, T_RATIONAL, [max_ap]),
        (37383, T_SHORT, int(jm.get("metering_mode") or 2)),
        (37384, T_SHORT, int(jm.get("light_source") or 0)),
        (37385, T_SHORT, int(jm.get("flash") or 0)),
        (37386, T_RATIONAL, [focal]),
        (41728, T_UNDEFINED, b"\x03"),
        (41729, T_UNDEFINED, b"\x01"),
        (41987, T_SHORT, int(jm.get("white_balance") or 1)),
        (41988, T_RATIONAL, [_rat_from(jm.get("digital_zoom"))]),
        (41989, T_SHORT, fl35),
        (41990, T_SHORT, int(jm.get("scene_capture") or 0)),
        (42016, T_ASCII, jm.get("image_unique_id") or ""),
    ]
    if lens_info:
        if lens_info.get("Maker"):
            e.append((42035, T_ASCII, str(lens_info["Maker"])))
        if lens_info.get("Model"):
            e.append((42036, T_ASCII, str(lens_info["Model"])))
        if lens_info.get("SerialNo"):
            e.append((42037, T_ASCII, str(lens_info["SerialNo"])))
    if jm.get("makernote"):
        e.append((37500, T_UNDEFINED, jm["makernote"]))
    return e


def build_preview_ifd(width, height):
    """SubIFD describing an embedded JPEG preview (Compression=7)."""
    return [
        (254, T_LONG, 1),
        (256, T_LONG, int(width)),
        (257, T_LONG, int(height)),
        (258, T_SHORT, (8, 8, 8)),
        (259, T_SHORT, 7),
        (262, T_SHORT, 6),
        (273, T_LONG, "@PREVSTRIP"),
        (277, T_SHORT, 3),
        (278, T_LONG, int(height)),
        (279, T_LONG, "@PREVSTRIPLEN"),
        (284, T_SHORT, 1),
    ]


PREVIEW_LONG = 1024


def _preview_dims(src_w, src_h, long_edge):
    """Preview pixel size that preserves the source aspect (no stretch) with the
    requested long edge - so a 3:2 frame stays 3:2 instead of being squeezed to 4:3."""
    long_edge = max(int(long_edge), 64)
    if src_w >= src_h:
        return long_edge, max(1, int(round(src_h * long_edge / src_w)))
    return max(1, int(round(src_w * long_edge / src_h))), long_edge


def _clean_preview_array(arr):
    """Tidy the RGB preview so high-ISO / near-black frames read clean in macOS
    Finder. (1) Hot pixels / specks: at preview scale a baked-in hot pixel becomes a
    1-2px bright blob, so we compare each pixel to its 5x5 median and, where it is
    >14 brighter in any channel AND sits in a dark neighbourhood (median < 56),
    replace it with that median - dilating the mask by 1px to mop up the blob halo.
    The camera bakes amplifier / hot-pixel specks into its JPEG while the raw is
    corrected, so Photoshop never shows them. (2) Shadow chroma: very dark pixels
    (luma < 22) carry magenta/green chroma noise; we fade those toward neutral grey
    in proportion to how dark they are, killing the coloured 'corruption' look
    without touching real (brighter) detail. (3) A predominantly black frame
    (mean < 12) gets a gentle blur to smooth residual grain. Bright / normal
    previews are left essentially unchanged - only dark-area specks are touched."""
    from PIL import Image, ImageFilter
    med = np.asarray(Image.fromarray(arr).filter(ImageFilter.MedianFilter(5)), np.int16)
    a = arr.astype(np.int16)
    hot = ((a - med).max(axis=2) > 14) & (med.max(axis=2) < 56)
    hot = np.asarray(Image.fromarray((hot * 255).astype(np.uint8)).filter(
        ImageFilter.MaxFilter(3))) > 0
    out = arr.copy()
    out[hot] = med.astype(np.uint8)[hot]
    o = out.astype(np.float32)
    luma = o.max(axis=2)
    f = np.clip(1.0 - luma / 22.0, 0.0, 1.0)[..., None]
    grey = o.mean(axis=2, keepdims=True)
    out = np.clip(o * (1.0 - f) + grey * f, 0, 255).astype(np.uint8)
    if float(out.mean()) < 12.0:
        out = np.asarray(Image.fromarray(out).filter(ImageFilter.GaussianBlur(0.7)))
    return np.ascontiguousarray(out, dtype=np.uint8)


def _rgb_preview_from_jpeg(jpeg_bytes, long_edge=PREVIEW_LONG):
    """Downsample the camera JPEG to a chunky-RGB preview at the requested long
    edge, preserving aspect (no 4:3 stretch), then tidy hot pixels / shadow chroma
    so near-black frames read clean in Finder. Returns (rgb_ndarray, width, height)."""
    from PIL import Image
    import io
    im = Image.open(io.BytesIO(jpeg_bytes)).convert("RGB")
    w, h = _preview_dims(im.width, im.height, long_edge)
    im = im.resize((w, h), Image.LANCZOS)
    return _clean_preview_array(np.asarray(im)), w, h


def _rgb_preview_from_raw(cfa, opts, long_edge=PREVIEW_LONG):
    """Fallback preview when no JPEG sidecar is present: a gamma-mapped luminance
    thumbnail off the mosaic (not demosaiced, but valid), at the requested long
    edge with correct aspect. Returns (rgb_ndarray, width, height)."""
    from PIL import Image
    a = np.asarray(cfa, np.float64)
    blk = float(opts.black) if opts.set_black else 0.0
    a = np.clip((a - blk) / max(float(opts.white) - blk, 1.0), 0.0, 1.0) ** (1.0 / 2.2)
    src_h, src_w = a.shape[:2]
    w, h = _preview_dims(src_w, src_h, long_edge)
    im = Image.fromarray((a * 255.0).astype(np.uint8)).resize((w, h), Image.BILINEAR).convert("RGB")
    return _clean_preview_array(np.asarray(im)), w, h


def _encode_preview_jpeg(rgb, quality=90):
    """Encode the cleaned RGB preview as a compact baseline JPEG (YCbCr 4:2:0) - the
    same format Adobe Camera Raw and QuickLook use for DNG previews. At 1024x683
    this is ~30-150 KB versus ~2 MB uncompressed, with no visible loss at preview
    scale, so --legacy-preview files stay close to the raw-only size."""
    from PIL import Image
    import io
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb, np.uint8)).save(
        buf, format="JPEG", quality=quality, subsampling=2, optimize=True)
    return buf.getvalue()


def build_raw_subifd(opts):
    """Raw-image SubIFD (NewSubfileType=0) for the standard layout: the CFA
    geometry / black / white / crop tags, with the raw strip pointer @RAWSTRIP."""
    e = [
        (254, T_LONG, 0),
        (256, T_LONG, RAW_W),
        (257, T_LONG, RAW_H),
        (258, T_SHORT, 16),
        (259, T_SHORT, 1),
        (262, T_SHORT, 32803),
        (273, T_LONG, "@RAWSTRIP"),
        (277, T_SHORT, 1),
        (278, T_LONG, RAW_H),
        (279, T_LONG, RAW_W * RAW_H * 2),
        (282, T_RATIONAL, [(300, 1)]),
        (283, T_RATIONAL, [(300, 1)]),
        (284, T_SHORT, 1),
        (296, T_SHORT, 2),
        (33421, T_SHORT, (2, 2)),
        (33422, T_BYTE, CFA_BYTES.get(opts.cfa, CFA_BYTES["RGGB"])),
        (50717, T_LONG, int(opts.white)),
        (50719, T_SHORT, ((0, 0) if opts.no_crop else (CROP, CROP))),
        (50720, T_SHORT, ((RAW_W, RAW_H) if opts.no_crop else (DNG_W, DNG_H))),
        (50733, T_LONG, 500),
        (50738, T_RATIONAL, [(0, 1)]),
    ]
    if opts.set_black:
        e.append((50713, T_SHORT, (1, 1)))
        e.append((50714, T_SHORT, int(opts.black)))
    return e


def build_ifd0_preview(opts, jm, serial, prev_w, prev_h, prev_len, jpeg=True):
    """IFD0 for the standard layout: the reduced-resolution preview image (a
    baseline JPEG by default, or uncompressed RGB with --preview-uncompressed) plus
    the camera profile (colour matrices, AsShotNeutral, calibration, illuminants),
    the Exif pointer, and SubIFDs -> raw."""
    model = MODEL_M8RAW if opts.color_m9 else MODEL_M8
    cm1 = COLORMATRIX1_M9 if opts.color_m9 else COLORMATRIX1_M8
    cm2 = COLORMATRIX2_M9 if opts.color_m9 else COLORMATRIX2_M8
    if not (jm and jm.get("as_shot_neutral")):
        asn = AS_SHOT_NEUTRAL
    else:
        asn = jm["as_shot_neutral"]
    e = [
        (254, T_LONG, 1),
        (256, T_LONG, int(prev_w)),
        (257, T_LONG, int(prev_h)),
        (258, T_SHORT, (8, 8, 8)),
        (259, T_SHORT, 7 if jpeg else 1),
        (262, T_SHORT, 6 if jpeg else 2),
        (271, T_ASCII, EXIF_MAKE),
        (272, T_ASCII, model),
        (273, T_LONG, "@PREVSTRIP"),
        (274, T_SHORT, 1),
        (277, T_SHORT, 3),
        (278, T_LONG, int(prev_h)),
        (279, T_LONG, int(prev_len)),
        (282, T_RATIONAL, [(72, 1)]),
        (283, T_RATIONAL, [(72, 1)]),
        (284, T_SHORT, 1),
        (296, T_SHORT, 2),
        *([(305, T_ASCII, jm["software"])] if (jm and jm.get("software")) else []),
        (315, T_ASCII, ""),
        (330, T_LONG, "@RAWSUBIFD"),
        (33432, T_ASCII, ""),
        (34859, T_SHORT, 0),
        (37390, T_RATIONAL, [FOCALPLANE_X]),
        (37391, T_RATIONAL, [FOCALPLANE_Y]),
        (37392, T_SHORT, 2),
        (37398, T_BYTE, b"\x00\x00\x00\x01"),
        (50706, T_BYTE, b"\x01\x04\x00\x00"),
        (50708, T_ASCII, UNIQUE_MODEL),
        (50721, T_SRATIONAL, cm1),
        (50722, T_SRATIONAL, cm2),
        (50723, T_SRATIONAL, CAMERA_CALIBRATION),
        (50724, T_SRATIONAL, CAMERA_CALIBRATION),
        (50728, T_RATIONAL, asn),
        (50731, T_RATIONAL, [(1, 1)]),
        (50732, T_RATIONAL, [(1, 1)]),
        (50741, T_SHORT, 1),
        (50778, T_SHORT, CALIB_ILLUM1),
        (50779, T_SHORT, CALIB_ILLUM2),
    ]
    if jpeg:
        e += [
            (529, T_RATIONAL, [(299, 1000), (587, 1000), (114, 1000)]),
            (530, T_SHORT, (2, 2)),
            (531, T_SHORT, 1),
            (532, T_RATIONAL, [(0, 1), (255, 1), (128, 1), (255, 1), (128, 1), (255, 1)]),
            (50970, T_LONG, 2),
        ]
    if jm is not None:
        e.append((34665, T_LONG, "@EXIF"))
        e.append((36867, T_ASCII, jm.get("datetime_original") or ""))
        if serial:
            e.append((50735, T_ASCII, str(serial)))
    return e


def build_dng_bytes_legacy(ifd0, exif, raw_strip, raw_subifd, preview_strip) -> bytes:
    """Assemble the standard layout: header, IFD0(preview), pool, ExifIFD, pool,
    raw SubIFD, pool, preview strip, raw strip. Sentinels: @EXIF, @RAWSUBIFD,
    @PREVSTRIP, @RAWSTRIP."""
    e0, it0, pool0, rel0, sz0 = _prepare_ifd(ifd0)
    eE, itE, poolE, relE, szE = _prepare_ifd(exif)
    eR, itR, poolR, relR, szR = _prepare_ifd(raw_subifd)

    ifd0_off = 8
    ifd0_pool_off = ifd0_off + sz0
    exif_off = ifd0_pool_off + len(pool0)
    exif_pool_off = exif_off + szE
    raw_ifd_off = exif_pool_off + len(poolE)
    raw_pool_off = raw_ifd_off + szR
    cur = raw_pool_off + len(poolR)
    if cur % 2:
        cur += 1
    prev_strip_off = cur
    after = prev_strip_off + len(preview_strip)
    if after % 2:
        after += 1
    raw_strip_off = after

    sentinels = {"@EXIF": exif_off, "@RAWSUBIFD": raw_ifd_off,
                 "@PREVSTRIP": prev_strip_off, "@RAWSTRIP": raw_strip_off}

    def emit(entries, items, pool_off, rel):
        b = bytearray(struct.pack("<H", len(entries)))
        for idx, (tag, typ, kind, payload) in enumerate(items):
            if kind == "ptr":
                b += struct.pack("<HHII", tag, typ, 1, sentinels[payload])
            elif kind == "inline":
                count, raw = payload
                b += struct.pack("<HHI", tag, typ, count) + raw + b"\x00" * (4 - len(raw))
            else:
                count, i = payload
                b += struct.pack("<HHII", tag, typ, count, pool_off + rel[i])
        b += struct.pack("<I", 0)
        return bytes(b)

    buf = bytearray(b"II" + struct.pack("<HI", 42, ifd0_off))
    buf += emit(e0, it0, ifd0_pool_off, rel0) + pool0
    buf += emit(eE, itE, exif_pool_off, relE) + poolE
    buf += emit(eR, itR, raw_pool_off, relR) + poolR
    if len(buf) % 2:
        buf += b"\x00"
    assert len(buf) == prev_strip_off, (len(buf), prev_strip_off)
    buf += preview_strip
    if len(buf) % 2:
        buf += b"\x00"
    assert len(buf) == raw_strip_off, (len(buf), raw_strip_off)
    buf += raw_strip
    return bytes(buf)


def write_dng(out_path: str, cfa: np.ndarray, jm, opts: Options, serial, fnumber,
              lens_info=None, preview_jpeg: "bytes | None" = None,
              preview_dims=None) -> None:
    strip = np.clip(np.round(cfa), 0, opts.white).astype("<u2").tobytes()
    exif = build_exif(opts, jm, fnumber, lens_info) if jm is not None else None

    if getattr(opts, "legacy_preview", False):
        long_edge = getattr(opts, "preview_size", PREVIEW_LONG)
        prev = None
        if preview_jpeg:
            try:
                prev = _rgb_preview_from_jpeg(preview_jpeg, long_edge)
            except Exception as e:
                log.warning("Could not build RGB preview from JPEG (%s); using raw fallback.", e)
        if prev is None:
            prev = _rgb_preview_from_raw(cfa, opts, long_edge)
        prev_rgb, prev_w, prev_h = prev
        if preview_jpeg is not None:
            _a = np.asarray(prev_rgb).astype(np.int16)
            _luma = _a.mean(2)
            _colour = (_a.max(2) - _a.min(2)) > 20
            _frac = float(_colour.mean())
            if (_a.mean() < 35.0 and _frac > 0.05 and _colour.any()
                    and float(np.median(_luma[_colour])) < 40.0):
                prev_rgb, prev_w, prev_h = _rgb_preview_from_raw(cfa, opts, long_edge)
                log.info("Dark, colour-noisy frame: preview rebuilt from corrected raw "
                         "(grayscale; camera JPEG carried %.0f%% shadow colour-noise).",
                         100.0 * _frac)
        as_jpeg = not getattr(opts, "preview_uncompressed", False)
        if as_jpeg:
            prev_strip = _encode_preview_jpeg(prev_rgb)
        else:
            prev_strip = np.ascontiguousarray(prev_rgb, np.uint8).tobytes()
        ifd0 = build_ifd0_preview(opts, jm, serial, prev_w, prev_h, len(prev_strip), jpeg=as_jpeg)
        raw_subifd = build_raw_subifd(opts)
        os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
        data = build_dng_bytes_legacy(ifd0, exif if exif is not None else [],
                                      strip, raw_subifd, prev_strip)
        with open(out_path, "wb") as f:
            f.write(data)
        log.info("Wrote %s  (standard layout: %dx%d %s preview in IFD0, %dx%d raw in "
                 "SubIFD, white=%d%s%s)", out_path, prev_w, prev_h,
                 "JPEG" if as_jpeg else "RGB", RAW_W, RAW_H, opts.white,
                 ", black=%d" % opts.black if opts.set_black else "",
                 ", M9 colour" if opts.color_m9 else "")
        return

    ifd0 = build_ifd0(opts, jm, serial, fnumber)
    preview_ifd = None
    if opts.preview and preview_jpeg:
        w, h = preview_dims
        preview_ifd = build_preview_ifd(w, h)
        preview_ifd = [(t, ty, (len(preview_jpeg) if v == "@PREVSTRIPLEN" else v))
                       for (t, ty, v) in preview_ifd]

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    data = build_dng_bytes(ifd0, exif if exif is not None else [],
                           strip, preview_ifd, preview_jpeg if preview_ifd else None)
    with open(out_path, "wb") as f:
        f.write(data)
    log.info("Wrote %s  (%dx%d, white=%d%s%s%s)", out_path, RAW_W, RAW_H, opts.white,
             ", black=%d" % opts.black if opts.set_black else "",
             ", M9 colour" if opts.color_m9 else "",
             ", +preview" if (opts.preview and preview_jpeg) else "")


def _green_mean(cfa, cfa_name):
    pat = CFA_PATTERNS.get(cfa_name, CFA_PATTERNS["RGGB"])
    pos = [(0, 0), (0, 1), (1, 0), (1, 1)]
    greens = [p for p, v in zip(pos, pat) if v == 1]
    vals = [cfa[r::2, c::2].mean() for (r, c) in greens]
    return float(np.mean(vals))


def verify_dng(path, opts):
    """Re-read a just-written DNG and self-check its structure - essentially the
    --probe geometry checks applied to our own output, plus tag-consistency
    assertions. Returns (ok: bool, checks: list[(name, ok, detail)])."""
    import struct as _st
    checks = []
    def chk(name, cond, detail=""):
        checks.append((name, bool(cond), detail)); return bool(cond)
    try:
        buf = open(path, "rb").read()
    except Exception as e:
        return False, [("readable", False, str(e))]
    fsize = len(buf)
    if not chk("TIFF header II/42", len(buf) >= 8 and buf[:2] == b"II"
               and _st.unpack_from("<H", buf, 2)[0] == 42, "bytes[:4]=%r" % buf[:4]):
        return False, checks
    u16 = lambda o: _st.unpack_from("<H", buf, o)[0]
    u32 = lambda o: _st.unpack_from("<I", buf, o)[0]
    SZ = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 9: 4, 10: 8}
    def read_ifd(off):
        if not (0 < off < fsize - 1):
            return None, 0
        n = u16(off); t = {}
        for k in range(n):
            eo = off + 2 + k * 12
            t[u16(eo)] = (u16(eo + 2), u32(eo + 4), eo + 8)
        return t, n
    def val(t, tag, m=4):
        if t is None or tag not in t:
            return None
        typ, cnt, vo = t[tag]; sz = SZ.get(typ, 1); tot = sz * cnt
        base = vo if tot <= 4 else u32(vo); out = []
        for k in range(min(cnt, m)):
            o = base + k * sz
            if typ == 3:   out.append(u16(o))
            elif typ == 4: out.append(u32(o))
            elif typ in (5, 10): out.append((u32(o), u32(o + 4)))
            else: out.append(buf[o])
        return out
    ifd0_off = u32(4)
    t0, n0 = read_ifd(ifd0_off)
    if not chk("IFD0 readable", t0 is not None, "off=%d" % ifd0_off):
        return False, checks
    tags0 = [u16(ifd0_off + 2 + k * 12) for k in range(n0)]
    chk("IFD0 tags ascending", tags0 == sorted(tags0))
    chk("DNGVersion present", 50706 in t0)
    raw_t, layout = None, "none"
    if val(t0, 262) == [32803]:
        raw_t, layout = t0, "raw-in-IFD0"
    elif 330 in t0:
        rt, _ = read_ifd(val(t0, 330, 1)[0])
        if val(rt, 262) == [32803]:
            raw_t, layout = rt, "raw-in-SubIFD"
    chk("raw CFA IFD found [%s]" % layout, raw_t is not None)
    if raw_t is not None:
        chk("raw width %d" % RAW_W, val(raw_t, 256) == [RAW_W], "%s" % val(raw_t, 256))
        chk("raw height %d" % RAW_H, val(raw_t, 257) == [RAW_H], "%s" % val(raw_t, 257))
        chk("BitsPerSample 16", val(raw_t, 258) == [16], "%s" % val(raw_t, 258))
        chk("uncompressed", val(raw_t, 259) == [1], "%s" % val(raw_t, 259))
        bc = (val(raw_t, 279) or [0])[0]; so = (val(raw_t, 273) or [0])[0]
        chk("raw strip length", bc == RAW_W * RAW_H * 2, "%d B" % bc)
        chk("raw strip within file", 0 < so and so + bc <= fsize,
            "off=%d end=%d size=%d" % (so, so + bc, fsize))
        chk("WhiteLevel %d" % opts.white, val(raw_t, 50717) == [opts.white], "%s" % val(raw_t, 50717))
        if opts.set_black:
            chk("BlackLevel %d" % opts.black, val(raw_t, 50714) == [opts.black], "%s" % val(raw_t, 50714))
        want = list(CFA_BYTES.get(opts.cfa, CFA_BYTES["RGGB"]))
        chk("CFAPattern %s" % opts.cfa, val(raw_t, 33422, 4) == want, "%s" % val(raw_t, 33422, 4))
    asn = val(t0, 50728, 3) or (val(raw_t, 50728, 3) if raw_t is not None else None)
    chk("AsShotNeutral (3 rationals)", asn is not None and len(asn) == 3, "%s" % asn)
    if opts.legacy_preview:
        chk("IFD0 is preview", val(t0, 262) in ([2], [6]) and val(t0, 254) == [1],
            "photo=%s type=%s" % (val(t0, 262), val(t0, 254)))
        pbc = (val(t0, 279) or [0])[0]; pso = (val(t0, 273) or [0])[0]
        chk("preview strip within file", 0 < pso and pso + pbc <= fsize,
            "off=%d end=%d" % (pso, pso + pbc))
    return all(c[1] for c in checks), checks


def process_one(raw_path, jpg_path, bia_path, opts, lensdb, sensdb, camera_serial=None):
    """Convert one RAW (or DNG) to a DNG. Returns the output path or None."""
    stem, ext = os.path.splitext(raw_path)
    out_dir = opts.out_dir or os.path.dirname(raw_path)
    out_path = os.path.join(out_dir, os.path.basename(stem) + ".DNG")
    if os.path.exists(out_path) and not opts.refresh and ext.lower() != ".dng":
        log.info("File %s already exists - skipping (use -r to overwrite).", out_path)
        return None

    if ext.lower() == ".dng":
        raw_full, _ = read_dng_cfa(raw_path)
        cfa = np.zeros((RAW_H, RAW_W), np.float64)
        cfa[:raw_full.shape[0], :raw_full.shape[1]] = raw_full
        jm = read_jpeg_meta(jpg_path)
    else:
        cfa = read_raw(raw_path, opts)
        jm = read_jpeg_meta(jpg_path)
        if bia_path and os.path.isfile(bia_path):
            cfa = np.clip(cfa - read_raw(bia_path, opts), 0, opts.white)
            opts = dataclasses.replace(opts, set_black=True, black=0)
            log.info("Subtracted bias frame %s (BlackLevel set to 0).",
                     os.path.basename(bia_path))

    serial = camera_serial or (jm.get("serial") if jm else None)
    iso = (jm.get("iso") if jm else None)
    applied_gain = 1

    if opts.sensor or opts.sensor_test:
        sdb = sensdb.get(serial) if serial else None
        if sdb is None and sensdb:
            if not serial and len(sensdb) == 1:
                sdb = next(iter(sensdb.values()))
            else:
                log.warning("No sensor-database entry for serial '%s' (sensdb has: %s); "
                            "skipping sensor fix (run -sd on this body's darks).",
                            serial or "unknown", ", ".join(sensdb.keys()))
                sdb = None
        if sdb:
            lines = list(sdb.get("lines") or [])
            if opts.auto_repair and not opts.sensor_test:
                auto = detect_defect_columns(cfa.mean(axis=0))
                if len(auto) > AUTO_REPAIR_WARN:
                    log.warning(
                        "--auto-repair flagged %d columns on this image. That many almost "
                        "always means it is reading scene structure on a LIT capture, not "
                        "sensor defects; repairing them replaces real image columns with a "
                        "neighbour average. --auto-repair is intended for dark frames - for "
                        "normal photos drop it and rely on the sensdb Line entries from -sd "
                        "(plain -s repairs those). Proceeding to repair %d columns.",
                        len(auto), len(auto))
                for c in auto:
                    lines.append((int(c), 0, int(c), RAW_H - 1))
            if opts.sensor and not opts.sensor_test and sdb.get("levels") is not None:
                levels = sdb["levels"]
                applied_gain = _iso_gain(iso)
                if applied_gain != 1:
                    levels = levels * applied_gain
                    log.info("ISO %s: scaling sensor darkfield by analog gain "
                             "x%d (base ISO 160).", iso, applied_gain)
                cfa = apply_levels(cfa, levels, opts.white)
            cfa = repair_lines(cfa, lines, opts.white, test_mode=opts.sensor_test)
        else:
            log.warning("No sensor database entry for serial '%s'.", serial)

    lens_info = None
    code = (jm.get("lens_code_raw") if jm else 0) or opts.lens_code
    if opts.lens and opts.lens_code and opts.lens_code in lensdb:
        lens_info = lensdb[opts.lens_code]
    elif opts.lens and isinstance(code, str) and code in lensdb:
        lens_info = lensdb[code]

    ckey = str(code) if code else None
    sdb_e = sensdb.get(serial) if serial else None
    if sdb_e is None and sensdb and not serial and len(sensdb) == 1:
        sdb_e = next(iter(sensdb.values()))
    meter_offset = None
    if not (opts.legacy_fnumber or opts.mimic_fnumber):
        if sdb_e and ckey:
            meter_offset = (sdb_e.get("meter_offsets") or {}).get(ckey)
        if meter_offset is None and opts.meter_offset_map:
            meter_offset = opts.meter_offset_map.get((str(serial) if serial else None, ckey))

    exptime = (jm.get("exposure_time") if jm else None)
    exptime = (exptime[0] / exptime[1]) if exptime else None
    xcheck_used, xcheck_div, image_calib = False, None, None
    if opts.aperture:
        fnumber = float(opts.aperture)
    elif jm and jm.get("fnumber"):
        fnumber = jm["fnumber"][0] / jm["fnumber"][1]
    else:
        _gm = _green_mean(cfa, opts.cfa)
        _eb = (None if opts.legacy_fnumber else (jm.get("ext_brightness") if jm else None))
        _ml = (None if opts.legacy_fnumber else (jm.get("measured_lv") if jm else None))
        fnumber = estimate_fnumber(_gm, opts.black, exptime, iso,
                                   mimic_original=opts.mimic_fnumber,
                                   ext_bv=_eb, meas_lv=_ml, meter_offset=meter_offset)
        if (not (opts.legacy_fnumber or opts.mimic_fnumber)
                and meter_offset is not None and _ml is not None and _eb is not None):
            if sdb_e and ckey:
                image_calib = (sdb_e.get("image_calibs") or {}).get(ckey)
            if image_calib is not None:
                image_f = estimate_fnumber(_gm, opts.black, exptime, iso, image_calib=image_calib)
                xcheck_div = abs(2.0 * math.log2(fnumber) - 2.0 * math.log2(image_f))
                if xcheck_div > XCHECK_THRESHOLD_STOPS and image_f > fnumber:
                    fnumber, xcheck_used = image_f, True
    if lens_info and lens_info.get("Apertures"):
        fnumber = snap_aperture(fnumber, lens_info["Apertures"])
    if opts.verbose:
        _ml = (jm.get("measured_lv") if jm else None)
        _eb = (jm.get("ext_brightness") if jm else None)
        if opts.aperture:
            _how = "forced (-A)"
        elif jm and jm.get("fnumber"):
            _how = "camera value (coded lens)"
        elif opts.mimic_fnumber:
            _how = "mimic-original estimate"
        elif meter_offset is not None and _ml is not None and _eb is not None:
            _how = "meter offset %.4f  (MeasLV-ExtBV = %+.3f)" % (meter_offset, _ml - _eb)
            if xcheck_used:
                _how = ("cross-check OVERRIDE -> calibrated image f/%g: meter said f/%g but the two "
                        "disagree by %.1f stops (> %.1f), so the ExternalSensorBrightnessValue is "
                        "corrupt on this frame; using the meter-free image estimate"
                        % (fnumber, estimate_fnumber(_green_mean(cfa, opts.cfa), opts.black, exptime, iso,
                                                     ext_bv=_eb, meas_lv=_ml, meter_offset=meter_offset),
                           xcheck_div, XCHECK_THRESHOLD_STOPS))
            elif image_calib is not None and xcheck_div is not None:
                if xcheck_div > XCHECK_THRESHOLD_STOPS:
                    _how += ("  [cross-check: image f/%g reads %.1f st WIDER than meter (> %.1f) -- not a "
                             "corrupt-meter signature but the image leg luminance-biased on a brighter-"
                             "than-calibration frame; kept the meter]"
                             % (image_f, xcheck_div, XCHECK_THRESHOLD_STOPS))
                else:
                    _how += "  [cross-check: image agrees within %.1f st]" % xcheck_div
        elif (not opts.legacy_fnumber) and _eb is not None:
            _how = ("ExtBV-anchored estimate (deterministic default; no stored MeterOffset "
                    "for body %s / lens %s -- --calibrate-fnumber for exact apertures, or "
                    "--selfcal for batch self-calibration)"
                    % (serial, code))
        else:
            _how = "image-brightness constant (light meters ignored)"
        log.info("   %s: FNumber f/%g  via %s", os.path.basename(raw_path), fnumber, _how)

    preview_jpeg, preview_dims = None, None
    if (opts.preview or opts.legacy_preview) and jpg_path and os.path.isfile(jpg_path):
        try:
            preview_jpeg = open(jpg_path, "rb").read()
            preview_dims = _jpeg_dims(preview_jpeg) or (RAW_W, RAW_H)
        except Exception as e:
            log.warning("Could not embed preview: %s", e)
            preview_jpeg = None

    if opts.set_black and applied_gain != 1:
        opts = dataclasses.replace(opts, black=int(opts.black) * applied_gain)

    write_dng(out_path, cfa, jm, opts, serial, fnumber, lens_info,
              preview_jpeg, preview_dims)
    if opts.verify:
        ok, results = verify_dng(out_path, opts)
        if ok:
            log.info("VERIFY PASS: %s (%d checks)", os.path.basename(out_path), len(results))
        else:
            fails = "; ".join("%s {%s}" % (n, d) for n, o, d in results if not o)
            log.error("VERIFY FAIL: %s - %s", os.path.basename(out_path), fails)
    return out_path


def _jpeg_dims(b: bytes):
    """Return (width, height) from a JPEG byte string, or None."""
    i, n = 2, len(b)
    while i + 9 < n:
        if b[i] != 0xFF:
            i += 1
            continue
        marker = b[i + 1]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h = struct.unpack(">H", b[i + 5:i + 7])[0]
            w = struct.unpack(">H", b[i + 7:i + 9])[0]
            return (w, h)
        seg = struct.unpack(">H", b[i + 2:i + 4])[0]
        i += 2 + seg
    return None


def _column_means(raw_path, opts):
    return read_raw(raw_path, opts).mean(axis=0)


def create_darkfield(raw_path, jpg_path, opts, sensdb_path, sensdb):
    """-sd on a single frame (used by the GUI). Computes LevelCorrection and writes it."""
    jm = read_jpeg_meta(jpg_path)
    serial = (jm.get("serial") if jm else None)
    if not serial:
        try:
            serial = input("Camera serial number for this darkfield: ").strip() or "UNKNOWN"
        except Exception:
            serial = "UNKNOWN"
    log.info("Computing sensor level correction for camera %s from %s ...",
             serial, os.path.basename(raw_path))
    colmean = _column_means(raw_path, opts)
    levels, defects = compute_levels(colmean, zero_defects=opts.auto_lines)
    lines = None
    if opts.auto_lines and defects.size:
        lines = [(int(c), 0, int(c), RAW_H - 1) for c in defects]
    write_sensdb(sensdb_path, serial, levels, lines)
    log.info("Saved %d LevelCorrection values to %s under [%s] (%d defect column(s)%s).",
             levels.size, sensdb_path, serial, int(defects.size),
             " -> Line entries" if lines else "")


def create_darkfield_smart(jobs, opts, sensdb_path, sensdb, serial=None):
    """-sd over a folder: select clean, fast, low-ISO darks, average their column
    means, then build one LevelCorrection profile."""
    jobs = [j for j in jobs if os.path.splitext(j[0])[1].lower() == ".raw"]
    if not jobs:
        log.warning("Darkfield (-sd) needs RAW frames; DNG files cannot be used.")
        return
    cand = []
    for (raw, jpg, _bia) in jobs:
        jm = read_jpeg_meta(jpg) if jpg else None
        iso = (jm.get("iso") if jm else None) or 0
        et = (jm.get("exposure_time") if jm else None)
        et = (et[0] / et[1]) if et else None
        cand.append((raw, jpg, jm, iso, et))

    isos = [c[3] for c in cand if c[3]]
    min_iso = min(isos) if isos else 0
    selected, skipped = [], []
    for (raw, jpg, jm, iso, et) in cand:
        ok = True
        reason = ""
        if iso and iso > max(min_iso * 1.5, 320):
            ok, reason = False, f"ISO {iso} too high"
        elif et is not None and et > 1.0:
            ok, reason = False, f"{et:g}s too long (dark current)"
        elif min_iso and iso and iso > min_iso:
            ok, reason = False, f"ISO {iso} > lowest {min_iso}"
        (selected if ok else skipped).append((raw, reason))
    if not selected:
        selected = [(c[0], "") for c in cand]

    if serial is None:
        for (raw, jpg, jm, iso, et) in cand:
            if jm and jm.get("serial"):
                serial = jm["serial"]
                break
    if not serial:
        try:
            serial = input("Camera serial number for this darkfield: ").strip() or "UNKNOWN"
        except Exception:
            serial = "UNKNOWN"

    log.info("Darkfield from %d frame(s) [%s]:", len(selected), serial)
    for raw, _ in selected:
        log.info("   use  %s", os.path.basename(raw))
    for raw, reason in skipped:
        log.info("   skip %s  (%s)", os.path.basename(raw), reason)

    acc = None
    for raw, _ in selected:
        cm = _column_means(raw, opts)
        acc = cm if acc is None else acc + cm
    colmean = acc / len(selected)
    levels, defects = compute_levels(colmean, zero_defects=opts.auto_lines)
    lines = None
    if opts.auto_lines and defects.size:
        lines = [(int(c), 0, int(c), RAW_H - 1) for c in defects]
    write_sensdb(sensdb_path, serial, levels, lines)
    log.info("Saved %d LevelCorrection values to %s (averaged %d frame(s), %d defect "
             "column(s)%s).", levels.size, sensdb_path, len(selected), int(defects.size),
             " -> Line entries" if lines else "")


def _sidecar(stem, ext):
    for e in (ext, ext.upper(), ext.lower()):
        if os.path.isfile(stem + e):
            return stem + e
    return None


def discover_jobs(inputs, recursive):
    raws = []
    for item in inputs:
        if os.path.isdir(item):
            pat = "**/*" if recursive else "*"
            for f in glob.glob(os.path.join(item, pat), recursive=recursive):
                if os.path.splitext(f)[1].lower() in (".raw", ".dng"):
                    raws.append(f)
        elif os.path.isfile(item):
            if os.path.splitext(item)[1].lower() in (".raw", ".dng"):
                raws.append(item)
        else:
            log.warning("Not found: %s", item)
    jobs = []
    for r in sorted(set(raws)):
        stem = os.path.splitext(r)[0]
        jpg = _sidecar(stem, ".jpg") or _sidecar(stem, ".jpeg")
        bia = _sidecar(stem, ".bia")
        jobs.append((r, jpg, bia))
    return jobs


USAGE = f"""{PROG} {VERSION_DISPLAY} - refined Leica M8 RAW -> DNG converter
(clean reimplementation of Arvid's m8raw2dng v1.20)

Usage: {PROG} [options] [files-or-folders ...]

Original-compatible switches:
  -i <path>      input file or folder (default: current folder)
  -o <path>      output folder (default: alongside the input)
  -v             verbose
  -r             refresh: overwrite existing DNGs
  -b [val]       write a BlackLevel tag (default {BLACK_DEFAULT}); raise to cure
                 magenta shadows, lower to cure green
  -p             embed the full-resolution camera JPEG as the DNG preview
  -c             use the M9 ("M8RAW") colour matrices instead of the M8 ones
  -l [6bitcode]  apply lens EXIF from lensdb.ini; optional forced 6-bit code
  -s             apply sensor fixes (darkfield + line repair) from sensdb.ini
  -sd            create/update the sensor darkfield (one frame, or a whole folder)
  -st            sensor test: paint configured/auto-detected bad columns white

Refinements:
  -A --aperture F     force the EXIF FNumber to f/F (otherwise it is estimated)
     --calibrate-fnumber AP[,AP...]
                      calibration mode: derive this body+lens's aperture-meter offset
                      from the given known-aperture frames and store it in sensdb.ini
                      (MeterOffset.<lenscode> under [serial]). Apertures must match the
                      input frames in filename order -- shoot anything (a cup on a
                      table, auto exposure fine) at, say, f/2.8,4,5.6,8,11, then:
                      --calibrate-fnumber 2.8,4,5.6,8,11 -l 000110 <those 5 frames>.
                      5-10 frames, once per (body,lens); afterwards every conversion
                      reads the stored offset and recovers the true aperture from the
                      two light meters (MeasuredLV - ExternalSensorBrightnessValue),
                      no -A needed. With no stored offset the deterministic per-frame
                      ExtBV-anchored estimate is used (see --selfcal for the optional
                      batch self-calibration). When a stored ImageCalib is also present,
                      a directional meter<->image cross-check runs automatically to catch
                      a corrupt external light-meter reading.
     --mimic-fnumber  estimate FNumber the original tool's (compressed) way instead
                      of the accurate default; less accurate, base-ISO only
     --legacy-fnumber ignore both light meters when estimating FNumber (use the
                      assumed-luminance image-brightness constant only); the meters are
                      used automatically when present and are markedly more accurate --
                      this is for reproducing pre-2.8 output or for files without the
                      Leica MakerNote
     --selfcal        opt into batch self-calibration of the aperture-meter offset from
                      the camera's own ApproxF over a >={SELFCAL_MIN_FRAMES}-frame batch. Off by default
                      because it is BATCH-DEPENDENT (the same frame can land on a
                      different aperture in a different batch); the default is the
                      deterministic per-frame ExtBV-anchored estimate. A stored
                      MeterOffset (--calibrate-fnumber) is always preferred over both.
     --auto-lines     -sd: ALSO detect bright/defect columns, zero them in
                      LevelCorrection, and record them as Line entries (off by
                      default; without it -sd writes the darkfield only). -s then
                      neighbour-interpolates every Line entry stored in sensdb.ini.
     --auto-repair    -s : ALSO re-detect defect columns on each image (off by default;
                      sensdb Line entries are always repaired regardless)
  -R --recursive      walk sub-folders
  -j --jobs N         convert N files in parallel
     --dry-run        list what would be done, write nothing
     --probe [PATH]   report a .RAW/.DNG file's geometry - or every such file in
                      a folder (PATH, or -i / positional inputs, or cwd) - then exit
     --db-dir DIR     folder holding lensdb.ini / sensdb.ini (default: next to this script)
     --cfa PHASE      Bayer phase RGGB|BGGR|GRBG|GBRG (default RGGB)
     --no-crop        mark the DNG's DefaultCrop as the full {RAW_W}x{RAW_H} frame so
                      Photoshop/ACR open the uncropped image incl. the 2px edge
                      (default: crop to {DNG_W}x{DNG_H}, matching the reference tool)
     --legacy-preview emit the standard in-camera M8 DNG layout (use with -p): a
                      sharp 3:2 JPEG preview in IFD0 with the raw CFA in a SubIFD.
                      Fixes macOS Finder / QuickLook thumbnails and keeps files
                      small (the JPEG preview is only a few KB). Without it, -p
                      keeps Arvid's raw-in-IFD0 layout (byte-identical to the
                      original tool). Raw pixels are identical either way.
     --preview-size N long edge in px of the --legacy-preview image (default 1024,
                      3:2 aspect; e.g. 768 for smaller, 1536 for sharper)
     --preview-uncompressed
                      embed the preview as uncompressed RGB instead of JPEG (much
                      larger files; only needed if a reader dislikes the JPEG)
     --verify         re-read each DNG after writing and self-check its structure
                      (geometry, strip bounds, key tags); reports PASS / FAIL

Advanced (rarely needed - the defaults are correct for the M8):
     --white N        white / ADC ceiling (default {WHITE_DEFAULT})
     --raw-offset N   header samples to skip in the RAW (default {RAW_HEADER_SAMPLES})
     --raw-endian E   little|big (default little)
     --log [FILE]     also write a log file (conversion, --verify and --probe
                      output); FILE optional - auto-named if omitted
     --version
"""


def parse_args(argv):
    o = Options()
    i, positionals, log_file = 0, [], None
    while i < len(argv):
        a = argv[i]

        def nextval():
            nonlocal i
            i += 1
            return argv[i] if i < len(argv) else None

        if a in ("-h", "--help"):
            print(USAGE); sys.exit(0)
        elif a == "--version":
            print(f"{PROG} {VERSION_DISPLAY}"); sys.exit(0)
        elif a == "-i":
            v = nextval()
            if v:
                positionals.append(v)
        elif a == "-o":
            o.out_dir = nextval()
        elif a == "-v":
            o.verbose = True
        elif a == "-r":
            o.refresh = True
        elif a == "-p":
            o.preview = True
        elif a == "-c":
            o.color_m9 = True
        elif a == "-st":
            o.sensor_test = True
        elif a == "-sd":
            o.sensor_darkfield_create = True
        elif a == "-s":
            o.sensor = True
        elif a in ("-A", "--aperture"):
            try:
                o.aperture = float(nextval())
            except (TypeError, ValueError):
                print("Bad -A/--aperture value"); sys.exit(2)
        elif a == "--auto-lines":
            o.auto_lines = True
        elif a == "--mimic-fnumber":
            o.mimic_fnumber = True
        elif a == "--legacy-fnumber":
            o.legacy_fnumber = True
        elif a == "--selfcal":
            o.selfcal = True
        elif a == "--calibrate-fnumber":
            o.calibrate_fnumber = nextval()
        elif a == "--auto-repair":
            o.auto_repair = True
        elif a.startswith("-b"):
            o.set_black = True
            if len(a) > 2 and a[2:].lstrip("-").isdigit():
                o.black = int(a[2:])
            elif i + 1 < len(argv) and argv[i + 1].lstrip("-").isdigit():
                o.black = int(nextval())
        elif a.startswith("-l"):
            o.lens = True
            if len(a) > 2:
                o.lens_code = a[2:]
            elif i + 1 < len(argv) and len(argv[i + 1]) == 6 and all(c in "01" for c in argv[i + 1]):
                o.lens_code = nextval()
        elif a in ("-R", "--recursive"):
            o.recursive = True
        elif a in ("-j", "--jobs"):
            o.jobs = max(1, int(nextval() or 1))
        elif a == "--dry-run":
            o.dry_run = True
        elif a == "--probe":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                o.probe = nextval()
            else:
                o.probe = True
        elif a == "--db-dir":
            o.db_dir = nextval()
        elif a == "--cfa":
            o.cfa = (nextval() or "RGGB").upper()
        elif a == "--white":
            o.white = int(nextval())
        elif a == "--no-crop":
            o.no_crop = True
        elif a == "--legacy-preview":
            o.legacy_preview = True
        elif a == "--preview-size":
            try:
                o.preview_size = max(128, min(4096, int(nextval())))
            except (TypeError, ValueError):
                print("Bad --preview-size value"); sys.exit(2)
        elif a == "--preview-uncompressed":
            o.preview_uncompressed = True
        elif a == "--verify":
            o.verify = True
        elif a == "--raw-offset":
            o.raw_offset = int(nextval())
        elif a == "--raw-endian":
            o.raw_endian = nextval()
        elif a == "--log":
            if i + 1 < len(argv) and not argv[i + 1].startswith("-"):
                log_file = nextval()
            else:
                log_file = "<auto>"
        elif a.startswith("-"):
            print(f"Unknown option: {a}\n"); print(USAGE); sys.exit(2)
        else:
            positionals.append(a)
        i += 1
    o.inputs = positionals
    if o.cfa not in CFA_PATTERNS:
        print(f"Bad --cfa {o.cfa}; choose from {list(CFA_PATTERNS)}"); sys.exit(2)
    return o, log_file


def setup_logging(verbose, log_file):
    handlers = [logging.StreamHandler(sys.stdout)]
    if log_file:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    logging.basicConfig(level=logging.DEBUG if verbose else logging.INFO,
                        format="%(message)s", handlers=handlers)
    for noisy in ("PIL", "tifffile", "PIL.TiffImagePlugin"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def probe(path, opts):
    size = os.path.getsize(path)
    log.info("File:   %s", path)
    log.info("Size:   %s bytes", f"{size:,}")
    ext = os.path.splitext(path)[1].lower()
    if ext == ".raw":
        log.info("Expected M8 RAW frame: %s bytes (%d header + %d rows x %d + "
                 "%d trailing row, 16-bit; %dx%d active)", f"{RAW_BYTES:,}",
                 RAW_HEADER_SAMPLES, RAW_H, RAW_ROW_STRIDE, RAW_TRAILING_ROWS, RAW_W, RAW_H)
        if size == RAW_BYTES:
            log.info("=> exact match.")
        else:
            log.info("=> differs by %+d byte(s).", size - RAW_BYTES)
    elif ext == ".dng" and HAVE_TIFFFILE:
        with tifffile.TiffFile(path) as tf:
            for pi, p in enumerate(tf.pages):
                log.info("  page %d: shape=%s dtype=%s photometric=%s",
                         pi, p.shape, p.dtype, p.photometric)


def _worker(args):
    (raw, jpg, bia), opts, lensdb, sensdb = args
    try:
        return process_one(raw, jpg, bia, opts, lensdb, sensdb)
    except Exception as e:
        log.error("FAILED %s: %s", raw, e)
        return None


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    opts, log_file = parse_args(argv)
    if log_file == "<auto>":
        import datetime
        base = opts.out_dir or os.getcwd()
        try:
            os.makedirs(base, exist_ok=True)
        except Exception:
            base = os.getcwd()
        log_file = os.path.join(
            base, "m8raw2dng2_%s.log" % datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
    setup_logging(opts.verbose, log_file)
    if log_file:
        log.info("Log file: %s", log_file)

    if opts.probe:
        probe_inputs = (opts.inputs if opts.probe is True else [opts.probe]) or [os.getcwd()]
        files = []
        for pin in probe_inputs:
            if os.path.isdir(pin):
                files.extend(j[0] for j in discover_jobs([pin], opts.recursive))
            elif os.path.isfile(pin):
                files.append(pin)
            else:
                log.info("Not found: %s", pin)
        files = sorted(dict.fromkeys(files))
        if not files:
            log.info("No .RAW/.DNG files found to probe.")
            return 0
        if len(files) > 1:
            log.info("Probing %d file(s):\n", len(files))
        for k, fp in enumerate(files):
            if k:
                log.info("")
            probe(fp, opts)
        return 0

    lensdb = parse_lensdb(_db_path(opts, "lensdb.ini"))
    sensdb_path = _db_path(opts, "sensdb.ini")
    sensdb = parse_sensdb(sensdb_path)

    if not opts.inputs:
        opts.inputs = [os.getcwd()]
    jobs = discover_jobs(opts.inputs, opts.recursive)
    if not jobs:
        log.info("No RAW/DNG files found. (Pass files or folders, or use -i.)")
        print(USAGE)
        return 0

    if opts.calibrate_fnumber is not None:
        return calibrate_fnumber_offset(jobs, opts, sensdb_path)

    if opts.sensor_darkfield_create:
        if len(jobs) == 1:
            raw, jpg, _ = jobs[0]
            create_darkfield(raw, jpg, opts, sensdb_path, sensdb)
        else:
            create_darkfield_smart(jobs, opts, sensdb_path, sensdb)
        return 0

    log.info("%d image(s) to process.", len(jobs))
    if opts.dry_run:
        for raw, jpg, bia in jobs:
            log.info("would convert %s (jpg=%s bia=%s)", raw, bool(jpg), bool(bia))
        return 0

    opts.meter_offset_map = resolve_meter_offsets(jobs, opts, sensdb) if opts.selfcal else {}

    if opts.jobs > 1 and len(jobs) > 1:
        import multiprocessing as mp
        payload = [((r, j, b), opts, lensdb, sensdb) for (r, j, b) in jobs]
        with mp.Pool(opts.jobs) as pool:
            results = pool.map(_worker, payload)
    else:
        results = [_worker(((r, j, b), opts, lensdb, sensdb)) for (r, j, b) in jobs]

    ok = sum(1 for x in results if x)
    log.info("Done: %d/%d converted.", ok, len(jobs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
