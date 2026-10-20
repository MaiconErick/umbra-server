#!/usr/bin/env python3
"""
Organize anime folders from "New folder" into Animes folder.
- Heuristic to detect anime entries
- Clean names by removing tags and common release/codec keywords
- Move entries to destination with safe conflict handling
"""
import os, re, shutil, sys
SRC = "/home/pi/MaiconErick/Umbra/mnt/s_share/New folder"
DST = "/home/pi/MaiconErick/Umbra/mnt/s_share/Animes"
SKIP_GROUPS_MOVIE = ['yts', 'yts.mx']
ANIME_GROUP_HINTS = ['judas', 'xc', 'feibanyama', 'ember', 'embers']
COMMON_TAGS_RE = re.compile(r"\b(1080p|720p|2160p|4k|hevc|x265|x264|10bit|8bit|dual[- ]audio|multi[- ]subs|bd(rip)?|bdrip|bluray|webrip|web[- ]rip|nf|op(uce|us)|ddp|dd|10 bits|10bit|he-aac|aac)\b", re.I)
BRACKET_RE = re.compile(r"\[[^\]]*\]")
PAREN_RE = re.compile(r"\([^)]*\)")
DOTS_RE = re.compile(r"[._]+")
SPACE_RE = re.compile(r"\s+")


def is_anime(name):
    n = name
    lower = n.lower()
    # clear obvious movie release groups
    if any(x in lower for x in SKIP_GROUPS_MOVIE):
        return False
    # typical anime signals
    if re.search(r"\bseason\b", lower):
        return True
    if re.search(r"\bs\d{1,2}\b", n):
        return True
    if re.search(r"\bova\b", lower):
        return True
    if re.search(r"\bepisode(s)?\b", lower):
        return True
    if '+' in name and 'movie' in lower or 'movies' in lower:
        return True
    # check leading release-group bracket
    m = re.match(r"^\[([^\]]+)\]", name)
    if m:
        grp = m.group(1).lower()
        if any(g in grp for g in ANIME_GROUP_HINTS):
            return True
    # fallback: if name contains words common on anime releases
    if re.search(r"\b(tv|ova|movie)\b", lower):
        return True
    return False


def clean_name(name):
    s = name
    # remove bracketed tags
    s = BRACKET_RE.sub(' ', s)
    # remove common codec/quality tags
    s = COMMON_TAGS_RE.sub(' ', s)
    # remove parenthesis contents (years, misc)
    s = PAREN_RE.sub(' ', s)
    # replace dots/underscores with space
    s = DOTS_RE.sub(' ', s)
    # remove trailing release-group like -EMBER or edge2020
    s = re.sub(r"[-_]?\b(edge\d{4}|ember[s]?|-ember)\b", ' ', s, flags=re.I)
    # remove leftover non-alphanumeric except + and : and - kept
    s = re.sub(r"[^\w\s\+\-]", ' ', s)
    s = SPACE_RE.sub(' ', s).strip()
    # fix common "S01+Movies" spacing
    s = re.sub(r"\b(S)(\d{1,2})\b", lambda m: f"{m.group(1)}{m.group(2).zfill(2)}", s)
    return s


def safe_move(srcpath, dstdir, name):
    os.makedirs(dstdir, exist_ok=True)
    target = os.path.join(dstdir, name)
    if not os.path.exists(target):
        shutil.move(srcpath, target)
        return target
    # find available suffix
    for i in range(1,1000):
        alt = f"{target}_{i}"
        if not os.path.exists(alt):
            shutil.move(srcpath, alt)
            return alt
    raise RuntimeError('no available target name')


def main():
    if not os.path.isdir(SRC):
        print(f"Source not found: {SRC}")
        sys.exit(1)
    moved = []
    skipped = []
    for entry in sorted(os.listdir(SRC)):
        if entry.startswith('.'):
            continue
        srcpath = os.path.join(SRC, entry)
        if not (os.path.isdir(srcpath) or os.path.isfile(srcpath)):
            continue
        try:
            if is_anime(entry):
                newname = clean_name(entry)
                if not newname:
                    newname = entry
                target = safe_move(srcpath, DST, newname)
                moved.append((entry, target))
                print(f"MOVED: '{entry}' -> '{target}'")
            else:
                skipped.append(entry)
                print(f"SKIP: '{entry}' (not detected as anime)")
        except Exception as e:
            print(f"ERROR moving {entry}: {e}")
    print('\nSUMMARY:')
    print(f"Moved {len(moved)} items")
    for o,t in moved:
        print(f" - {o} -> {t}")
    print(f"Skipped {len(skipped)} items")
    for s in skipped:
        print(f" - {s}")

if __name__=='__main__':
    main()
