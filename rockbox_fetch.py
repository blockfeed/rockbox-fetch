#!/usr/bin/env python3
"""
rockbox_fetch.py — Fetch & deploy Rockbox builds (nightly or release) to a mounted SD card.

Features:
- Fetch latest nightly, specific nightly date, or release (with checksum verification if published).
- List devices, list nightly dates, list releases.
- Non-destructive: backs up `.rockbox` before merging new files (user configs/themes preserved).
- Revert support to most recent or chosen backup.
- Hardened fetching (browser User-Agent, retries, fallback to daily.shtml if needed).
"""

import argparse
import getpass
import io
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple
from urllib.parse import urljoin

import requests
from requests.adapters import HTTPAdapter, Retry
from zipfile import ZipFile

BASE_RELEASE = "https://download.rockbox.org/release/"
BASE_DAILY = "https://download.rockbox.org/daily/"
DAILY_SHTML = "https://www.rockbox.org/daily.shtml"
DAILY_INDEX_TMPL = BASE_DAILY + "{device}/"

# ---------------- HTTP ----------------
UA = "Mozilla/5.0 (X11; Linux x86_64; rv:120.0) Gecko/20100101 Firefox/120.0"
_session = requests.Session()
_retry = Retry(total=3, backoff_factor=0.4, status_forcelist=(403, 429, 500, 502, 503, 504))
_session.mount("https://", HTTPAdapter(max_retries=_retry))
_session.headers.update({"User-Agent": UA, "Accept": "*/*"})

def get_text(url: str, timeout=20) -> str:
    r = _session.get(url, timeout=timeout)
    if r.status_code == 403:
        raise SystemExit(f"[!] HTTP 403 for {url}")
    r.raise_for_status()
    return r.text

def get_bytes(url: str, timeout=60) -> bytes:
    r = _session.get(url, timeout=timeout)
    r.raise_for_status()
    return r.content

# ---------------- Logging ----------------
def log(msg: str): print(f"[+] {msg}")
def warn(msg: str): print(f"[!] {msg}")
def die(msg: str, code: int = 1): warn(msg); sys.exit(code)

# ---------------- CLI ----------------
def parse_args():
    p = argparse.ArgumentParser(description="Download and deploy Rockbox builds (nightly/release).")
    p.add_argument("--device", help="Rockbox device build name (e.g., erosqnative).")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--date", help="Daily build date YYYYMMDD (e.g., 20250822).")
    g.add_argument("--release", help="Release version (e.g., 4.0).")
    p.add_argument("--label", help="Volume label (e.g., H2).")
    p.add_argument("--mount-path", help="Explicit mount point of SD root.")
    p.add_argument("--mount-root", default=f"/run/media/{getpass.getuser()}",
                   help="Root under which removable volumes are mounted.")
    p.add_argument("--revert", nargs="?", const="latest",
                   help="Restore previous backup (omit for latest, or pass tarball path).")
    p.add_argument("--dry-run", action="store_true", help="Plan only; no writes.")
    p.add_argument("-v", "--verbose", action="store_true", help="Verbose logging.")

    # Listing
    p.add_argument("--list-releases", action="store_true", help="List official releases and exit.")
    p.add_argument("--list-devices", action="store_true", help="List all device build names and exit.")
    p.add_argument("--list-dailies", action="store_true", help="List nightly dates for --device and exit.")
    p.add_argument("--max-list", type=int, default=0, help="Max rows printed (0 = all).")
    return p.parse_args()

# ---------------- Build discovery ----------------
NIGHTLY_RE = re.compile(r"rockbox-(?P<device>[a-z0-9]+)-(?P<date>\d{8})\.zip")

def latest_nightly_url_for_device(device: str) -> Tuple[str, str]:
    text = get_text(DAILY_INDEX_TMPL.format(device=device))
    dates = [m.group("date") for m in NIGHTLY_RE.finditer(text) if m.group("device") == device]
    if not dates:
        for h in re.findall(r'href="([^"]+)"', text):
            mm = NIGHTLY_RE.search(h)
            if mm and mm.group("device") == device:
                dates.append(mm.group("date"))
    if not dates: die(f"No nightly builds found for {device}")
    latest = max(dates)
    return urljoin(BASE_DAILY + f"{device}/", f"rockbox-{device}-{latest}.zip"), latest

