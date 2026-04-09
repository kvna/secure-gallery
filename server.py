#!/usr/bin/env python3
"""
Local Photo Gallery Server - with zip session management
Run: python3 server.py [--folder ./photos] [--zip file.zip] [--port 8765]
"""

import os, sys, json, shutil, zipfile, argparse, base64, mimetypes
import threading, traceback, io
from datetime import datetime
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote

try:
    from PIL import Image, ExifTags
    import piexif
    HAS_PIL = True
except ImportError:
    HAS_PIL = False
    print("Warning: Pillow/piexif not installed. pip install Pillow piexif")

try:
    import imagehash
    HAS_IMAGEHASH = True
except ImportError:
    HAS_IMAGEHASH = False
    print("Warning: imagehash not installed. pip install imagehash  (duplicate detection disabled)")

try:
    import pyzipper
    HAS_PYZIPPER = True
except ImportError:
    HAS_PYZIPPER = False

# Set by start-gallery.sh via env; used for AES-256 zip encryption/decryption.
GALLERY_PASSWORD: str = os.environ.get('GALLERY_PASSWORD', '')

IMAGE_EXTENSIONS = {'.jpg','.jpeg','.png','.gif','.webp','.bmp','.tiff','.tif','.heic'}
VIDEO_EXTENSIONS = {'.mp4','.mov','.avi','.mkv','.webm','.m4v','.mts','.m2ts','.3gp'}
SUPPORTED_EXTENSIONS = IMAGE_EXTENSIONS | VIDEO_EXTENSIONS
PREVIOUS_SESSIONS_DIR = 'previous_sessions'
DUPLICATES_DIR        = 'duplicates'
SESSION_FOLDER        = '.gallery_session'   # fixed extraction dir, sibling of photos/
PHASH_THRESHOLD       = 8

def dup_folder(photos_folder: Path) -> Path:
    """
    Duplicates folder sits ALONGSIDE photos/, not inside it.
    ~/ns/photos  →  ~/ns/duplicates
    This keeps it completely outside the gallery scan.
    """
    return Path(photos_folder).parent / DUPLICATES_DIR   # hamming distance — 0=identical, ≤8=very similar, ≤15=similar

import os as _os

def _fast_walk(folder, _root=None):
    """Faster than Path.rglob on FUSE/Crostini — uses os.scandir.
    Skips excluded top-level dirs (duplicates/, previous_sessions/, etc.)"""
    folder = Path(folder)
    if _root is None:
        _root = folder
    try:
        with _os.scandir(str(folder)) as it:
            entries = sorted(it, key=lambda e: e.name)
    except PermissionError:
        return
    for entry in entries:
        p = Path(entry.path)
        if entry.is_file(follow_symlinks=False):
            yield p
        elif entry.is_dir(follow_symlinks=False):
            # Skip excluded dirs at the top level only
            if folder == _root:
                dir_name = entry.name
                if dir_name in _EXCLUDED_DIRS or dir_name.startswith('.'):
                    continue
            yield from _fast_walk(p, _root=_root)



EXIF_TAG_LABELS = {
    'Make':'Camera Make','Model':'Camera Model','Software':'Software',
    'DateTime':'Date Taken','DateTimeOriginal':'Original Date','DateTimeDigitized':'Digitized Date',
    'ExposureTime':'Exposure Time','FNumber':'Aperture (f/)','ISOSpeedRatings':'ISO Speed',
    'ShutterSpeedValue':'Shutter Speed','ApertureValue':'Aperture Value','ExposureBiasValue':'Exposure Bias',
    'MaxApertureValue':'Max Aperture','MeteringMode':'Metering Mode','Flash':'Flash',
    'FocalLength':'Focal Length','FocalLengthIn35mmFilm':'Focal Length (35mm)',
    'ColorSpace':'Color Space','PixelXDimension':'Width (px)','PixelYDimension':'Height (px)',
    'ExposureMode':'Exposure Mode','WhiteBalance':'White Balance','SceneCaptureType':'Scene Type',
    'Contrast':'Contrast','Saturation':'Saturation','Sharpness':'Sharpness',
    'SubjectDistanceRange':'Subject Distance','GPSLatitude':'GPS Latitude','GPSLongitude':'GPS Longitude',
    'GPSAltitude':'GPS Altitude','GPSSpeed':'GPS Speed','GPSImgDirection':'GPS Direction',
    'LensModel':'Lens Model','LensMake':'Lens Make','Artist':'Artist','Copyright':'Copyright',
    'ImageDescription':'Description','XResolution':'X Resolution','YResolution':'Y Resolution',
    'ResolutionUnit':'Resolution Unit','Orientation':'Orientation','BitsPerSample':'Bits Per Sample',
    'Compression':'Compression','PhotometricInterpretation':'Color Mode',
}
METERING_MODES={0:'Unknown',1:'Average',2:'Centre-weighted',3:'Spot',4:'Multi-spot',5:'Multi-segment',6:'Partial'}
FLASH_VALUES={0:'No Flash',1:'Flash Fired',16:'Flash Off',24:'Flash Off',25:'Flash Fired (auto)',32:'No Flash (unavail)'}
EXPOSURE_MODES={0:'Auto',1:'Manual',2:'Auto Bracket'}
WHITE_BALANCE={0:'Auto',1:'Manual'}
ORIENTATION_LABELS={1:'Normal',2:'Mirrored',3:'Rotated 180',4:'Mirrored+180',5:'Mirrored+90CCW',6:'Rotated 90CW',7:'Mirrored+90CW',8:'Rotated 90CCW'}
COLOR_SPACE={1:'sRGB',65535:'Uncalibrated'}
SCENE_TYPES={0:'Standard',1:'Landscape',2:'Portrait',3:'Night'}
RESOLUTION_UNITS={1:'No unit',2:'inch',3:'centimetre'}

# ── FORMAT HELPERS ─────────────────────────────────────────────────────────────

def format_rational(val):
    if isinstance(val,tuple) and len(val)==2:
        n,d=val
        if d==0: return str(n)
        r=n/d
        return str(int(r)) if r==int(r) else f"{r:.4f}".rstrip('0').rstrip('.')
    return str(val)

def format_exposure_time(val):
    if isinstance(val,tuple) and len(val)==2:
        n,d=val
        if d==0: return f"{n}s"
        if n==1: return f"1/{d}s"
        r=n/d
        return f"{r:.1f}s" if r>=1 else f"1/{int(d/n)}s"
    return str(val)

def format_focal_length(val):
    return f"{format_rational(val)}mm" if isinstance(val,tuple) else f"{val}mm"

def dms_to_decimal(dms,ref):
    try:
        d=dms[0][0]/dms[0][1]; m=dms[1][0]/dms[1][1]; s=dms[2][0]/dms[2][1]
        dec=d+m/60+s/3600
        return round(-dec if ref in ('S','W') else dec,6)
    except: return None

# ── METADATA ───────────────────────────────────────────────────────────────────

def extract_metadata(filepath):
    meta={}; path=Path(filepath); stat=path.stat()
    meta['file']={
        'Filename':path.name,
        'File Size':f"{stat.st_size/1024:.1f} KB" if stat.st_size<1024*1024 else f"{stat.st_size/(1024*1024):.2f} MB",
        'File Type':path.suffix.upper().lstrip('.'),
        'Modified':datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
    }
    if not HAS_PIL: return meta
    try:
        with Image.open(filepath) as img:
            mp=(img.width*img.height)/1_000_000
            meta['image']={'Width':f"{img.width} px",'Height':f"{img.height} px",
                           'Mode':img.mode,'Format':img.format or path.suffix.upper().lstrip('.'),
                           'Megapixels':f"{mp:.1f} MP"}
            exif_data={}; gps_data={}
            raw=img._getexif() if hasattr(img,'_getexif') else None
            if raw:
                for tid,val in raw.items():
                    tname=ExifTags.TAGS.get(tid,str(tid))
                    if tname=='GPSInfo':
                        for gid,gv in val.items(): gps_data[ExifTags.GPSTAGS.get(gid,str(gid))]=gv
                        continue
                    if tname not in EXIF_TAG_LABELS: continue
                    label=EXIF_TAG_LABELS[tname]
                    if tname=='ExposureTime': val=format_exposure_time(val)
                    elif tname=='FNumber': val=f"f/{format_rational(val)}"
                    elif tname in ('FocalLength','FocalLengthIn35mmFilm'): val=format_focal_length(val)
                    elif tname in ('ApertureValue','ShutterSpeedValue','ExposureBiasValue','MaxApertureValue'): val=format_rational(val)
                    elif tname in ('XResolution','YResolution'): val=f"{format_rational(val)} dpi"
                    elif tname=='MeteringMode': val=METERING_MODES.get(val,str(val))
                    elif tname=='Flash': val=FLASH_VALUES.get(val,str(val))
                    elif tname=='ExposureMode': val=EXPOSURE_MODES.get(val,str(val))
                    elif tname=='WhiteBalance': val=WHITE_BALANCE.get(val,str(val))
                    elif tname=='Orientation': val=ORIENTATION_LABELS.get(val,str(val))
                    elif tname=='ColorSpace': val=COLOR_SPACE.get(val,str(val))
                    elif tname=='SceneCaptureType': val=SCENE_TYPES.get(val,str(val))
                    elif tname=='ResolutionUnit': val=RESOLUTION_UNITS.get(val,str(val))
                    elif isinstance(val,bytes):
                        try: val=val.decode('utf-8',errors='replace').strip('\x00')
                        except: val=repr(val)
                    elif isinstance(val,tuple) and len(val)==2: val=format_rational(val)
                    if val and str(val).strip(): exif_data[label]=str(val)
            if exif_data: meta['exif']=exif_data
            if gps_data:
                gf={}; lat=lon=None
                if 'GPSLatitude' in gps_data and 'GPSLatitudeRef' in gps_data:
                    lat=dms_to_decimal(gps_data['GPSLatitude'],gps_data['GPSLatitudeRef'])
                    if lat is not None: gf['Latitude']=f"{lat} {'N' if lat>=0 else 'S'}"
                if 'GPSLongitude' in gps_data and 'GPSLongitudeRef' in gps_data:
                    lon=dms_to_decimal(gps_data['GPSLongitude'],gps_data['GPSLongitudeRef'])
                    if lon is not None: gf['Longitude']=f"{lon} {'E' if lon>=0 else 'W'}"
                if lat is not None and lon is not None:
                    gf['Maps Link']=f"https://maps.google.com/?q={lat},{lon}"
                if gf: meta['gps']=gf
    except Exception as e: meta['_error']=str(e)
    return meta

