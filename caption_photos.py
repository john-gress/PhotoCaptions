#!/usr/bin/env python3
"""
Google Takeout Photos -> Captioned Images
-------------------------------------------
Walks a Google Takeout Google Photos export, matches each photo to its
JSON sidecar metadata file, and produces a copy of the photo with a
white caption strip added below it containing:
  - Description / title (whichever is present)
  - Date/time taken
  - Location (place name via --geocode, raw GPS coords otherwise,
    or omitted entirely with --no-location)
  - Any other metadata fields found in the JSON that aren't already covered

Usage:
    pip install Pillow requests
    python caption_photos.py <input_folder> <output_folder> [options]

Options:
    --geocode              Reverse-geocode GPS coords into a place name
                            (needs internet; rate-limited to Nominatim's
                            1 request/second policy, so large libraries
                            with many unique locations will take a while)
    --font-size N           Force a fixed font size in pixels, identical
                            on every image regardless of resolution.
    --font-scale N          Divisor for auto-scaled font size
                            (font_size = image_width / N). Lower values
                            = larger text, higher = smaller. Default 60.
                            Keeps text proportionally consistent across
                            photos of different resolutions - ignored if
                            --font-size is set.
    --no-location           Never print location info at all (useful for
                            scanned slides/photos with bogus GPS data)

Notes:
  - <input_folder> should point at the folder(s) extracted from your
    Takeout zip(s) that contain the actual photo + .json pairs
    (e.g. "Takeout/Google Photos/Photos from 2019").
  - Videos (mp4, mov, etc.) are not captioned, but are MOVED (not
    copied) into the output folder in the same relative location, and
    their modified time is set to match photoTakenTime if a matching
    JSON sidecar is found. The original video will no longer exist in
    the Takeout export after this - only the output copy remains.
  - Edited versions: if both "IMG_1234.jpg" and "IMG_1234-edited.jpg"
    exist in the same folder, only the "-edited" version is processed;
    the original is skipped. Metadata is looked up under the edited
    filename first, falling back to the original filename's JSON if
    Takeout didn't produce a separate sidecar for the edited copy.
"""

import json
import os
import shutil
import time
import argparse
import textwrap
from pathlib import Path
from collections import defaultdict
from datetime import datetime, timezone
from PIL import Image, ImageDraw, ImageFont, ImageOps

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.heic', '.webp', '.gif', '.bmp', '.tiff'}
VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.m4v', '.3gp', '.mpg', '.mpeg', '.wmv', '.mkv'}
EDITED_SUFFIX = '-edited'

def get_device_type(meta: dict):
    """deviceType isn't a top-level field - Google nests it inside
    googlePhotosOrigin, under whichever upload-source key applies
    (mobileUpload, webUpload, etc.), e.g.:
      "googlePhotosOrigin": {"mobileUpload": {"deviceType": "IOS_PHONE"}}
    Check top-level first in case that ever changes, then search the
    nested origin dict for it."""
    device = meta.get('deviceType')
    if device:
        return device
    origin = meta.get('googlePhotosOrigin') or {}
    for value in origin.values():
        if isinstance(value, dict) and value.get('deviceType'):
            return value['deviceType']
    return None


def extract_extra_fields(meta: dict):
    """Pulls out a few specific extra fields worth showing, if present:
    deviceType, contentOwnerName, and sharedAlbumComments (sorted
    chronologically, each attributed to a name). Everything else in the
    JSON (imageViews, url, googlePhotosOrigin, etc.) is intentionally
    ignored as not caption-worthy."""
    lines = []

    device = get_device_type(meta)
    if device:
        lines.append(f"Device: {device}")

    owner = meta.get('contentOwnerName')
    if owner:
        lines.append(f"Owner: {owner}")

    comments = meta.get('sharedAlbumComments')
    if comments:
        def comment_ts(c):
            ts = (c.get('creationTime') or {}).get('timestamp')
            try:
                return int(ts)
            except (TypeError, ValueError):
                return 0

        for c in sorted(comments, key=comment_ts):
            text = c.get('text')
            if not text:
                continue
            name = (
                c.get('contentOwnerName') or c.get('author')
                or c.get('authorName') or c.get('name')
                or owner or 'Unknown'
            )
            lines.append(f"{name}: {text}")

    return lines

# Minimum seconds between geocode requests, per Nominatim's usage policy
GEOCODE_MIN_INTERVAL = 1.0
_last_geocode_time = 0.0

