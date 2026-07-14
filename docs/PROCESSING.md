# How m8raw2dng2 processes an M8 diagnostic RAW

This is the end-to-end account of what the tool does to a Leica M8 service-mode
(diagnostic) RAW dump and why each step exists. It is written to preserve the
craft, independent of the code: the reasoning here should let someone rebuild the
pipeline from scratch.

The baseline is a faithful decode. A bare `-v` conversion is byte-identical to
Arvid Kuehl's original `m8raw2dng` except for the estimated `FNumber`; every
correction below that is opt-in and layers on top. See the wiki
([The Math](https://github.com/botrx555/m8raw2dng2/wiki/The-Math), [Fidelity](https://github.com/botrx555/m8raw2dng2/wiki/Fidelity), [The Original Tool](https://github.com/botrx555/m8raw2dng2/wiki/The-Original-Tool)) for the measured basis.

## Why uncompressed 14-bit matters

The M8's diagnostic mode writes the sensor's full 14-bit uncompressed readout.
The camera's normal DNG path instead applies a companding curve that quantises the
highlights and bakes in a coarser tone before storage. Starting from the plain
14-bit dump keeps every code value the sensor actually resolved, so the developer
sees the true shadow separation and highlight headroom rather than the camera's
lossy approximation of them. The whole point of this tool is to get that readout
into a standard DNG without throwing any of it away: maximum extraction, then let
the raw developer decide.

## The input trio

| File | Role |
|---|---|
| `.RAW` | the 14-bit sensor dump; pixels only, no metadata |
| `.JPG` | same stem; the sole metadata source (EXIF + Leica MakerNote) |
| `.BIA` | optional, same stem; a full-frame dark reference (long exposures) |

The RAW carries no EXIF. Everything the DNG needs about the shot (body serial,
ISO, exposure, focal length, the two light meters) comes from the paired JPG. The
`.BIA`, when the camera wrote one, is a second full frame in the identical
container, not a per-column vector.

## The pipeline, stage by stage

### Stage 1 - Ingest the RAW

The dump is `21,049,052` bytes of little-endian 16-bit samples: `54` leading
header samples, then `2646` rows of `3976` samples, then one trailing stride-row
that is ignored. Only the first `3968` samples of each row are image data; the
remaining 8 are discarded. Values are clamped to `16383` (14-bit ceiling).

```
RAW_W, RAW_H          = 3968, 2646
RAW_HEADER_SAMPLES    = 54
RAW_ROW_STRIDE        = 3976
clamp                 = 16383
```

The result is the raw CFA mosaic: one 14-bit value per photosite, no rendering.
That mosaic is what every later stage operates on, and what a plain conversion
writes out untouched.

Insight: the 8 discarded samples per row and the trailing row are stride padding,
not image. Reading them as pixels is the classic first-attempt failure (a sheared
or wrapped image); the `3976` stride versus `3968` active width is the key.

### Stage 2 - Black level (`-b`)

A raw developer derives white balance from the black point, and the M8's green
channel sits slightly above red and blue. Too low a black level tints shadows
green; too high tints them magenta. The reference value is `92`.

Crucially this is a **tag only**. `-b` writes a `BlackLevel` DNG tag; it never
edits a pixel (the CFA is byte-identical at any `-b` value). With no `-b` the tag
is omitted and the developer assumes 0 - matching the original's bare conversion,
which also writes no `BlackLevel`. Under `-b` the original writes `92 x ISO gain`
(tag-only); m8raw2dng2's plain `-b` is a flat 92 (it gain-scales `BlackLevel` only
under `-s`, see Stage 4). Either way `-b` is a sensible default that never alters
the sensor data.

Insight: separating "the number a developer should assume" (a tag) from "the
pixels" is what keeps the decode honest. The black point is metadata, not a pixel
edit.

### Stage 3 - Bias subtraction (`.BIA`)

Long exposures accumulate a per-exposure dark pedestal. When the camera wrote a
`.BIA` beside the RAW, and it is left in the input folder, the tool reads it as
another raw frame, subtracts it from the CFA, clamps to `[0, white]`, and forces
`BlackLevel` to 0 (the pedestal is now removed, so black really is 0).

This is a deliberate divergence: the original ignores an adjacent `.BIA`. Because
it modifies pixels, it is opt-in by presence - move the `.BIA` out of the folder
for a plain decode.

Insight: a full-frame dark subtract corrects fixed-pattern and pedestal terms that
a single black-level number cannot, but only for that specific exposure. It is a
per-frame correction, not a sensor-wide one.

### Stage 4 - Folder-averaged sensor darkfield (`-s`, `-sd`)

Per image column, the level correction is:

```
LevelCorrection[c]  =  max( 0,  column_mean[c]  -  T )
T                   =  0.8406 * mean( column_mean )
```

`T` is calibrated to the ISO-160 reference. `-sd` measures these values (one per
column) from dark frames and stores them in `sensdb.ini`; `-s` subtracts them.
`-sd` can average a whole folder of dark frames, auto-selecting the clean, fast,
low-ISO ones for a less noisy darkfield and skipping slow or high-ISO frames.

At higher ISO the base darkfield is scaled by the whole-stop analog gain:

```
gain  =  2 ^ round( log2( ISO / 160 ) )    ->    1, 2, 4, 8, 16
```

so a base-ISO darkfield applied to an ISO-2500 frame is multiplied by 16. When
`-b` is combined with `-s`, the written `BlackLevel` is scaled by the same gain
(under plain `-b` it stays flat). The original also writes `base_black x gain`
under `-b`, so the two tools agree under `-s -b` and diverge under plain `-b` at
high ISO.

Insight: column-wise fixed-pattern noise is the dominant structured artifact on
this sensor. The original uses the same additive per-column shape but with a per-frame target
that drifts (single dark frames land near `0.83`, and the exact routine is
deterministic in the binary yet undecoded); the flat `0.8406` here lands blacks
within roughly one code value of the original at base ISO (that gap is
gain-amplified at high ISO) while being simpler to reason about.

### Stage 5 - Defect-column detection and repair

`detect_defect_columns` flags columns that deviate strongly from the local trend.
Under `-sd` with `--auto-lines` those columns are zeroed in `LevelCorrection` and
recorded as `Line` entries; `-s` then repairs each by neighbour interpolation. The
plain formula still applies to about `99.95%` of columns, matching the reference.

The original zeroes or suppresses defect columns and leaves them; this tool
repairs them - a deliberate, more-accurate divergence. Repair uses a `+2px`
RAW-to-DNG coordinate shift so the repaired column lands correctly after the 2px
border crop.

Insight: zeroing a stuck column hides it but leaves a dark streak; interpolating
from its neighbours reconstructs plausible detail. The coordinate shift matters
because detection runs in RAW space and the fix must land in cropped DNG space.

### Stage 6 - Aperture recovery for un-coded lenses (`FNumber`)

The M8 records no aperture for an un-coded lens, and the original synthesises one
from image brightness (it tracks the camera's own rough estimate, not the true
aperture, and compresses hard toward the narrow end - a real f/16 written as about
f/7.75). This tool instead recovers aperture from the two light meters:

```
Av_APEX  =  -( MeasuredLV - ExternalSensorBrightnessValue )  +  offset
```

with the slope locked at `-1`, then snapped to the lens's half-stop grid. Scene
luminance cancels in the difference of the two meters, which is what makes the
estimate scene-independent. Order of preference:

| Path | Trigger | Accuracy |
|---|---|---|
| Stored `MeterOffset` | `--calibrate-fnumber` once per (body, lens) | within a few hundredths of a stop |
| Deterministic default | no stored offset | low average error, batch-independent |
| `-A` / `--aperture` | explicit value | exact (100% byte-identical DNG) |

`--calibrate-fnumber AP[,AP...]` derives the per-(body, lens) offset from 5-10
known-aperture frames and also stores a meter-free image constant `ImageCalib` for
a directional cross-check: when the meter and image estimates diverge past about
2.5 stops the meter cell is treated as glitched and the calibrated image estimate
is used instead. A black-frame guard falls back to the pure-image formula when a
frame carries no image signal, so a capped or blocked long exposure still reads as
correctly stopped down.

Insight: aperture from the meter *difference* is the right idea because it cancels
the scene, but its floor is how well the two meter cells are matched. The residual
leans on scenes least like the calibration light (dim interiors read slightly
wide), and only crosses a grid line where f/2.8 sits at the rounding boundary. A
stored offset removes the lean.

### Stage 7 - Full-resolution embedded preview

By default the raw sits in IFD0 (byte-identical to the original), which some
viewers cannot thumbnail. `--legacy-preview` writes a sharp 3:2 baseline JPEG
preview (YCbCr 4:2:0, the tag set Adobe Camera Raw uses) into IFD0 and moves the
raw CFA to a SubIFD - the standard in-camera M8 layout - fixing macOS Finder and
QuickLook thumbnails while keeping files near the raw-strip floor (about 21 MB).
`--preview-size` sets the long edge; `--preview-uncompressed` embeds RGB instead.
The preview is auto-cleaned of hot pixels and shadow chroma noise; the raw CFA is
never touched, so pixels are identical across all preview options.

Insight: the preview is a convenience layer, strictly separate from the sensor
data. No preview option can change a raw pixel - that invariant is what lets the
feature exist without compromising fidelity.

### Stage 8 - DNG tag construction

The output is a hand-built little-endian TIFF (no imaging library needed to write
it): IFD0 with 47 tags, an EXIF sub-IFD with 21 tags, the MakerNote copied
verbatim, the M8 colour matrices under calibration illuminants 17 (Standard Light
A) and 21 (D65), and a single uncompressed strip of 16-bit linear CFA samples.

| Tag | Value |
|---|---|
| `WhiteLevel` | `16383` |
| `BlackLevel` | only with `-b` (tag only) |
| CFA phase | from `--cfa` (default RGGB) |
| `DefaultCropOrigin` / `Size` | `(2,2)` / `3964 x 2642` (full `3968 x 2646` with `--no-crop`) |
| `AsShotNeutral` | computed per shot from that frame's own data |
| `UniqueCameraModel` | `M8 Digital Camera` |
| `Software` | firmware string from the JPG (omitted if absent) |
| DPI / crop factor | 300 / 1.33 |

`AsShotNeutral` being per-shot (not a fixed daylight value stamped on every file)
matches the original, so two frames can legitimately carry different values. `-c`
swaps `ColorMatrix1/2` to the M9 "M8RAW" set and renames `Model`, leaving the
illuminants, `UniqueCameraModel` and `CameraCalibration` unchanged.

Insight: the 2px border crop is genuine sensor data (ordinary photosites,
continuous with the scene), trimmed by DNG convention as a demosaic-quality
margin - not masked optical black. The original's DNG carries the identical crop
tags, so both open the same way in ACR and Photoshop.

### Stage 9 - Verify

`verify_dng` re-reads each written DNG and self-checks its structure: TIFF header,
IFD0 readable, tags ascending, DNGVersion, the raw CFA IFD located, width and
height, 16-bit, uncompressed, strip length and in-bounds, `WhiteLevel`,
`CFAPattern`, `AsShotNeutral` - plus `BlackLevel` (with `-b`) and the preview IFD
(with `--legacy-preview`). It reports PASS or FAIL with a check count.

Insight: a hand-built TIFF has no library validating it on write, so the tool
validates its own output on read. Structural self-check is cheap insurance against
a malformed byte offset silently producing an unreadable file.

## The order, and what is optional

```
1 Ingest              always
2 Black level (-b)    opt-in tag
3 Bias (.BIA)         opt-in by presence, edits pixels
4 Darkfield (-s/-sd)  opt-in, edits pixels
5 Defect repair       opt-in (under -s), edits pixels
6 Aperture (FNumber)  always estimated; -A overrides exactly
7 Preview             opt-in layout; never edits pixels
8 DNG construction    always
9 Verify              opt-in (--verify)
```

Stages 1, 8 and the aperture estimate always run; a bare `-v` runs only those and
reproduces the original byte-for-byte except `FNumber`. Everything that edits
pixels (bias, darkfield, defect repair) is opt-in, so fidelity to the original is
the default and every correction is a conscious choice.
