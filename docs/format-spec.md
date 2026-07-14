# Leica M8 service-mode uncompressed RAW - format specification

A plain-decode specification for the Leica M8 diagnostic (service-mode)
uncompressed RAW dump, written for a decoder implementer. It covers the file
format and the minimal decode only. No processing refinements (darkfield, defect
repair, bias subtraction, aperture recovery) are part of this specification;
those are optional and out of scope for a baseline decoder.

The facts below were established by decoding the format directly and verifying
that a plain decode reproduces the reference converter's output byte for byte.

## Origin

The Leica M8, in its undocumented service/diagnostic mode, writes an uncompressed
14-bit sensor dump alongside a normal JPG. This differs from the camera's standard
DNG, which applies a companding curve. The dump is a flat sensor readout with a
small fixed header; there is no compression and no embedded metadata in the RAW
itself.

## File trio

A capture in this mode produces, sharing one filename stem:

| Extension | Required | Contents |
|---|---|---|
| `.RAW` | yes | 14-bit uncompressed sensor dump (pixels only) |
| `.JPG` | yes | EXIF + Leica MakerNote; the only metadata source |
| `.BIA` | no | full-frame dark reference, same container as `.RAW` |

The `.RAW` carries no EXIF. A decoder must read the paired `.JPG` for all
metadata. The `.BIA` is written by the camera on long exposures and is optional
and non-deterministic; a baseline decoder can ignore it.

## RAW byte layout

Little-endian unsigned 16-bit samples throughout. Total size is fixed.

| Field | Value |
|---|---|
| Total file size | `21,049,052` bytes |
| Leading header | `54` samples (skip) |
| Rows | `2646` |
| Samples per row (stride) | `3976` |
| Active samples per row | `3968` (first N; discard the remaining 8) |
| Trailing | one stride-row (`3976` samples), ignored |
| Value ceiling | `16383` (14-bit; clamp on read) |

Decode:

```
offset = 54 samples
for row in 0 .. 2645:
    read 3976 samples
    keep samples 0 .. 3967 as image
    discard samples 3968 .. 3975
values = min(value, 16383)
```

Size check: `(54 + 2646*3976 + 1*3976) * 2 = 21,049,052` bytes.

The result is a `3968 x 2646` single-channel CFA mosaic, one 14-bit value per
photosite, no rendering applied.

## Sensor geometry and CFA

| Quantity | Value |
|---|---|
| Full sensor frame | `3968 x 2646` |
| Recommended active area origin | `(2, 2)` |
| Recommended active area size | `3964 x 2642` |
| CFA pattern | RGGB |
| Bit depth | 14 (values 0 to 16383) |

The `3968 x 2646` grid is the full readout. The reference converter writes all of
it and marks a `(2, 2)` crop origin with a `3964 x 2642` active size in the DNG,
trimming a 2px border. That border is genuine sensor data (ordinary photosites
continuous with the scene), trimmed by DNG convention as a demosaic-quality
margin, not masked optical black. A decoder should expose the full frame and the
`(2, 2)` / `3964 x 2642` crop so output matches existing M8 DNGs in ACR.

## Metadata (from the JPG)

The paired JPG's EXIF and Leica MakerNote carry everything the DNG needs:

| Field | Use |
|---|---|
| Body serial | `UniqueCameraModel` / identification |
| ISO | tagged; drives any ISO-dependent handling |
| Exposure time | tagged |
| Focal length | `FocalLength`; `FocalLengthIn35mmFilm = round(focal * 1.33)` |
| Firmware string | `Software` tag (omit if absent) |
| MakerNote | copied verbatim into the DNG |

The M8 crop factor is `1.33`. For an un-coded lens the JPG records no true
aperture (only the camera's rough estimate); a baseline decoder may omit
`FNumber` or pass the JPG's estimate through - accurate aperture recovery is out
of scope here.

## Colour

The M8 colour characterisation is carried as DNG colour matrices under two
calibration illuminants:

| Illuminant tag | Illuminant |
|---|---|
| 17 | Standard Light A |
| 21 | D65 |

`AsShotNeutral` is computed per shot from that frame's own data, so two frames can
legitimately carry different values; it is not a fixed daylight constant. (A
variant colour set matching the M9 "M8RAW" profile also exists; the Standard-A /
D65 pair above is the default.)

## Baseline DNG output

A plain decode targets a 16-bit linear uncompressed DNG:

- a single uncompressed strip of 16-bit linear CFA samples, full `3968 x 2646`;
- `WhiteLevel` `16383`;
- no `BlackLevel` tag (the reference writes none; a developer assumes 0), or an
  optional tag if the implementer chooses - it does not alter pixels;
- `CFAPattern` RGGB;
- `DefaultCropOrigin` `(2, 2)`, `DefaultCropSize` `3964 x 2642`;
- the M8 colour matrices and illuminants above;
- the MakerNote and relevant EXIF from the JPG.

## Reference decode command

The reference converter produces a plain decode with:

```
m8raw2dng2 -v -b 92 --cfa RGGB -i ./Input -o ./Output
```

with any `.BIA` kept out of the input folder and no sensor database in play, so
`BlackLevel` stays a plain per-frame tag (no ISO-gain scaling) and the pixels are
a clean, unmodified decode. A decoder producing byte-identical CFA data and the
tags above is format-complete; everything beyond it is optional refinement.

## Verification target

A correct baseline decoder, run on the shared sample frames, should produce CFA
pixel data byte-identical to the reference's plain `-v` output (the estimated
`FNumber` is the only field that legitimately differs, and it is optional). ISO
160, 320, 640, 1250 and 2500 frames all decode with the identical geometry and
header; the format is ISO-independent.