# ── THUMBNAIL ──────────────────────────────────────────────────────────────────

THUMB_CACHE_DIR = '.thumbcache'   # inside photos folder, hidden from gallery
THUMB_SIZE      = 400
_thumb_semaphore = threading.Semaphore(3)   # max 3 concurrent thumbnail generations


def _thumb_cache_path(photos_folder: Path, image_rel: str) -> Path:
    """Return the cache file path for a given image."""
    # Use a flat structure — replace path separators with __ to avoid subdirs
    safe = image_rel.replace('/', '__').replace('\\', '__')
    return photos_folder / THUMB_CACHE_DIR / (safe + '.jpg')


def make_thumbnail(filepath, photos_folder=None, image_rel=None, max_size=THUMB_SIZE):
    """
    Generate a thumbnail. If photos_folder and image_rel are given, check the
    disk cache first and save there after generation — so repeat requests are
    served from disk in microseconds rather than re-processing each time.
    """
    if not HAS_PIL:
        return None

    # Check disk cache first
    cache_path = None
    if photos_folder and image_rel:
        cache_path = _thumb_cache_path(Path(photos_folder), image_rel)
        if cache_path.exists():
            # Serve from cache — fast path
            try:
                return base64.b64encode(cache_path.read_bytes()).decode('ascii')
            except Exception:
                pass  # fall through to regenerate

    with _thumb_semaphore:
        try:
            # Load fully into memory first — avoids file-handle issues with rotate()
            with Image.open(filepath) as src:
                src.load()   # force full decode before closing file
                img = src.copy()

            # Apply EXIF orientation
            try:
                raw = img._getexif() if hasattr(img, '_getexif') else None
                if raw:
                    for tid, val in raw.items():
                        if ExifTags.TAGS.get(tid) == 'Orientation':
                            degrees = {3:180, 6:270, 8:90}.get(val, 0)
                            if degrees:
                                img = img.rotate(degrees, expand=True)
                            break
            except Exception:
                pass

            # Resize
            img.thumbnail((max_size, max_size), Image.LANCZOS)
            if img.mode in ('RGBA', 'P', 'LA'):
                img = img.convert('RGB')

            buf = io.BytesIO()
            img.save(buf, format='JPEG', quality=82)
            data = buf.getvalue()

            # Save to disk cache
            if cache_path:
                try:
                    cache_path.parent.mkdir(parents=True, exist_ok=True)
                    cache_path.write_bytes(data)
                except Exception as ce:
                    print(f"  ⚠  Cache write failed: {ce}", flush=True)

            return base64.b64encode(data).decode('ascii')

        except Exception as e:
            import traceback
            print(f"  ✘  make_thumbnail failed for {filepath}: {e}", flush=True)
            traceback.print_exc()
            return None

def make_video_thumbnail(filepath, photos_folder=None, image_rel=None):
    """
    Generate a thumbnail for a video file.
    Tries ffmpeg first (best quality), falls back to a placeholder icon.
    Returns base64-encoded JPEG string or None.
    """
    cache_path = None
    if photos_folder and image_rel:
        cache_path = _thumb_cache_path(Path(photos_folder), image_rel)
        if cache_path.exists():
            try:
                return base64.b64encode(cache_path.read_bytes()).decode('ascii')
            except Exception:
                pass

    with _thumb_semaphore:
        # Try ffmpeg — extract frame at 0.5s
        try:
            import subprocess, tempfile
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmp:
                tmp_path = tmp.name
            result = subprocess.run(
                ['ffmpeg', '-y', '-ss', '00:00:01', '-i', str(filepath),
                 '-vframes', '1', '-q:v', '3',
                 '-vf', f'scale={THUMB_SIZE}:{THUMB_SIZE}:force_original_aspect_ratio=decrease',
                 tmp_path],
                capture_output=True, timeout=15
            )
            if result.returncode == 0 and Path(tmp_path).exists():
                data = Path(tmp_path).read_bytes()
                Path(tmp_path).unlink(missing_ok=True)
                if cache_path:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(data)
                    except Exception:
                        pass
                return base64.b64encode(data).decode('ascii')
            Path(tmp_path).unlink(missing_ok=True)
        except (FileNotFoundError, Exception):
            pass  # ffmpeg not available or failed

        # Fallback: generate a simple dark placeholder with PIL
        if HAS_PIL:
            try:
                img = Image.new('RGB', (THUMB_SIZE, THUMB_SIZE), color=(20, 20, 20))
                buf = io.BytesIO()
                img.save(buf, format='JPEG', quality=70)
                data = buf.getvalue()
                if cache_path:
                    try:
                        cache_path.parent.mkdir(parents=True, exist_ok=True)
                        cache_path.write_bytes(data)
                    except Exception:
                        pass
                return base64.b64encode(data).decode('ascii')
            except Exception:
                pass

    return None


# ── IMAGE LIST ─────────────────────────────────────────────────────────────────

# Top-level subfolders never shown in the gallery
_EXCLUDED_DIRS = {PREVIOUS_SESSIONS_DIR, DUPLICATES_DIR, 'temp', '.git',
                  THUMB_CACHE_DIR}

def _is_image(f, folder):
    """Return True if f is an image we should show.
    Excludes previous_sessions/, duplicates/, temp/ and any hidden dirs."""
    if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
        return False
    try:
        parts = f.relative_to(folder).parts
        if parts and parts[0] in _EXCLUDED_DIRS:
            return False
        if any(p.startswith('.') for p in parts):
            return False
    except ValueError:
        return False
    return True

def get_images(folder):
    folder=Path(folder); images=[]
    for f in sorted(_fast_walk(folder)):
        if f.is_file() and _is_image(f, folder):
            images.append(str(f.relative_to(folder)))
    return images

def folder_hash(folder):
    """Lightweight fingerprint of the image list — name + mtime + size."""
    import hashlib
    folder=Path(folder); parts=[]
    for f in sorted(_fast_walk(folder)):
        if f.is_file() and _is_image(f, folder):
            st=f.stat()
            parts.append(f"{f.relative_to(folder)}:{st.st_mtime:.0f}:{st.st_size}")
    return hashlib.md5('\n'.join(parts).encode()).hexdigest()

# ── SIDECAR ────────────────────────────────────────────────────────────────────

SIDECAR_SUFFIX='.gallery.json'

def sidecar_path(image_path):
    return Path(str(image_path)+SIDECAR_SUFFIX)

def read_sidecar(image_path):
    sc=sidecar_path(image_path)
    if sc.exists():
        try: return json.loads(sc.read_text('utf-8'))
        except: pass
    return {'keywords':[],'category':'','notes':''}

def write_sidecar(image_path,data):
    sidecar_path(image_path).write_text(json.dumps(data,indent=2,ensure_ascii=False),'utf-8')

