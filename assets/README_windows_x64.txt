m8raw2dng2 - Leica M8 RAW to DNG converter
Version 2.13.1 beta - standalone Windows build (x64)

Converts the Leica M8's uncompressed service-mode RAW files into
16-bit linear DNG with correct M8 colour, geometry and metadata.

USE
  1. Place .RAW files, each with its .JPG, in Input beside the app.
  2. Launch; set options; Convert.
  3. DNGs are written to Output.

FIRST LAUNCH
  Unsigned build. SmartScreen may warn:
  More info -> Run anyway. Once only.

PLACEMENT
  m8raw2dng2.exe resolves lensdb.ini, sensdb.ini, Input and Output
  relative to its own location. Keep the whole folder together -
  the .exe needs the _internal folder beside it - in a writable
  directory.

FILES BESIDE THE APP
  lensdb.ini   optional 6-bit lens table; edit via "Edit lenses..."
  sensdb.ini   optional per-ISO sensor calibration
  Input        source .RAW + .JPG, created on first launch
  Output       converted .DNG, created on first launch

PROJECT
  Source, documentation and updates:
  https://github.com/botrx555/m8raw2dng2
