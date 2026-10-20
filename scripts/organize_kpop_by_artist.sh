#!/usr/bin/env bash
set -euo pipefail

MNT=/home/pi/MaiconErick/Umbra/mnt/smb_192.168.1.133_D
CREDS=/home/pi/MaiconErick/Umbra/credentials/smb_192.168.1.133.sec
SRC_REL="Music/xNewMusic/KPOP OLD 20xx"
SRC="$MNT/$SRC_REL"
LOGDIR=/home/pi/MaiconErick/Umbra/logs
mkdir -p "$LOGDIR"
LOG="$LOGDIR/organize_kpop_by_artist_$(date +%Y%m%d_%H%M%S).log"
>"$LOG"

# mount share (try 3.0,2.1,1.0)
mkdir -p "$MNT"
mounted=""
for v in 3.0 2.1 1.0; do
  if sudo mount -t cifs //192.168.1.133/D$ "$MNT" -o credentials="$CREDS",uid=$(id -u pi),gid=$(id -g pi),iocharset=utf8,vers=$v,file_mode=0644,dir_mode=0755 2>/tmp/mount_err.txt; then
    mounted="$v"
    echo "Mounted SMB vers=$v" | tee -a "$LOG"
    break
  else
    echo "mount vers=$v failed" | tee -a "$LOG"
    cat /tmp/mount_err.txt >> "$LOG" || true
  fi
done

if [ -z "$mounted" ]; then
  echo "ERROR: could not mount SMB share" | tee -a "$LOG" >&2
  exit 2
fi

if [ ! -d "$SRC" ]; then
  echo "ERROR: source path not found: $SRC" | tee -a "$LOG" >&2
  echo "Listing top-level of mounted share:" >> "$LOG"
  ls -la "$MNT" >> "$LOG" || true
  sudo umount -l "$MNT" || true
  exit 3
fi

moved=0
skipped=0
failed=0

# process all files recursively
find "$SRC" -type f -print0 | while IFS= read -r -d '' file; do
  base=$(basename -- "$file")
  # extract artist and track using first ' - '
  if [[ "$base" == *" - "* ]]; then
    artist_raw="${base%% - *}"
    track="${base#* - }"
  else
    artist_raw="Unknown Artist"
    track="$base"
  fi
  # sanitize artist folder name: trim spaces and replace forbidden chars
  safe_artist=$(echo "$artist_raw" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//' -e 's/[\\\/:*?"<>|]/_/g')
  dest_dir="$SRC/$safe_artist"

  # skip if already inside correct artist folder
  parent_dir=$(dirname -- "$file")
  if [ "$parent_dir" = "$dest_dir" ]; then
    skipped=$((skipped+1))
    echo "SKIP already in artist folder: $file" >> "$LOG"
    continue
  fi

  mkdir -p "$dest_dir"

  # prepare destination filename; handle collisions
  dest_file="$dest_dir/$track"
  if [ -e "$dest_file" ]; then
    basef="${track%.*}"
    ext="${track##*.}"
    if [ "$basef" = "$track" ]; then
      # no extension
      i=1
      while [ -e "$dest_dir/$basef ($i)" ]; do i=$((i+1)); done
      dest_file="$dest_dir/$basef ($i)"
    else
      i=1
      while [ -e "$dest_dir/$basef ($i).$ext" ]; do i=$((i+1)); done
      dest_file="$dest_dir/$basef ($i).$ext"
    fi
  fi

  if mv -- "$file" "$dest_file" 2>>"$LOG"; then
    echo "MOVED: '$file' -> '$dest_file'" >> "$LOG"
    moved=$((moved+1))
  else
    echo "FAILED: mv '$file' -> '$dest_file'" >> "$LOG"
    failed=$((failed+1))
  fi

done

# find runs in a subshell; capture counts from log by parsing
moved=$(grep -c '^MOVED:' "$LOG" || true)
skipped=$(grep -c '^SKIP' "$LOG" || true)
failed=$(grep -c '^FAILED' "$LOG" || true)

sudo umount -l "$MNT" || true

echo "DONE. moved=$moved skipped=$skipped failed=$failed log=$LOG"
exit 0