def all_tags_summary(folder):
    """Return {rel_path: tag_data} for all tagged images.
    Uses the cached image list when available (fast), falls back to disk walk.
    """
    folder = Path(folder)
    result = {}
    # Use cached image list if the watcher has populated it
    image_rels = list(_image_list) if _image_list else None
    if not image_rels:
        # Fallback: walk the folder
        image_rels = []
        for f in sorted(_fast_walk(folder)):
            if f.is_file() and _is_image(f, folder):
                image_rels.append(str(f.relative_to(folder)))
    for rel in image_rels:
        fp = folder / rel
        sc = sidecar_path(fp)
        if sc.exists():
            try:
                data = read_sidecar(fp)
                if data.get('keywords') or data.get('category') or data.get('notes'):
                    result[rel] = data
            except Exception:
                pass
    return result

# ── EXIF WRITE ─────────────────────────────────────────────────────────────────

def write_exif_tags(image_path,keywords,category,notes):
    if not HAS_PIL: return False,'Pillow not installed'
    if Path(image_path).suffix.lower() not in {'.jpg','.jpeg'}:
        return False,'EXIF write only supported for JPEG'
    try:
        with Image.open(image_path) as img:
            try: ed=piexif.load(img.info.get('exif',b''))
            except: ed={'0th':{},'Exif':{},'GPS':{},'1st':{}}
            ed['0th'][piexif.ImageIFD.XPKeywords]='\x00'.join(keywords).encode('utf-16-le')+b'\x00\x00'
            if notes: ed['0th'][piexif.ImageIFD.ImageDescription]=notes.encode('ascii','replace')
            if category: ed['0th'][piexif.ImageIFD.XPSubject]=category.encode('utf-16-le')+b'\x00\x00'
            eb=piexif.dump(ed)
        with Image.open(image_path) as img:
            img.save(image_path,'JPEG',exif=eb,quality=95,subsampling=0)
        return True,'EXIF written successfully'
    except Exception as e: return False,str(e)

# ── RENAME ─────────────────────────────────────────────────────────────────────

def sanitise_filename(name):
    import re
    name=re.sub(r'[/\\\x00]','_',name.strip())
    name=re.sub(r'\s+','_',name); name=re.sub(r'_+','_',name)
    name=re.sub(r'[<>:"|?*]','',name).lstrip('.-')
    return name or 'unnamed'

def auto_rename_suggestion(image_path):
    import re; parts=[]
    if HAS_PIL:
        try:
            with Image.open(image_path) as img:
                raw=img._getexif() if hasattr(img,'_getexif') else None
                ds=ms=None
                if raw:
                    for tid,val in raw.items():
                        t=ExifTags.TAGS.get(tid,'')
                        if t in ('DateTimeOriginal','DateTime') and not ds: ds=str(val)
                        if t=='Model' and not ms: ms=str(val).strip()
                if ds:
                    try:
                        dt=datetime.strptime(ds.strip(),'%Y:%m:%d %H:%M:%S')
                        parts.append(dt.strftime('%Y-%m-%d_%H%M%S'))
                    except: pass
                if ms:
                    safe=re.sub(r'[^a-zA-Z0-9]+','_',ms).strip('_')
                    if safe: parts.append(safe)
        except: pass
    if not parts:
        parts.append(datetime.fromtimestamp(Path(image_path).stat().st_mtime).strftime('%Y-%m-%d_%H%M%S'))
    return '_'.join(parts)

def rename_image(folder,old_rel,new_stem):
    folder=Path(folder); old_path=folder/old_rel
    if not old_path.exists(): return False,f'Not found: {old_rel}',None
    ext=old_path.suffix; safe=sanitise_filename(new_stem)
    if not safe: return False,'Invalid filename',None
    new_path=old_path.parent/(safe+ext); new_rel=str(new_path.relative_to(folder))
    if new_path.exists() and new_path!=old_path: return False,f'"{safe+ext}" already exists',None
    if new_path==old_path: return True,'Name unchanged',new_rel
    old_path.rename(new_path)
    osc=sidecar_path(old_path)
    if osc.exists():
        osc.rename(sidecar_path(new_path))
        print(f"  ↪  Sidecar: {osc.name} → {sidecar_path(new_path).name}")
    print(f"  ↪  Renamed: {old_rel} → {new_rel}")
    return True,f'Renamed to {safe+ext}',new_rel

# ── SESSION / ZIP ──────────────────────────────────────────────────────────────

class Session:
    def __init__(self,photos_dir,source_zip=None,session_dir=None):
        self.photos_dir  = Path(photos_dir)
        self.source_zip  = Path(source_zip) if source_zip else None
        self.session_dir = Path(session_dir) if session_dir else None
        self.started_at  = datetime.now()

    @property
    def is_zip_session(self): return self.session_dir is not None

    @property
    def serve_folder(self):
        return self.session_dir if self.is_zip_session else self.photos_dir

    @property
    def temp_dir(self): return self.session_dir   # backwards compat alias

    def zip_name(self):
        return f"photos-{self.started_at.strftime('%Y-%m-%d_%H%M%S')}.zip"


def find_existing_zip(folder):
    for f in sorted(Path(folder).glob('photos-*.zip')):
        if f.is_file(): return f
    return None


def _gallery_name(photos_dir: Path) -> str:
    """Return gallery name from gallery.json in parent dir, or parent dir name."""
    gallery_root = photos_dir.parent
    meta_file = gallery_root / 'gallery.json'
    if meta_file.exists():
        try:
            data = json.loads(meta_file.read_text())
            name = data.get('name', '').strip()
            if name:
                return name
        except Exception:
            pass
    return gallery_root.name


def _zip_mtime(zip_path):
    try:    return zip_path.stat().st_mtime
    except: return 0.0


def _session_marker(session_dir):
    return session_dir / '.extracted_from'


def extract_zip_to_session(zip_path, photos_dir):
    """
    Extract zip to a fixed folder alongside photos_dir:
      ~/ns/photos  ->  ~/ns/.gallery_session/

    If the session folder already contains files extracted from the same
    zip (same name + mtime), skip extraction and reuse as-is.
    Returns the session folder path.
    """
    session_dir = photos_dir.parent / SESSION_FOLDER
    marker      = _session_marker(session_dir)
    zip_mtime   = _zip_mtime(zip_path)
    marker_info = f"{zip_path.name}:{zip_mtime:.0f}"

    if session_dir.exists() and marker.exists():
        if marker.read_text().strip() == marker_info:
            count = sum(1 for f in _fast_walk(session_dir) if f.is_file()
                        and not f.name.startswith('.'))
            print(f"  OK  Reusing session folder: {session_dir}")
            print(f"      ({count} files already extracted from {zip_path.name})")
            return session_dir
        else:
            print(f"  >>  Zip changed - re-extracting...")
            shutil.rmtree(session_dir, ignore_errors=True)

    session_dir.mkdir(parents=True, exist_ok=True)
    print(f"  >>  Extracting {zip_path.name} -> {session_dir}")
    if HAS_PYZIPPER:
        with pyzipper.AESZipFile(zip_path, 'r') as zf:
            if GALLERY_PASSWORD:
                zf.setpassword(GALLERY_PASSWORD.encode())
            zf.extractall(session_dir)
    else:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(session_dir)
    count = sum(1 for f in _fast_walk(session_dir) if f.is_file())
    print(f"  OK  Extracted {count} files")
    marker.write_text(marker_info)
    return session_dir


def zip_session(session):
    """Zip serve_folder → photos_dir/photos-datetime.zip. Returns (ok,zip_path,msg)."""
    photos_dir=session.photos_dir; serve_dir=session.serve_folder
    zip_name=session.zip_name(); zip_path=photos_dir/zip_name

    # Safety net: move existing photos-*.zip to previous_sessions/
    prev_dir=photos_dir/PREVIOUS_SESSIONS_DIR; prev_dir.mkdir(exist_ok=True)
    for old in sorted(photos_dir.glob('photos-*.zip')):
        dest=prev_dir/old.name
        if dest.exists(): dest.unlink()
        shutil.move(str(old),str(dest))
        print(f"  🗄   Archived: {old.name} → previous_sessions/")

    cache_dir_name = THUMB_CACHE_DIR
    all_files=[f for f in _fast_walk(serve_dir) if f.is_file()
                and f.name != HASH_INDEX_FILENAME
                and not f.name.startswith('.')
                and cache_dir_name not in f.parts]
    if not all_files: return False,None,'No files to zip'

    encrypted = HAS_PYZIPPER and bool(GALLERY_PASSWORD)
    label = ' [AES-256]' if encrypted else ''
    print(f"  🗜   Zipping {len(all_files)} files → {zip_name}{label} …")
    try:
        if encrypted:
            with pyzipper.AESZipFile(zip_path, 'w',
                                     compression=pyzipper.ZIP_DEFLATED,
                                     encryption=pyzipper.WZ_AES) as zf:
                zf.setpassword(GALLERY_PASSWORD.encode())
                for f in all_files:
                    zf.write(f, str(f.relative_to(serve_dir)))
        else:
            with zipfile.ZipFile(zip_path,'w',zipfile.ZIP_DEFLATED,compresslevel=6) as zf:
                for f in all_files:
                    zf.write(f,str(f.relative_to(serve_dir)))
    except Exception as e: return False,None,f'Zip failed: {e}'

    # Verify
    try:
        if encrypted:
            with pyzipper.AESZipFile(zip_path, 'r') as zf:
                zf.setpassword(GALLERY_PASSWORD.encode())
                bad = zf.testzip()
                if bad: return False,zip_path,f'Corruption in: {bad}'
                zcount = len([n for n in zf.namelist() if not n.endswith('/')])
        else:
            with zipfile.ZipFile(zip_path,'r') as zf:
                bad=zf.testzip()
                if bad: return False,zip_path,f'Corruption in: {bad}'
                zcount=len([n for n in zf.namelist() if not n.endswith('/')])
    except Exception as e: return False,zip_path,f'Verify failed: {e}'

    if zcount!=len(all_files):
        return False,zip_path,f'Count mismatch: {len(all_files)} files vs {zcount} in zip'

    size_mb=zip_path.stat().st_size/(1024*1024)
    msg=f'Saved {zcount} files → {zip_name} ({size_mb:.1f} MB)'
    print(f"  ✔   {msg}")
    return True,zip_path,msg


