m8raw2dng2 - Leica M8 RAW to DNG converter
Version 2.13.1 beta - standalone macOS build (arm64)

Converts the Leica M8's uncompressed service-mode RAW files into
16-bit linear DNG with correct M8 colour, geometry and metadata.

USE
  1. Place .RAW files, each with its .JPG, in Input beside the app.
  2. Launch; set options; Convert.
  3. DNGs are written to Output.

FIRST LAUNCH
  Unsigned build. macOS blocks the first open:
  right-click -> Open -> Open. If still blocked:
  xattr -dr com.apple.quarantine /path/to/m8raw2dng2.app
  Once only.

PLACEMENT
  The app resolves lensdb.ini, sensdb.ini, Input and Output
  relative to its own location. Keep m8raw2dng2.app inside this
  folder, in a writable directory; do not move the app into
  /Applications, where adjacent files cannot be created.

FILES BESIDE THE APP
  lensdb.ini   optional 6-bit lens table; edit via "Edit lenses..."
  sensdb.ini   optional per-ISO sensor calibration
  Input        source .RAW + .JPG, created on first launch
  Output       converted .DNG, created on first launch

PROJECT
  Source, documentation and updates:
  https://github.com/botrx555/m8raw2dng2