# Font paths to try, in order, across macOS / Windows / Linux. A bare
# "DejaVuSans.ttf" only resolves on systems where it happens to be on
# Pillow's font search path (common on Linux, NOT present by default on
# macOS) - if none of these resolve, we fall back to Pillow's built-in
# bitmap font, which on older Pillow versions ignores the size argument.
FONT_CANDIDATES = [
    "DejaVuSans.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",   # macOS
    "/System/Library/Fonts/Helvetica.ttc",             # macOS
    "/Library/Fonts/Arial.ttf",                        # macOS (older)
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", # Linux
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",  # Linux
    "C:\\Windows\\Fonts\\arial.ttf",                   # Windows
    "Arial.ttf",
]

_font_cache = {}


def get_font(size: int):
    if size in _font_cache:
        return _font_cache[size]
    for path in FONT_CANDIDATES:
        try:
            font = ImageFont.truetype(path, size)
            _font_cache[size] = font
            return font
        except Exception:
            continue
    # Last resort: Pillow's built-in font. Recent Pillow (>=10.1) accepts
    # a size argument here; older versions silently ignore it.
    try:
        font = ImageFont.load_default(size=size)
    except TypeError:
        font = ImageFont.load_default()
        print(f"    [warning] No scalable font found on this system - "
              f"captions will use Pillow's tiny built-in font regardless "
              f"of --font-size. Install a TrueType font or point "
              f"FONT_CANDIDATES at one to fix this.")
    _font_cache[size] = font
    return font


def is_edited(image_path: Path) -> bool:
    return image_path.stem.lower().endswith(EDITED_SUFFIX)


def base_stem(image_path: Path) -> str:
    """Stem with any '-edited' suffix stripped off."""
    stem = image_path.stem
    if stem.lower().endswith(EDITED_SUFFIX):
        return stem[: -len(EDITED_SUFFIX)]
    return stem


def select_images(all_images):
    """Group images by (folder, base name, extension). When both an
    original and an '-edited' version exist, keep only the edited one."""
    groups = defaultdict(list)
    for p in all_images:
        key = (p.parent, base_stem(p).lower(), p.suffix.lower())
        groups[key].append(p)

    selected = []
    for key, paths in groups.items():
        edited = [p for p in paths if is_edited(p)]
        if edited:
            selected.extend(edited)
            for p in paths:
                if p not in edited:
                    print(f"[skip] {p.name} - using edited version instead")
        else:
            selected.extend(paths)
    return selected


def _json_candidates_for(image_path: Path):
    return [
        image_path.with_suffix(image_path.suffix + '.json'),
        image_path.with_suffix(image_path.suffix + '.supplemental-metadata.json'),
        Path(str(image_path) + '.json'),
        Path(str(image_path) + '.supplemental-metadata.json'),
    ]


def _fuzzy_json_match(image_path: Path, stem: str):
    prefix = stem[:46]
    for f in image_path.parent.glob('*.json'):
        if f.stem.startswith(prefix):
            return f
    return None


def find_json_for_image(image_path: Path):
    """Google Takeout's JSON naming has varied over the years - try the
    known patterns, then fall back to a fuzzy filename match (Takeout
    sometimes truncates long original filenames in the sidecar name).
    For '-edited' files, also fall back to the original (non-edited)
    filename's metadata, since Takeout often only writes one JSON per
    photo even when an edited copy exists."""
    for c in _json_candidates_for(image_path):
        if c.exists():
            return c

    match = _fuzzy_json_match(image_path, image_path.stem)
    if match:
        return match

    if is_edited(image_path):
        original_path = image_path.with_name(base_stem(image_path) + image_path.suffix)
        for c in _json_candidates_for(original_path):
            if c.exists():
                return c
        match = _fuzzy_json_match(original_path, original_path.stem)
        if match:
            return match

    return None


def fmt_timestamp(ts_dict):
    if not ts_dict or 'timestamp' not in ts_dict:
        return None
    try:
        dt = datetime.fromtimestamp(int(ts_dict['timestamp']), tz=timezone.utc)
        return dt.strftime('%Y-%m-%d %H:%M UTC')
    except Exception:
        return None


def get_taken_epoch(meta: dict):
    """Raw Unix timestamp from photoTakenTime, if present and valid."""
    ts = (meta.get('photoTakenTime') or {}).get('timestamp')
    try:
        return int(ts)
    except (TypeError, ValueError):
        return None


_geocode_cache = {}