def cleanup_temp(session):
    """Keep the fixed session folder — clears marker so next startup re-stamps."""
    if not session.is_zip_session or not session.session_dir:
        return
    marker = _session_marker(session.session_dir)
    marker.unlink(missing_ok=True)
    print(f"  📁  Session folder preserved: {session.session_dir}")


def perform_logout(session):
    ok,zip_path,msg=zip_session(session)
    if not ok: return {'ok':False,'message':msg,'zip_name':None}
    cleanup_temp(session)
    return {'ok':True,'message':msg,'zip_name':zip_path.name if zip_path else None}


def setup_session(photos_dir,explicit_zip=None):
    photos_dir=Path(photos_dir); photos_dir.mkdir(parents=True,exist_ok=True)
    zip_path=explicit_zip or find_existing_zip(photos_dir)
    if zip_path:
        if not Path(zip_path).exists():
            print(f"  Zip not found: {zip_path}"); sys.exit(1)
        session_dir = extract_zip_to_session(Path(zip_path), photos_dir)
        return Session(photos_dir=photos_dir, source_zip=zip_path, session_dir=session_dir)
    print(f"  Folder session: {photos_dir}")
    return Session(photos_dir=photos_dir)


# ── AUTO-RENAME SYSTEM ─────────────────────────────────────────────────────────
#
# Convention:  image-YYYY-MM-DD-HHMMSS.ext
#              image-YYYY-MM-DD-HHMMSS-N.ext   (N=2,3,… for collisions)
#
# A file is considered "already correctly named" if its stem matches the pattern.
# The watcher thread runs every 2 seconds, checks for non-conforming images,
# and renames them immediately (including any sidecar .gallery.json).

import re as _re

IMAGE_NAME_RE = _re.compile(
    r'^image-\d{4}-\d{2}-\d{2}-\d{6}(-\d+)?$'
)

def is_correctly_named(path: Path) -> bool:
    """Return True if the file already follows the image-DATE-TIME convention."""
    return bool(IMAGE_NAME_RE.match(path.stem))


def build_canonical_stem(image_path: Path, used_stems: set) -> str:
    """
    Generate image-YYYY-MM-DD-HHMMSS stem for image_path.
    If that stem is already in used_stems, append -2, -3, … until unique.
    Adds the chosen stem to used_stems.
    """
    # Try EXIF DateTimeOriginal first, then DateTime, then mtime
    dt = None
    if HAS_PIL:
        try:
            # Use load()+copy() to avoid lazy-loading issues with EXIF reads
            with Image.open(image_path) as src:
                src.load()
                raw = src._getexif() if hasattr(src, '_getexif') else None
                if raw:
                    for tag_id, val in raw.items():
                        tag = ExifTags.TAGS.get(tag_id, '')
                        if tag in ('DateTimeOriginal', 'DateTime') and val:
                            try:
                                dt = datetime.strptime(str(val).strip(), '%Y:%m:%d %H:%M:%S')
                                break
                            except ValueError:
                                pass
        except Exception:
            pass

    if dt is None:
        dt = datetime.fromtimestamp(image_path.stat().st_mtime)

    base = dt.strftime('image-%Y-%m-%d-%H%M%S')

    # Ensure uniqueness within the current batch
    stem = base
    counter = 2
    while stem in used_stems:
        stem = f"{base}-{counter}"
        counter += 1

    used_stems.add(stem)
    return stem


def auto_rename_new_images(folder: Path) -> list:
    """
    Scan folder for images that don't follow the naming convention and rename them.
    Returns list of (old_rel, new_rel) tuples for everything that was renamed.
    Thread-safe via a module-level lock.
    """
    renamed = []
    folder  = Path(folder)

    with _auto_rename_lock:
        # Collect all current correctly-named stems to avoid collisions
        used_stems = set()
        for f in _fast_walk(folder):
            if f.is_file() and _is_image(f, folder) and is_correctly_named(f):
                used_stems.add(f.stem)

        # Find images that need renaming
        to_rename = []
        for f in sorted(_fast_walk(folder)):
            if f.is_file() and _is_image(f, folder) and not is_correctly_named(f):
                to_rename.append(f)

        if not to_rename:
            print(f"  🏷   Auto-rename: nothing to rename (folder has {len(used_stems)} correctly-named images)", flush=True)
            return []

        print(f"  🏷   Auto-rename: {len(to_rename)} image(s) need renaming", flush=True)
        for f in to_rename:
            print(f"       → will rename: {f.name}", flush=True)

        for old_path in to_rename:
            # Re-check it still exists (another iteration may have moved it)
            if not old_path.exists():
                continue

            new_stem = build_canonical_stem(old_path, used_stems)
            new_name = new_stem + old_path.suffix.lower()   # normalise ext case too
            new_path = old_path.parent / new_name

            # Handle the extremely unlikely case it already exists on disk
            if new_path.exists() and new_path != old_path:
                # Force a unique counter
                c = 2
                while new_path.exists():
                    new_stem_c = f"{new_stem}-{c}"
                    new_path   = old_path.parent / (new_stem_c + old_path.suffix.lower())
                    c += 1
                used_stems.add(new_path.stem)

            try:
                old_rel = str(old_path.relative_to(folder))
                new_rel = str(new_path.relative_to(folder))

                old_path.rename(new_path)

                # Rename sidecar if it exists
                old_sc = sidecar_path(old_path)
                if old_sc.exists():
                    new_sc = sidecar_path(new_path)
                    old_sc.rename(new_sc)
                    print(f"  ↪  Sidecar: {old_sc.name} → {new_sc.name}")

                print(f"  ↪  {old_rel} → {new_rel}")
                renamed.append((old_rel, new_rel))

                # Mark as new — but only after startup scan completes,
                # so images that existed before the server started aren't flagged.
                if _startup_complete:
                    _new_files.add(new_rel)
                    print(f"  🆕  Marked as new: {new_rel}")

            except Exception as e:
                print(f"  ✘  Auto-rename failed for {old_path.name}: {e}", file=sys.stderr)

    return renamed


_auto_rename_lock = threading.Lock()

# ── SESSION STATE (maintained by background thread, read by HTTP handlers) ─────

_new_files:      set = set()   # rel paths marked new this session (never persisted)
_startup_complete: bool = False
_current_hash:   str  = ''     # folder hash — updated by watcher, read by /api/images/watch
_current_count:  int  = 0      # image count — updated by watcher
_dup_log:        list = []     # duplicate moves this session (in-memory only)

# ── JSON HASH INDEX ────────────────────────────────────────────────────────────
#
# Stored as  <folder>/photo-hashes.json
# Schema:
#   {
#     "image-2024-03-15-143022.jpg": {
#       "sha256":     "a3f4...",
#       "phash":      "f8c0a3...",   # null if imagehash not installed
#       "size":       3145728,
#       "mtime":      1710506422.0,
#       "indexed_at": "2024-03-15 14:30:22"
#     },
#     ...
#   }
#
# Lookup is O(1) by filename.  Duplicate detection uses two reverse maps built
# in memory from the JSON — these are cheap because we only build them once
# per watcher tick (not per HTTP request).

HASH_INDEX_FILENAME = 'photo-hashes.json'
import hashlib as _hashlib


def hash_index_path(folder: Path) -> Path:
    # Always store in the permanent photos_dir, even in zip sessions.
    # This avoids re-hashing 444 images every time a new session starts.
    if SESSION and SESSION.is_zip_session:
        return SESSION.photos_dir / HASH_INDEX_FILENAME
    return folder / HASH_INDEX_FILENAME


