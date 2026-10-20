#!/usr/bin/env python3
import os, shutil, sys, re
SRC = "/home/pi/MaiconErick/Umbra/mnt/s_share/Animes"
DST = "/home/pi/MaiconErick/Umbra/mnt/s_share/S Anime"
COMMON_EXTS = ['mkv','mp4','avi','m4v','mov','webm']

if not os.path.exists(SRC):
    print(f"Source not found: {SRC}")
    sys.exit(1)

os.makedirs(DST, exist_ok=True)

moved = []
errors = []

for entry in sorted(os.listdir(SRC)):
    if entry.startswith('.'):
        continue
    srcpath = os.path.join(SRC, entry)
    try:
        # Fix filenames like "Name mkv" -> "Name.mkv"
        newentry = entry
        if os.path.isfile(srcpath):
            # match trailing space + ext (no dot)
            m = re.search(r"\s+(%s)$" % '|'.join(COMMON_EXTS), entry, re.I)
            if m:
                ext = m.group(1)
                base = entry[:m.start()].rstrip()
                newentry = f"{base}.{ext}"
        # prepare destination and avoid collisions
        destpath = os.path.join(DST, newentry)
        if os.path.exists(destpath):
            for i in range(1,1000):
                name_noext, dot, ext = newentry.rpartition('.')
                if dot:
                    candidate = f"{name_noext}_{i}.{ext}"
                else:
                    candidate = f"{newentry}_{i}"
                destpath = os.path.join(DST, candidate)
                if not os.path.exists(destpath):
                    newentry = os.path.basename(destpath)
                    break
        shutil.move(srcpath, destpath)
        moved.append((srcpath, destpath))
        print(f"MOVED: '{srcpath}' -> '{destpath}'")
    except Exception as e:
        errors.append((srcpath, str(e)))
        print(f"ERROR moving {srcpath}: {e}")

print('\nSUMMARY:')
print(f"Moved {len(moved)} items")
for s,d in moved:
    print(f" - {s} -> {d}")
print(f"Errors: {len(errors)}")
for s,e in errors:
    print(f" - {s}: {e}")

# remove old Animes dir if empty
try:
    if not os.listdir(SRC):
        os.rmdir(SRC)
        print(f"Removed empty source directory: {SRC}")
except Exception:
    pass