def short_place_name(address: dict, display_name: str) -> str:
    """Build a concise 'City, Region, Country' style string instead of
    Nominatim's full street-level address."""
    if not address:
        return display_name
    locality = (
        address.get('city') or address.get('town') or address.get('village')
        or address.get('hamlet') or address.get('suburb') or address.get('county')
    )
    region = address.get('state') or address.get('region')
    country = address.get('country')
    parts = [p for p in (locality, region, country) if p]
    return ', '.join(parts) if parts else display_name


def reverse_geocode(lat, lon, label=''):
    """Reverse-geocode with Nominatim. Rate-limited to ~1 req/sec.
    Returns (place_name_or_None, error_reason_or_None)."""
    global _last_geocode_time

    key = (round(lat, 4), round(lon, 4))
    if key in _geocode_cache:
        return _geocode_cache[key], None

    elapsed = time.monotonic() - _last_geocode_time
    if elapsed < GEOCODE_MIN_INTERVAL:
        time.sleep(GEOCODE_MIN_INTERVAL - elapsed)

    try:
        import requests
        r = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={'lat': lat, 'lon': lon, 'format': 'json', 'accept-language': 'en'},
            headers={'User-Agent': 'photo-captioner/1.0'},
            timeout=5,
        )
        _last_geocode_time = time.monotonic()

        if r.status_code != 200:
            return None, f"HTTP {r.status_code}"

        data = r.json()
        if 'error' in data:
            return None, data.get('error', 'no result')

        name = short_place_name(data.get('address'), data.get('display_name'))
        _geocode_cache[key] = name
        return name, None

    except ImportError:
        return None, "requests library not installed"
    except Exception as e:
        _last_geocode_time = time.monotonic()
        return None, f"{type(e).__name__}: {e}"


def build_caption(meta: dict, geocode: bool = False, show_location: bool = True,
                   image_label: str = ''):
    lines = []

    # Filename is always shown, regardless of whether a description
    # exists - previously this was either/or with description, which
    # meant the filename silently disappeared on any photo that had a
    # caption typed in Google Photos.
    if image_label:
        lines.append(f"File: {image_label}")

    desc = meta.get('description')
    if desc:
        lines.append(desc)

    taken = fmt_timestamp(meta.get('photoTakenTime'))
    if taken:
        lines.append(f"Taken: {taken}")

    if show_location:
        geo = meta.get('geoData') or meta.get('geoDataExif')
        if geo and (geo.get('latitude') or geo.get('longitude')):
            lat, lon = geo.get('latitude'), geo.get('longitude')
            if lat or lon:
                place, err = (None, None)
                if geocode:
                    place, err = reverse_geocode(lat, lon, label=image_label)
                    if err:
                        print(f"    [geocode failed: {err}] falling back to coordinates")
                lines.append(f"Location: {place or f'{lat:.5f}, {lon:.5f}'}")

    people = meta.get('people')
    if people:
        names = ', '.join(p.get('name', '') for p in people if p.get('name'))
        if names:
            lines.append(f"People: {names}")

    lines.extend(extract_extra_fields(meta))

    return lines