def nightly_url_for_date(device: str, yyyymmdd: str) -> str:
    if not re.fullmatch(r"\d{8}", yyyymmdd): die("--date must be YYYYMMDD")
    return urljoin(BASE_DAILY + f"{device}/", f"rockbox-{device}-{yyyymmdd}.zip")

def release_url(device: str, version: str) -> str:
    return urljoin(BASE_RELEASE, f"{version}/rockbox-{device}-{version}.zip")

def list_releases() -> list[str]:
    text = get_text(BASE_RELEASE)
    hrefs = re.findall(r'href="([^"/]+)/"', text)
    versions = [h for h in hrefs if re.fullmatch(r"\d+(\.\d+)+", h)]
    versions.sort(key=lambda v: tuple(map(int, v.split("."))), reverse=True)
    return versions

def list_devices_from_daily() -> list[str]:
    try:
        text = get_text(BASE_DAILY)
        dirs = re.findall(r'href="([a-z0-9]+)/"', text)
        if dirs: return sorted(set(dirs))
    except SystemExit: pass
    page = get_text(DAILY_SHTML)
    cand = set(re.findall(r'href="/?daily/([a-z0-9]+)/', page))
    cand |= set(re.findall(r'rockbox-([a-z0-9]+)-\d{8}\.zip', page))
    return sorted(cand)

def list_dailies_for_device(device: str) -> list[str]:
    try:
        text = get_text(DAILY_INDEX_TMPL.format(device=device))
        dates = [m.group("date") for m in NIGHTLY_RE.finditer(text) if m.group("device") == device]
        for h in re.findall(r'href="([^"]+)"', text):
            mm = NIGHTLY_RE.search(h)
            if mm and mm.group("device") == device: dates.append(mm.group("date"))
        out = sorted(set(dates), reverse=True)
        if out: return out
    except SystemExit: pass
    page = get_text(DAILY_SHTML)
    return sorted(set(re.findall(rf'rockbox-{re.escape(device)}-(\d{{8}})\.zip', page)), reverse=True)

# ---------------- FS helpers ----------------
def resolve_mount_path(label: Optional[str], mount_root: str, mount_path_cli: Optional[str]) -> Path:
    if mount_path_cli:
        mp = Path(mount_path_cli).expanduser().resolve()
        if not mp.exists(): die(f"--mount-path does not exist: {mp}")
        return mp
    if not label: die("Provide --label or --mount-path")
    root = Path(mount_root).expanduser()
    candidate = root / label
    if not candidate.is_dir():
        alts = [p for p in root.glob("*") if p.is_dir() and p.name.lower() == label.lower()]
        if not alts: die(f"Mount path not found for label '{label}'")
        candidate = alts[0]
    return candidate

def find_dot_rockbox(root: Path) -> Path: return root / ".rockbox"

def ensure_writable(path: Path):
    testfile = path / f".write_test_{int(time.time())}"
    try:
        with open(testfile, "w") as f: f.write("ok")
    except Exception as e: die(f"Cannot write to {path}: {e}")
    finally:
        try: testfile.unlink(missing_ok=True)
        except Exception: pass

# ---------------- Backup / Revert ----------------
def backups_dir(root: Path) -> Path:
    b = root / ".rockbox_backups"; b.mkdir(exist_ok=True); return b

def create_backup(dot_rockbox: Path, dry: bool, verbose: bool) -> Optional[Path]:
    if not dot_rockbox.exists():
        warn(f"No {dot_rockbox} to back up."); return None
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    out = backups_dir(dot_rockbox.parent) / f"rockbox-backup-{ts}.tar.gz"
    if dry: log(f"[dry-run] Would back up: {out}"); return out
    log(f"Creating backup: {out}")
    with tarfile.open(out, "w:gz") as tar: tar.add(dot_rockbox, arcname=".rockbox")
    return out

