# Google Photos Takeout Captioner

Adds a white caption strip below each photo from a Google Takeout export,
containing metadata pulled from the accompanying JSON sidecar file:
filename, description, date taken, location, people, device, album owner,
and shared album comments.

## What it does

- Walks a Google Takeout **Google Photos** export folder
- Matches each photo to its `.json` metadata sidecar
- Writes a captioned copy of the photo (original + white strip added below)
  to an output folder, preserving the original folder structure
- Sets the captioned output file's modified time to match the photo's
  actual "Taken" date
- Leaves video files (mp4, mov, etc.) in place in the source folder, and
  creates a symbolic link to each one at the matching location in the
  output folder instead (no caption is burned into video). The symlink's
  own modified time is set to match "Taken" date.
- Handles `-edited` photo pairs (e.g. `IMG_1234.jpg` +
  `IMG_1234-edited.jpg`) by using only the edited version

## Requirements

```
pip install Pillow requests
```

(`requests` is only needed if you use `--geocode`.)

On macOS, if you hit an `externally-managed-environment` error from pip,
use a virtual environment instead:

```
python3 -m venv photo-venv
source photo-venv/bin/activate
pip install Pillow requests
```

## Basic usage

```
python3 caption_photos.py <input_folder> <output_folder> [options]
```

`<input_folder>` should point at the extracted Takeout folder containing
the photo + `.json` pairs, e.g. `"Takeout/Google Photos/Photos from 2019"`.

## Options

| Flag | What it does |
|---|---|
| `--geocode` | Reverse-geocodes GPS coordinates into a place name (e.g. "Paris, Île-de-France, France") instead of raw lat/long. Requires internet and the `requests` library. Rate-limited to ~1 request/second per Nominatim's usage policy, so large libraries with many unique locations will take a while. Results are in English and cached per unique location. |
| `--no-location` | Never include location info in the caption at all — no coordinates, no geocoding. Useful for scanned slides or old photos with bogus/inherited GPS data. |
| `--font-size N` | Forces a fixed caption font size in pixels, identical on every image regardless of resolution. |
| `--font-scale N` | Divisor for the default auto-scaled font size (`font_size = image_width / N`). Lower = larger text, higher = smaller. Default is `60`. Keeps caption text visually proportional across photos of different resolutions. Ignored if `--font-size` is set. |
| `--touch-source` | Also sets the modified time of **original source files** (in the input folder) to match the "Taken" date: the source image and its JSON sidecar for photos, or the source video and its JSON sidecar for videos. For `-edited` photos, the corresponding non-edited original is touched too. This modifies your Takeout export in place — off by default. |

## What's in the caption

In order, when present in the metadata:

1. **File** — the actual filename on disk
2. **Description** — if you added a caption in Google Photos
3. **Taken** — date/time the photo was actually taken
4. **Location** — place name (with `--geocode`) or raw GPS coordinates
5. **People** — names Google Photos recognized in the photo
6. **Device** — the camera/phone model, if Google recorded it
7. **Owner** — the shared album's content owner, if applicable
8. **Comments** — shared album comments, oldest first, as `Name: text`

Fields not listed above (view counts, internal URLs, upload origin info,
etc.) are intentionally left out of the caption.

## Notes and known behaviors

- **Orientation**: photos are auto-rotated to match their EXIF orientation
  tag before captioning, so the output matches what you see in Photos/Preview.
- **`-edited` files**: if both `IMG_1234.jpg` and `IMG_1234-edited.jpg`
  exist, only the edited version is captioned; the original is skipped
  (logged in the output). Metadata is looked up under the edited filename
  first, falling back to the original filename's JSON if needed.
- **Videos**: left in place in the source folder — nothing is moved or
  deleted. A symbolic link to each video is created at the matching
  location in the output folder. The symlink's own modified time (not
  the source video's) is always set to match "Taken" date. Touching the
  *original* source video and its JSON sidecar with that timestamp only
  happens with `--touch-source`, exactly like photos. Since the output
  only contains links, the output folder isn't a self-contained copy of
  your videos — moving or deleting the source Takeout folder will break
  those links.
- **Fonts**: tries a list of common system font paths (macOS, Windows,
  Linux) before falling back to Pillow's built-in bitmap font, which
  ignores `--font-size`/`--font-scale` entirely — if you see a
  `[warning] No scalable font found` message, none of the listed paths
  existed on your system.

## Example

```
python3 caption_photos.py \
  "Takeout/Google Photos/Photos from 2018" \
  "Takeout/2018_captioned" \
  --geocode --font-scale 45 --touch-source
```