def caption_image(image_path: Path, json_path: Path, output_path: Path,
                   geocode: bool = False, show_location: bool = True,
                   font_size_override: int = None, font_scale: float = 60.0):
    with open(json_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)

    lines = build_caption(meta, geocode=geocode, show_location=show_location,
                           image_label=image_path.name)
    if not lines:
        lines = ["No metadata found"]

    img = Image.open(image_path)
    # Apply the EXIF orientation tag to the actual pixel data before
    # doing anything else. Pillow's open() does NOT auto-rotate, so
    # without this, images that display correctly in Photos/Preview
    # (because they read the orientation tag) would get captioned in
    # their raw, un-rotated form. exif_transpose() also resets the
    # orientation tag to "normal" so the output doesn't get rotated
    # again by whatever app opens it next.
    img = ImageOps.exif_transpose(img)
    img = img.convert('RGB')
    width, height = img.size

    font_size = font_size_override if font_size_override else max(14, int(width / font_scale))
    font = get_font(font_size)

    max_chars = max(20, width // (font_size // 2))
    wrapped = []
    for line in lines:
        wrapped.extend(textwrap.wrap(line, width=max_chars) or [''])

    line_height = int(font_size * 1.1)
    padding = int(font_size * 0.8)
    strip_height = padding * 2 + line_height * len(wrapped)

    new_img = Image.new('RGB', (width, height + strip_height), 'white')
    new_img.paste(img, (0, 0))
    draw = ImageDraw.Draw(new_img)

    y = height + padding
    for line in wrapped:
        draw.text((padding, y), line, fill='black', font=font)
        y += line_height

    output_path.parent.mkdir(parents=True, exist_ok=True)
    new_img.save(output_path, quality=92)

    # Set the output file's modified/access time to match "Taken" date,
    # so Finder's Date Modified column (and any date-based sort) reflects
    # when the photo was actually taken rather than when this script ran.
    taken_epoch = get_taken_epoch(meta)
    if taken_epoch is not None:
        try:
            os.utime(output_path, (taken_epoch, taken_epoch))
        except Exception as e:
            print(f"    [warning] Could not set file timestamp: {e}")


def process_videos(in_root: Path, out_root: Path):
    """Moves video files (mp4, mov, etc.) straight into the output
    folder, preserving the relative directory structure, and sets each
    moved file's modified time to its photoTakenTime if a matching JSON
    sidecar is found. Metadata is read BEFORE the move so a bad/missing
    JSON never blocks the move itself - it just means the timestamp
    won't be updated. This MOVES (not copies) files: the originals will
    no longer exist in the Takeout export afterward. The JSON sidecar
    itself is left in place; only the video file is relocated."""
    videos = [p for p in in_root.rglob('*') if p.suffix.lower() in VIDEO_EXTS]

    moved = 0
    no_metadata = 0
    errors = 0

    for video_path in videos:
        taken_epoch = None
        json_path = find_json_for_image(video_path)
        if json_path:
            try:
                with open(json_path, 'r', encoding='utf-8') as f:
                    meta = json.load(f)
                taken_epoch = get_taken_epoch(meta)
            except Exception as e:
                print(f"    [warning] Could not read metadata for {video_path.name}: {e}")
        else:
            print(f"[warning] No metadata found for {video_path.name} - moving without timestamp update")
            no_metadata += 1

        rel = video_path.relative_to(in_root)
        out_path = out_root / rel
        out_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            shutil.move(str(video_path), str(out_path))
        except Exception as e:
            print(f"[error] Could not move {rel}: {e}")
            errors += 1
            continue

        moved += 1
        print(f"[moved] {rel}")

        if taken_epoch is not None:
            try:
                os.utime(out_path, (taken_epoch, taken_epoch))
            except Exception as e:
                print(f"    [warning] Could not set file timestamp: {e}")

    print(f"\nVideos: {moved} moved, {no_metadata} moved without metadata, {errors} errors.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input_folder', help='Root of the extracted Takeout Google Photos export')
    ap.add_argument('output_folder', help='Where captioned images should be written')
    ap.add_argument('--geocode', action='store_true',
                     help='Reverse geocode GPS coordinates into a place name (needs internet + `pip install requests`; rate-limited to 1 req/sec)')
    ap.add_argument('--font-size', type=int, default=None,
                     help='Force a fixed caption font size in pixels, identical on every image regardless of resolution (default: auto-scales with image width - see --font-scale)')
    ap.add_argument('--font-scale', type=float, default=60.0,
                     help='Divisor used for auto-scaled font size (font_size = image_width / font_scale). Lower = larger text, higher = smaller text. Default: 60. Ignored if --font-size is set.')
    ap.add_argument('--no-location', action='store_true',
                     help='Never include location info in captions (useful for scans with invalid GPS data)')
    args = ap.parse_args()

    in_root = Path(args.input_folder)
    out_root = Path(args.output_folder)

    all_images = [p for p in in_root.rglob('*') if p.suffix.lower() in IMAGE_EXTS]
    images_to_process = select_images(all_images)

    count = 0
    skipped = 0
    for image_path in images_to_process:
        json_path = find_json_for_image(image_path)
        if not json_path:
            skipped += 1
            print(f"[skip] No metadata found for {image_path}")
            continue
        rel = image_path.relative_to(in_root)
        out_path = out_root / rel
        try:
            caption_image(
                image_path, json_path, out_path,
                geocode=args.geocode,
                show_location=not args.no_location,
                font_size_override=args.font_size,
                font_scale=args.font_scale,
            )
            count += 1
            print(f"[ok] {rel}")
        except Exception as e:
            skipped += 1
            print(f"[error] {rel}: {e}")

    print(f"\nDone. {count} images captioned, {skipped} skipped.")

    process_videos(in_root, out_root)


if __name__ == '__main__':
    main()