def load_hash_index(folder: Path) -> dict:
    """Load index from JSON, return {} if missing or corrupt."""
    p = hash_index_path(folder)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text('utf-8'))
    except Exception as e:
        print(f"  ⚠   Could not read hash index: {e}", file=sys.stderr)
        return {}


def save_hash_index(folder: Path, index: dict):
    """Write index atomically (write temp → rename)."""
    p    = hash_index_path(folder)
    tmp  = p.with_suffix('.tmp')
    try:
        tmp.write_text(json.dumps(index, indent=2, ensure_ascii=False), 'utf-8')
        tmp.replace(p)
    except Exception as e:
        print(f"  ✘  Could not save hash index: {e}", file=sys.stderr)
        tmp.unlink(missing_ok=True)


def sha256_file(path: Path) -> str:
    h = _hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def phash_file(path: Path):
    """Perceptual hash string or None.  Thumbnails first for speed."""
    if not HAS_PIL or not HAS_IMAGEHASH:
        return None
    try:
        with Image.open(path) as img:
            img.thumbnail((256, 256), Image.LANCZOS)
            if img.mode not in ('RGB', 'L'):
                img = img.convert('RGB')
            return str(imagehash.phash(img))
    except Exception:
        return None


def phash_distance(h1: str, h2: str) -> int:
    try:
        return imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2)
    except Exception:
        return 999


def is_entry_stale(entry: dict, path: Path) -> bool:
    """Return True if file has changed since it was indexed (size or mtime changed)."""
    try:
        st = path.stat()
        return (st.st_size != entry.get('size') or
                abs(st.st_mtime - entry.get('mtime', 0)) > 1.0)
    except FileNotFoundError:
        return True


