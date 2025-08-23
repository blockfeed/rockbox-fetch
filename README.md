# rockbox_fetch.py

A Python utility to download and deploy Rockbox builds (nightly or release) to a mounted SD card.

## Features

- Fetch and deploy the **latest nightly** or a specific nightly date for a given device (e.g. `erosqnative`).
- Install a **specific release** (e.g. `4.0`), verifying checksums if provided by Rockbox.
- **List modes**:
  - `--list-devices`: list all known Rockbox device build targets.
  - `--list-dailies`: list nightly builds available for a specific device.
  - `--list-releases`: list all official tagged releases.
- Non-destructive: backs up current `.rockbox` directory and merges new files over existing (user configs/themes preserved).
- `--revert` to restore the most recent backup or a specific backup tarball.
- `--dry-run` mode to preview without writing.

## Usage

### List devices
```bash
python3 rockbox_fetch.py --list-devices --max-list 0
```

### List nightly builds for a device
```bash
python3 rockbox_fetch.py --list-dailies --device erosqnative --max-list 20
```

### List official releases
```bash
python3 rockbox_fetch.py --list-releases
```

### Deploy latest nightly
```bash
python3 rockbox_fetch.py --device erosqnative --label H2
```

### Deploy specific nightly date
```bash
python3 rockbox_fetch.py --device erosqnative --label H2 --date 20250822
```

### Deploy a release with checksum verification
```bash
python3 rockbox_fetch.py --device erosqnative --label H2 --release 4.0
```

### Revert to the most recent backup
```bash
python3 rockbox_fetch.py --label H2 --revert
```

### Revert to a specific backup file
```bash
python3 rockbox_fetch.py --label H2 --revert /run/media/$USER/H2/.rockbox_backups/rockbox-backup-20250823-013500.tar.gz
```

### Dry-run (plan only)
```bash
python3 rockbox_fetch.py --device erosqnative --label H2 --dry-run -v
```

## Safety Notes

- Always backs up `.rockbox` before writing.
- Merges files (does not delete user content).
- Release ZIPs are verified if checksum files are present on Rockbox servers.

---
