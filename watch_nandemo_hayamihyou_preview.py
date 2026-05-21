from __future__ import annotations

import hashlib
import subprocess
import sys
import time
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parent
TARGET_FILE = ROOT_DIR / "shortcodes" / "nandemo" / "aga-hayamihyou-preview.html"
SYNC_SCRIPT = ROOT_DIR / "sync_nandemo_hayamihyou_preview.sh"
POLL_SECONDS = 2


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    if not TARGET_FILE.exists():
        print(f"Target file not found: {TARGET_FILE}", file=sys.stderr)
        return 1
    if not SYNC_SCRIPT.exists():
        print(f"Sync script not found: {SYNC_SCRIPT}", file=sys.stderr)
        return 1

    last_seen = digest(TARGET_FILE)
    print(f"Watching {TARGET_FILE}")
    print("Detected changes will sync to https://nandemo.trigger-tech.info/?p=24766&preview=true")

    while True:
        time.sleep(POLL_SECONDS)
        current = digest(TARGET_FILE)
        if current == last_seen:
            continue

        print("Change detected. Syncing preview post...")
        try:
            subprocess.run(
                [str(SYNC_SCRIPT)],
                cwd=str(ROOT_DIR),
                check=True,
            )
        except subprocess.CalledProcessError as exc:
            print(f"Sync failed with exit code {exc.returncode}", file=sys.stderr)
        else:
            last_seen = current
            print("Sync completed.")


if __name__ == "__main__":
    raise SystemExit(main())