def move_to_duplicates(folder: Path, dup_path: Path, kept_rel: str, reason: str):
    """Move dup_path into folder/duplicates/ and append to _dup_log."""
    dup_dir = dup_folder(folder)
    dup_dir.mkdir(parents=True, exist_ok=True)
    dest = dup_dir / dup_path.name
    if dest.exists():
        stem, ext = dup_path.stem, dup_path.suffix
        c = 2
        while dest.exists():
            dest = dup_dir / f"{stem}-dup{c}{ext}"
            c += 1
    try:
        shutil.move(str(dup_path), str(dest))
        sc = sidecar_path(dup_path)
        if sc.exists():
            shutil.move(str(sc), str(dup_dir / sc.name))
        dup_rel = str(dup_path.relative_to(folder))
        entry = {
            'moved':     dup_rel,
            'dest':      str(dest.relative_to(folder)),
            'kept':      kept_rel,
            'reason':    reason,
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        _dup_log.append(entry)
        print(f"  🗂   Dup moved: {dup_rel} → duplicates/  [{reason}]")
        return entry
    except Exception as e:
        print(f"  ✘  Move failed {dup_path.name}: {e}", file=sys.stderr)
        return None


def rebuild_hash_index(folder: Path) -> dict:
    """
    Called once at startup.
    Loads existing JSON, drops entries whose files are gone or stale,
    hashes any images not yet in the index, saves and returns the index.
    """
    import time as _t
    folder = Path(folder)
    index  = load_hash_index(folder)

    # Purge excluded dirs and missing/stale entries from index
    for rel in list(index.keys()):
        parts = Path(rel).parts
        # Remove entries from excluded dirs (duplicates/, temp/, etc.)
        if parts and (parts[0] in _EXCLUDED_DIRS or parts[0].startswith('.')):
            del index[rel]
            continue
        fp = folder / rel
        if not fp.exists() or is_entry_stale(index[rel], fp):
            del index[rel]

    # Find images not yet indexed
    to_hash = []
    for f in sorted(_fast_walk(folder)):
        if f.is_file() and _is_image(f, folder):
            rel = str(f.relative_to(folder))
            if rel not in index:
                to_hash.append((f, rel))

    if to_hash:
        print(f"  🔢  Hashing {len(to_hash)} image(s) for the index…")
        for f, rel in to_hash:
            if not f.exists():
                continue
            try:
                st    = f.stat()
                sha   = sha256_file(f)
                ph    = phash_file(f)
                index[rel] = {
                    'sha256':     sha,
                    'phash':      ph,
                    'size':       st.st_size,
                    'mtime':      st.st_mtime,
                    'indexed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                }
                _t.sleep(0.005)   # tiny yield between files
            except Exception as e:
                print(f"  ✘  Hash error {f.name}: {e}", file=sys.stderr)
        save_hash_index(folder, index)
        print(f"  ✔   Hash index saved ({len(index)} entries)")
    else:
        print(f"  ✔   Hash index loaded ({len(index)} entries, nothing new to hash)")

    return index


def scan_for_duplicates(folder: Path, index: dict) -> tuple:
    """
    Check all images against the index for duplicates.
    Returns (updated_index, found_any_new_dups).
    Only called from the background watcher thread.
    """
    import time as _t
    folder   = Path(folder)
    changed  = False

    # Build reverse lookups — skip any excluded-dir entries that slipped through
    sha_to_rel:   dict = {}
    phash_to_rel: dict = {}
    for rel, entry in index.items():
        parts = Path(rel).parts
        if parts and (parts[0] in _EXCLUDED_DIRS or parts[0].startswith('.')):
            continue   # never treat a duplicates/ file as the "kept" original
        sha_to_rel[entry['sha256']] = rel
        if entry.get('phash'):
            phash_to_rel[entry['phash']] = rel

    # Find images not yet in the index (newly added since last tick)
    to_check = []
    for f in sorted(_fast_walk(folder)):
        if f.is_file() and _is_image(f, folder):
            rel = str(f.relative_to(folder))
            if rel not in index:
                to_check.append((f, rel))

    for f, rel in to_check:
        if not f.exists():
            continue
        try:
            st  = f.stat()
            sha = sha256_file(f)
            ph  = phash_file(f)
            _t.sleep(0.005)
        except Exception as e:
            print(f"  ✘  Hash error {f.name}: {e}", file=sys.stderr)
            continue

        # Exact duplicate?
        if sha in sha_to_rel:
            existing_rel = sha_to_rel[sha]
            move_to_duplicates(folder, f, existing_rel, 'exact duplicate (SHA-256)')
            changed = True
            continue

        # Perceptual duplicate?
        dup_found = False
        if ph and HAS_IMAGEHASH:
            for existing_ph, existing_rel in list(phash_to_rel.items()):
                dist = phash_distance(ph, existing_ph)
                if dist <= PHASH_THRESHOLD:
                    existing_path = folder / existing_rel
                    if not existing_path.exists():
                        continue
                    # Keep the larger (higher quality) file
                    keep_new = f.exists() and st.st_size > existing_path.stat().st_size
                    if keep_new:
                        move_to_duplicates(
                            folder, existing_path, rel,
                            f'perceptual duplicate (pHash distance={dist}, kept larger)')
                        # Remove old entry — new file will be indexed below
                        index.pop(existing_rel, None)
                        sha_to_rel  = {v: k for k, v in
                                       {r: e['sha256'] for r, e in index.items()}.items()}
                        phash_to_rel.pop(existing_ph, None)
                    else:
                        move_to_duplicates(
                            folder, f, existing_rel,
                            f'perceptual duplicate (pHash distance={dist}, kept larger)')
                        dup_found = True
                    changed = True
                    break

        if dup_found:
            continue

        # Not a duplicate — add to index
        index[rel] = {
            'sha256':     sha,
            'phash':      ph,
            'size':       st.st_size,
            'mtime':      st.st_mtime,
            'indexed_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        }
        sha_to_rel[sha] = rel
        if ph:
            phash_to_rel[ph] = rel
        changed = True

    if changed:
        save_hash_index(folder, index)

    return index, changed


# ── BACKGROUND WATCHER ────────────────────────────────────────────────────────
#
# Uses watchdog (OS inotify on Linux) so we get instant file-change events
# with zero polling overhead. The HTTP handlers only read pre-computed state.

try:
    from watchdog.observers import Observer
    from watchdog.events import FileSystemEventHandler
    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False
    print("Warning: watchdog not installed — falling back to 2s polling. "
          "pip install watchdog")

# Shared image list — maintained by watcher, read by HTTP handlers
_image_list: list = []
_image_list_lock = threading.Lock()


def _refresh_image_list(folder: Path):
    """Recompute image list and hash, update shared state. Called by watcher."""
    global _current_hash, _current_count
    images = get_images(folder)
    h      = folder_hash(folder)
    with _image_list_lock:
        _image_list.clear()
        _image_list.extend(images)
    _current_count = len(images)
    _current_hash  = h



def _move_drop_files(photos_dir: Path, serve_folder: Path):
    """
    Move any image files sitting in photos_dir into serve_folder.
    This is the 'drop folder' — users copy images here and they
    appear in the gallery automatically.
    Skips zips, the hash index, and any non-image files.
    """
    if photos_dir == serve_folder:
        return 0   # folder session — already serving from photos_dir
    # Check what's in the drop folder
    try:
        all_files = list(photos_dir.iterdir())
        img_files = [f for f in all_files if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS]
        if img_files:
            print(f"  📥  Drop folder has {len(img_files)} image(s): {[f.name for f in img_files[:3]]}", flush=True)
    except Exception as e:
        print(f"  ✘  Cannot read drop folder {photos_dir}: {e}", flush=True)
        return 0
    moved = 0
    for f in sorted(photos_dir.iterdir()):
        if not f.is_file():
            continue
        if f.suffix.lower() not in SUPPORTED_EXTENSIONS:
            continue
        dest = serve_folder / f.name
        # Avoid overwriting — let auto-rename handle name conflicts later
        if dest.exists():
            stem, ext = f.stem, f.suffix
            c = 2
            while dest.exists():
                dest = serve_folder / f"{stem}-drop{c}{ext}"
                c += 1
        try:
            shutil.move(str(f), str(dest))
            print(f"  📥  Drop: {f.name} → .gallery_session/", flush=True)
            moved += 1
        except Exception as e:
            print(f"  ✘  Drop move failed {f.name}: {e}", file=sys.stderr)
    return moved


def start_watcher(folder: Path, interval: float = 2.0):
    """
    Start the background watcher.
    Uses watchdog (inotify) when available, falls back to polling.
    Either way the HTTP handlers never do file I/O — they read cached state.
    """
    folder = Path(folder)

    def _startup_and_loop(change_event: threading.Event):
        global _startup_complete
        import time

        # Move any images sitting in photos/ into the session folder first
        try:
            photos_dir = SESSION.photos_dir if SESSION else folder
            dropped = _move_drop_files(photos_dir, folder)
            if dropped:
                print(f"  📥  Moved {dropped} file(s) from drop folder at startup", flush=True)
                time.sleep(0.3)
        except Exception as e:
            print(f"  ✘  Drop folder startup: {e}", file=sys.stderr)

        # Rename anything in session folder that isn't correctly named
        try:
            auto_rename_new_images(folder)
        except Exception as e:
            print(f"  ✘  Auto-rename at startup: {e}", file=sys.stderr)

        print(f"  🔢  Building hash index…", flush=True)
        index = rebuild_hash_index(folder)

        n_before = len(_dup_log)
        index, _ = scan_for_duplicates(folder, index)
        n_found  = len(_dup_log) - n_before
        if n_found:
            print(f"  🗂   {n_found} duplicate(s) moved at startup")

        _refresh_image_list(folder)
        _startup_complete = True
        print(f"  ✔   Ready — {_current_count} images, {len(index)} hashed, {n_found} dup(s)", flush=True)

        # ── Event loop ───────────────────────────────────────────────────────
        while True:
            # Wait for a filesystem change (or timeout for safety net)
            triggered = change_event.wait(timeout=30.0)
            change_event.clear()

            if not triggered:
                # Periodic safety net — check drop folder and refresh
                try:
                    photos_dir = SESSION.photos_dir if SESSION else folder
                    n = _move_drop_files(photos_dir, folder)
                    if n:
                        time.sleep(0.3)
                    auto_rename_new_images(folder)
                except Exception:
                    pass
                _refresh_image_list(folder)
                continue

            # Give the OS a moment to finish writing (e.g. large file copy)
            time.sleep(0.3)

            # Check drop folder (photos_dir) for new files and move them in
            try:
                photos_dir = SESSION.photos_dir if SESSION else folder
                n_dropped = _move_drop_files(photos_dir, folder)
                if n_dropped:
                    # Give the OS a moment to finish the move before renaming
                    time.sleep(0.5)
            except Exception as e:
                print(f"  ✘  Drop folder: {e}", file=sys.stderr)

            try:
                auto_rename_new_images(folder)
            except Exception as e:
                print(f"  ✘  Auto-rename: {e}", file=sys.stderr)

            try:
                n_before = len(_dup_log)
                index, _ = scan_for_duplicates(folder, index)
                n_found  = len(_dup_log) - n_before
                if n_found:
                    print(f"  🗂   {n_found} new duplicate(s) moved")
            except Exception as e:
                print(f"  ✘  Dedup: {e}", file=sys.stderr)

            _refresh_image_list(folder)

    # Create a threading.Event that fires on filesystem changes
    change_event = threading.Event()

    if HAS_WATCHDOG:
        class _Handler(FileSystemEventHandler):
            def on_any_event(self, event):
                src = getattr(event, 'src_path', '') or ''
                # Ignore hash index saves (prevent feedback loops)
                if HASH_INDEX_FILENAME in src:
                    return
                # Ignore thumbnail cache changes
                if THUMB_CACHE_DIR in src:
                    return
                # Ignore sidecar files (written by the server itself)
                if src.endswith(SIDECAR_SUFFIX):
                    return
                print(f"  👁   Event: {event.event_type} {Path(src).name}", flush=True)
                change_event.set()

        observer = Observer()
        observer.schedule(_Handler(), str(folder), recursive=True)
        # Also watch photos_dir (the drop folder) for new arrivals
        photos_dir = SESSION.photos_dir if SESSION else folder
        if photos_dir != folder:
            observer.schedule(_Handler(), str(photos_dir), recursive=False)
            print(f"  👁   Watching {folder} + {photos_dir} (inotify)")
        else:
            print(f"  👁   Watching {folder} (inotify)")
        observer.daemon = True
        observer.start()
    else:
        # Fallback: pulse the event on a timer
        def _pulse():
            import time
            while True:
                time.sleep(interval)
                change_event.set()
        threading.Thread(target=_pulse, daemon=True, name="poll-pulse").start()
        print(f"  🔄  Polling {folder} every {interval}s")

    t = threading.Thread(
        target=_startup_and_loop,
        args=(change_event,),
        daemon=True,
        name="gallery-watcher",
    )
    t.start()
    return t


# ── HTTP HANDLER ───────────────────────────────────────────────────────────────

SESSION=None
_server_ref=None
MINIMAL_MODE=False   # set True via --minimal flag; disables watcher, auto-rename, dedup

def permanent_folder() -> Path:
    """
    Returns the stable folder to use for persistent data (thumb cache, hash index).
    For zip sessions this is .gallery_session/ (fixed, reused).
    For folder sessions this is the photos folder itself.
    """
    if SESSION:
        return SESSION.serve_folder   # .gallery_session/ or photos/ — both stable
    return Path('.')

class GalleryHandler(BaseHTTPRequestHandler):

    def log_message(self,format,*args):
        if args and len(args)>=2:
            code=str(args[1]); req=str(args[0])
            if code not in ('200','304') or 'POST' in req:
                print(f"  [{code}] {req}",file=sys.stderr)

    def send_json(self,data,status=200):
        body=json.dumps(data).encode()
        self.send_response(status)
        self.send_header('Content-Type','application/json')
        self.send_header('Content-Length',len(body))
        self.send_header('Access-Control-Allow-Origin','*')
        self.end_headers(); self.wfile.write(body)

    def send_file(self, filepath, content_type):
        """Serve a file, supporting HTTP Range requests for video streaming."""
        file_size = filepath.stat().st_size
        range_header = self.headers.get('Range')

        if range_header:
            # Parse Range: bytes=start-end
            try:
                byte_range = range_header.strip().replace('bytes=', '')
                start_str, end_str = byte_range.split('-')
                start = int(start_str) if start_str else 0
                end   = int(end_str)   if end_str   else file_size - 1
                end   = min(end, file_size - 1)
                length = end - start + 1
                self.send_response(206)
                self.send_header('Content-Type', content_type)
                self.send_header('Content-Range', f'bytes {start}-{end}/{file_size}')
                self.send_header('Content-Length', str(length))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                with open(filepath, 'rb') as f:
                    f.seek(start)
                    self.wfile.write(f.read(length))
            except Exception:
                self.send_response(416)
                self.end_headers()
        else:
            with open(filepath, 'rb') as f:
                data = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Accept-Ranges', 'bytes')
            self.send_header('Cache-Control', 'public, max-age=3600')
            self.end_headers()
            self.wfile.write(data)

    @property
    def folder(self): return SESSION.serve_folder

    def do_OPTIONS(self):
        self.send_response(200)
        self.send_header('Access-Control-Allow-Origin','*')
        self.send_header('Access-Control-Allow-Methods','GET, POST, OPTIONS')
        self.send_header('Access-Control-Allow-Headers','Content-Type')
        self.end_headers()

    def do_GET(self):
        parsed=urlparse(self.path); path=parsed.path; qs=parse_qs(parsed.query)

        if path=='/':
            self.send_file(Path(__file__).parent/'gallery.html','text/html; charset=utf-8')



        elif path=='/api/images':
            if MINIMAL_MODE:
                images = get_images(self.folder)
            else:
                with _image_list_lock:
                    images = list(_image_list)
                # If watcher hasn't populated the list yet, read from disk directly
                if not images and not _startup_complete:
                    images = get_images(self.folder)
            self.send_json({'images':images,'count':len(images),
                            'hash':_current_hash or 'loading',
                            'new_files':list(_new_files),
                            'ready': _startup_complete})

        elif path=='/api/new':
            self.send_json({'new_files':list(_new_files),'count':len(_new_files)})

        elif path=='/api/new/clear':
            _new_files.clear()
            self.send_json({'ok':True})

        elif path=='/api/duplicates':
            # Return dup log + current files in duplicates/ folder
            dup_dir = dup_folder(self.folder)
            on_disk = []
            if dup_dir.exists():
                for f in sorted(_fast_walk(dup_dir)):
                    if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS:
                        on_disk.append({
                            'rel':  DUPLICATES_DIR + '/' + f.name,
                            'name': f.name,
                            'size': f.stat().st_size,
                        })
            self.send_json({
                'log':      _dup_log,
                'count':    len(_dup_log),
                'on_disk':  on_disk,
                'disk_count': len(on_disk),
            })

        elif path=='/api/duplicates/clear-log':
            # Clear just the in-memory log (files stay in duplicates/)
            _dup_log.clear()
            self.send_json({'ok':True})

        elif path=='/api/images/watch':
            if MINIMAL_MODE:
                images = get_images(self.folder)
                h      = folder_hash(self.folder)
                self.send_json({'hash':h,'count':len(images),'dup_count':0,'ready':True})
            else:
                self.send_json({'hash':_current_hash,
                                'count':_current_count,
                                'dup_count':len(_dup_log),
                                'ready':_startup_complete})

        elif path=='/api/debug/scan':
            # Full scan report — all files found, what's included/excluded and why
            import os
            folder = self.folder
            report = {'folder': str(folder), 'entries': []}
            all_files = []
            for f in sorted(_fast_walk(folder)):
                if f.is_file():
                    rel  = str(f.relative_to(folder))
                    ext  = f.suffix
                    extl = ext.lower()
                    inc  = extl in SUPPORTED_EXTENSIONS
                    all_files.append({
                        'path':    rel,
                        'ext':     ext,
                        'ext_lower': extl,
                        'included': inc,
                        'reason':  'OK' if inc else f'extension "{ext}" not in supported list',
                        'size':    f.stat().st_size,
                    })
            report['entries']       = all_files
            report['total_files']   = len(all_files)
            report['included']      = sum(1 for e in all_files if e['included'])
            report['excluded']      = sum(1 for e in all_files if not e['included'])
            report['supported_ext'] = sorted(SUPPORTED_EXTENSIONS)
            self.send_json(report)

        elif path=='/api/session':
            self.send_json({
                'is_zip_session':SESSION.is_zip_session,
                'source_zip':SESSION.source_zip.name if SESSION.source_zip else None,
                'started_at':SESSION.started_at.strftime('%Y-%m-%d %H:%M:%S'),
                'photos_dir':str(SESSION.photos_dir),
                'gallery_name':_gallery_name(SESSION.photos_dir),
            })

        elif path=='/api/metadata':
            name=unquote(qs.get('file',[''])[0])
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            if not fp.exists() or fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                self.send_json({'error':'Not found'},404); return
            self.send_json(extract_metadata(fp))

        elif path=='/api/thumbnail':
            name=unquote(qs.get('file',[''])[0])
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            parts = Path(name).parts
            if parts and parts[0] in (_EXCLUDED_DIRS - {DUPLICATES_DIR}):
                self.send_json({'error':'Excluded'},403); return
            if parts and parts[0] == DUPLICATES_DIR:
                fp = dup_folder(self.folder) / Path(name).name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            if fp.suffix.lower() in VIDEO_EXTENSIONS:
                thumb = make_video_thumbnail(fp, photos_folder=permanent_folder(), image_rel=name)
            else:
                thumb = make_thumbnail(fp, photos_folder=permanent_folder(), image_rel=name)
            self.send_json({'thumbnail': thumb, 'is_video': fp.suffix.lower() in VIDEO_EXTENSIONS})

        elif path=='/api/image':
            name=unquote(qs.get('file',[''])[0])
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            # Allow images from duplicates/ for the review panel
            parts = Path(name).parts
            if parts and parts[0] in (_EXCLUDED_DIRS - {DUPLICATES_DIR}):
                self.send_json({'error':'Excluded'},403); return
            # Duplicates are outside the photos folder — resolve separately
            if parts and parts[0] == DUPLICATES_DIR:
                fp = dup_folder(self.folder) / Path(name).name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            mime,_=mimetypes.guess_type(str(fp))
            self.send_file(fp,mime or 'image/jpeg')

        elif path=='/api/tags':
            name=unquote(qs.get('file',[''])[0])
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            self.send_json(read_sidecar(fp))

        elif path=='/api/tags/all':
            self.send_json(all_tags_summary(self.folder))

        elif path=='/api/rename/suggest':
            name=unquote(qs.get('file',[''])[0])
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            self.send_json({'suggestion':auto_rename_suggestion(fp),'current_stem':fp.stem})

        elif path=='/api/duplicates/restore':
            # Move file from duplicates/ back to its original location
            dest_rel = payload.get('dest','')      # e.g. "duplicates/image-xxx.jpg"
            target_rel = payload.get('target','')  # e.g. "image-yyy.jpg" (kept file's folder)
            if not dest_rel:
                self.send_json({'error':'dest required'},400); return
            # dest_rel is "duplicates/filename.jpg" — resolve against dup_folder
            fname = Path(dest_rel).name
            src = dup_folder(self.folder) / fname
            if not src.exists():
                self.send_json({'error':f'Not found: {dest_rel}'},404); return
            # Restore to same directory as the kept file, or root if not specified
            if target_rel:
                restore_dir = (self.folder / target_rel).parent
            else:
                restore_dir = self.folder
            restore_dir.mkdir(parents=True, exist_ok=True)
            dest_path = restore_dir / src.name
            if dest_path.exists():
                self.send_json({'error':f'{src.name} already exists in target folder'},409); return
            shutil.move(str(src), str(dest_path))
            # Move sidecar too if present
            sc = sidecar_path(src)
            if sc.exists():
                shutil.move(str(sc), str(restore_dir / sc.name))
            new_rel = str(dest_path.relative_to(self.folder))
            print(f"  ↩   Restored: {dest_rel} → {new_rel}")
            # Update dup log entry
            for entry in _dup_log:
                if entry.get('dest') == dest_rel:
                    entry['restored'] = True
                    break
            self.send_json({'ok':True,'new_rel':new_rel})

        elif path=='/api/duplicates/delete':
            # Permanently delete a file from duplicates/
            dest_rel = payload.get('dest','')
            if not dest_rel:
                self.send_json({'error':'dest required'},400); return
            fname  = Path(dest_rel).name
            target = dup_folder(self.folder) / fname
            if not target.exists():
                self.send_json({'error':'Not found'},404); return
            # Only allow deleting from within duplicates/ folder for safety
            try:
                target.relative_to(dup_folder(self.folder))
            except ValueError:
                self.send_json({'error':'Can only delete from duplicates/ folder'},403); return
            target.unlink()
            sc = sidecar_path(target)
            if sc.exists(): sc.unlink()
            print(f"  🗑   Deleted: {dest_rel}")
            for entry in _dup_log:
                if entry.get('dest') == dest_rel:
                    entry['deleted'] = True
                    break
            self.send_json({'ok':True})

        else:
            self.send_response(404); self.end_headers()

    def do_POST(self):
        try: self._post()
        except Exception as e:
            print(f"  ✘ POST {self.path}: {e}",file=sys.stderr)
            traceback.print_exc(file=sys.stderr)
            try: self.send_json({'error':str(e)},500)
            except: pass

    def _post(self):
        path=urlparse(self.path).path
        length=int(self.headers.get('Content-Length',0))
        try: payload=json.loads(self.rfile.read(length))
        except: self.send_json({'error':'Invalid JSON'},400); return

        if path=='/api/tags':
            name=payload.get('file','')
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            data={
                'keywords':[str(k).strip() for k in payload.get('keywords',[]) if str(k).strip()],
                'category':str(payload.get('category','')).strip(),
                'notes':str(payload.get('notes','')).strip(),
            }
            write_sidecar(fp,data)
            print(f"  ✎  Tags: {name}")
            self.send_json({'ok':True,'saved':data})

        elif path=='/api/tags/export':
            name=payload.get('file','')
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            if not fp.exists(): self.send_json({'error':'Not found'},404); return
            d=read_sidecar(fp)
            ok,msg=write_exif_tags(fp,d.get('keywords',[]),d.get('category',''),d.get('notes',''))
            self.send_json({'ok':ok,'message':msg})

        elif path=='/api/rename':
            name=payload.get('file',''); stem=payload.get('new_stem','').strip()
            if not name or not stem: self.send_json({'error':'file and new_stem required'},400); return
            ok,msg,new=rename_image(self.folder,name,stem)
            if ok and name in _new_files:
                _new_files.discard(name)
                if new: _new_files.add(new)
            self.send_json({'ok':ok,'message':msg,'new_name':new})

        elif path=='/api/delete':
            name=payload.get('file','')
            if not name: self.send_json({'error':'No file'},400); return
            fp=self.folder/name
            print(f"  🗑   Delete request: {name} → {fp} exists={fp.exists()}", flush=True)
            if not fp.exists():
                self.send_json({'error':f'Not found: {fp}'},404); return
            if fp.suffix.lower() not in SUPPORTED_EXTENSIONS:
                self.send_json({'error':f'Not an image: {fp.suffix}'},403); return
            try:
                fp.unlink()
                sc=sidecar_path(fp)
                if sc.exists(): sc.unlink()
                try:
                    cp=_thumb_cache_path(permanent_folder(),name)
                    if cp.exists(): cp.unlink()
                except Exception:
                    pass   # cache cleanup is best-effort
                _new_files.discard(name)
                print(f"  🗑   Deleted OK: {name}", flush=True)
                self.send_json({'ok':True})
            except Exception as e:
                import traceback
                print(f"  ✘  Delete error: {e}", flush=True)
                traceback.print_exc()
                self.send_json({'error':str(e)},500)

        elif path=='/api/rename-all':
            # Force rename all non-conforming images immediately
            try:
                renamed = auto_rename_new_images(self.folder)
                # Refresh image list after renames
                _refresh_image_list(self.folder)
                self.send_json({
                    'ok': True,
                    'renamed': len(renamed),
                    'pairs': [(o, n) for o, n in renamed]
                })
            except Exception as e:
                self.send_json({'ok': False, 'error': str(e)})

        elif path=='/api/logout':
            def _do():
                import time; time.sleep(0.4)
                print("\n  🔒  Logout — zipping session…")
                result=perform_logout(SESSION)
                if result['ok']: print(f"  ✔   Saved: {result['zip_name']}")
                else: print(f"  ✘   Failed: {result['message']}")
                if _server_ref: _server_ref.shutdown()
            threading.Thread(target=_do,daemon=True).start()
            self.send_json({'ok':True,'message':'Zipping session — server will stop shortly…'})

        elif path=='/api/duplicates/restore':
            # Move file from duplicates/ back to its original location
            dest_rel = payload.get('dest','')      # e.g. "duplicates/image-xxx.jpg"
            target_rel = payload.get('target','')  # e.g. "image-yyy.jpg" (kept file's folder)
            if not dest_rel:
                self.send_json({'error':'dest required'},400); return
            # dest_rel is "duplicates/filename.jpg" — resolve against dup_folder
            fname = Path(dest_rel).name
            src = dup_folder(self.folder) / fname
            if not src.exists():
                self.send_json({'error':f'Not found: {dest_rel}'},404); return
            # Restore to same directory as the kept file, or root if not specified
            if target_rel:
                restore_dir = (self.folder / target_rel).parent
            else:
                restore_dir = self.folder
            restore_dir.mkdir(parents=True, exist_ok=True)
            dest_path = restore_dir / src.name
            if dest_path.exists():
                self.send_json({'error':f'{src.name} already exists in target folder'},409); return
            shutil.move(str(src), str(dest_path))
            # Move sidecar too if present
            sc = sidecar_path(src)
            if sc.exists():
                shutil.move(str(sc), str(restore_dir / sc.name))
            new_rel = str(dest_path.relative_to(self.folder))
            print(f"  ↩   Restored: {dest_rel} → {new_rel}")
            # Update dup log entry
            for entry in _dup_log:
                if entry.get('dest') == dest_rel:
                    entry['restored'] = True
                    break
            self.send_json({'ok':True,'new_rel':new_rel})

        elif path=='/api/duplicates/delete':
            # Permanently delete a file from duplicates/
            dest_rel = payload.get('dest','')
            if not dest_rel:
                self.send_json({'error':'dest required'},400); return
            fname  = Path(dest_rel).name
            target = dup_folder(self.folder) / fname
            if not target.exists():
                self.send_json({'error':'Not found'},404); return
            # Only allow deleting from within duplicates/ folder for safety
            try:
                target.relative_to(dup_folder(self.folder))
            except ValueError:
                self.send_json({'error':'Can only delete from duplicates/ folder'},403); return
            target.unlink()
            sc = sidecar_path(target)
            if sc.exists(): sc.unlink()
            print(f"  🗑   Deleted: {dest_rel}")
            for entry in _dup_log:
                if entry.get('dest') == dest_rel:
                    entry['deleted'] = True
                    break
            self.send_json({'ok':True})

        else:
            self.send_response(404); self.end_headers()

# ── STARTUP / MAIN ─────────────────────────────────────────────────────────────

def handle_ctrl_c(session,server):
    print("\n\n  ⚠   Server interrupted.")
    if session.is_zip_session:
        print(f"  📦  Active zip session from: {session.source_zip.name}")
    else:
        print(f"  📁  Active folder session: {session.photos_dir}")
    try:    answer=input("  Zip and save before exit? [y/N] ").strip().lower()
    except: answer='n'

    if answer in ('y','yes'):
        print("  🗜   Saving session…")
        result=perform_logout(session)
        if result['ok']: print(f"  ✔   Saved: {result['zip_name']}")
        else:
            print(f"  ✘   Save failed: {result['message']}")
            if session.is_zip_session:
                print(f"  ℹ   Temp folder preserved: {session.temp_dir}")
    else:
        if session.is_zip_session:
            print(f"  ℹ   Temp folder preserved: {session.temp_dir}")
            print("      Re-run to continue this session.")
    print()


def run(photos_dir, port, explicit_zip=None, minimal=False):
    global SESSION, _server_ref, MINIMAL_MODE, _startup_complete
    MINIMAL_MODE = minimal
    SESSION = setup_session(photos_dir, explicit_zip)

    if minimal:
        # ── MINIMAL MODE ──────────────────────────────────────────────────
        # No watcher, no auto-rename, no dedup, no hash index.
        # Image list is read directly on each request (safe for debugging).
        _startup_complete = True   # skip "not ready" guards
        images = get_images(SESSION.serve_folder)
        _image_list.extend(images)
        _current_count = len(images)
        count = len(images)
    else:
        start_watcher(SESSION.serve_folder, interval=2.0)
        count = '(indexing…)'

    ThreadingHTTPServer.allow_reuse_address = True
    server = ThreadingHTTPServer(('localhost', port), GalleryHandler)
    _server_ref = server

    url = f"http://localhost:{port}"
    print(f"\n📷  Photo Gallery{'  [MINIMAL MODE]' if minimal else ''}")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    if SESSION.is_zip_session:
        print(f"   Source  : {SESSION.source_zip.name}")
        print(f"   Session : {SESSION.temp_dir}")
    else:
        print(f"   Folder  : {SESSION.serve_folder}")
    print(f"   Images  : {count}")
    print(f"   URL     : {url}")
    if minimal:
        print(f"   Mode    : MINIMAL — auto-rename, dedup, file-watch disabled")
        print(f"             /api/images refreshes from disk on every request")
    print(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print(f"\nOpen {url} in your browser")
    print("Press Ctrl+C to stop\n")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        handle_ctrl_c(SESSION, server)


if __name__=='__main__':
    p=argparse.ArgumentParser(
        description='Local Photo Gallery',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Modes:
  Normal   python3 server.py --folder ./photos
  Minimal  python3 server.py --folder ./photos --minimal
             Disables: auto-rename, duplicate detection, file watcher,
             new-file tracking, hash index.  Use for debugging when the
             full server hangs or misbehaves.
""")
    p.add_argument('--folder',  '-f', default='./photos',
                   help='Photos folder (default: ./photos)')
    p.add_argument('--zip',     '-z', default=None,
                   help='Explicit zip to load (overrides auto-detect)')
    p.add_argument('--port',    '-p', type=int, default=8765,
                   help='Port (default: 8765)')
    p.add_argument('--minimal', '-m', action='store_true',
                   help='Minimal mode — disables all background processing')
    a=p.parse_args()
    run(Path(a.folder).resolve(), a.port,
        Path(a.zip).resolve() if a.zip else None,
        minimal=a.minimal)