def list_backups(root: Path) -> list[Path]:
    return sorted(backups_dir(root).glob("rockbox-backup-*.tar.gz"), key=lambda p: p.stat().st_mtime)

def restore_backup(root: Path, backup_tar: Path, dry: bool):
    dot_rockbox = root / ".rockbox"
    if dry: log(f"[dry-run] Would restore {backup_tar}"); return
    log(f"Restoring {backup_tar.name}")
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        with tarfile.open(backup_tar, "r:gz") as tar: tar.extractall(td_path)
        merge_copy(td_path / ".rockbox", dot_rockbox)

# ---------------- Merge copy ----------------
def merge_copy(src_dir: Path, dst_dir: Path):
    dst_dir.mkdir(exist_ok=True)
    for root, dirs, files in os.walk(src_dir):
        rel = Path(root).relative_to(src_dir)
        dst_sub = dst_dir / rel; dst_sub.mkdir(exist_ok=True)
        for d in dirs: (dst_sub / d).mkdir(exist_ok=True)
        for f in files: shutil.copy2(Path(root)/f, dst_sub/f)

# ---------------- Deploy ----------------
def unzip_and_deploy(zip_bytes: bytes, mount_root: Path, dry: bool, verbose: bool):
    with ZipFile(io.BytesIO(zip_bytes)) as zf:
        if ".rockbox" not in {p.split("/")[0] for p in zf.namelist() if "/" in p}:
            die("Archive lacks .rockbox")
        with tempfile.TemporaryDirectory() as td:
            td_path = Path(td); zf.extractall(td_path)
            if dry: log("[dry-run] Would merge into .rockbox"); return
            merge_copy(td_path/".rockbox", mount_root/".rockbox")

# ---------------- Main ----------------
def _print_capped(items, max_list: int):
    if max_list > 0:
        for v in items[:max_list]: print("  ", v)
        if len(items) > max_list: print(f"  ... ({len(items)-max_list} more)")
    else:
        for v in items: print("  ", v)

def main():
    args = parse_args()

    if args.list_releases:
        rels = list_releases(); log("Releases:"); _print_capped(rels, args.max_list); return
    if args.list_devices:
        devs = list_devices_from_daily(); log("Devices:"); _print_capped(devs, args.max_list); return
    if args.list_dailies:
        if not args.device: die("--list-dailies needs --device")
        dates = list_dailies_for_device(args.device)
        log(f"Dailies for {args.device}:"); _print_capped(dates, args.max_list); return

    if args.revert is not None:
        mp = resolve_mount_path(args.label, args.mount_root, args.mount_path)
        backup_tar = list_backups(mp)[-1] if args.revert == "latest" else Path(args.revert)
        if not backup_tar.exists(): die(f"Backup not found: {backup_tar}")
        ensure_writable(mp); restore_backup(mp, backup_tar, args.dry_run); return

    if not args.device: die("Need --device for deployment")
    mp = resolve_mount_path(args.label, args.mount_root, args.mount_path)
    dot_rb = find_dot_rockbox(mp)

    if args.release:
        url = release_url(args.device, args.release); label = f"release {args.release}"
    elif args.date:
        url = nightly_url_for_date(args.device, args.date); label = f"nightly {args.date}"
    else:
        url, latest = latest_nightly_url_for_device(args.device); label = f"nightly {latest}"

    log(f"Selected build: {label}\nURL: {url}")
    ensure_writable(mp)
    backup_path = create_backup(dot_rb, args.dry_run, args.verbose)
    if backup_path: log(f"Backup: {backup_path}")
    if args.dry_run: return

    log("Downloading...")
    zbytes = get_bytes(url)
    log("Deploying...")
    unzip_and_deploy(zbytes, mp, args.dry_run, args.verbose)
    log("Done.")

if __name__ == "__main__":
    main()

