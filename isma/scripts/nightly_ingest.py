#!/usr/bin/env python3
"""
ISMA Nightly Ingestion Pipeline

Runs via systemd daily. Discovers and processes new data from:
1. Mira local .claude/projects/ (new Claude Code sessions)
2. Mac .claude/projects/ (rsync + parse)
3. Jetson .claude/projects/ (rsync + parse)
4. Drop directory ($ISMA_DATA_DIR/incoming/)

Usage:
    python3 nightly_ingest.py                # Full nightly run
    python3 nightly_ingest.py --check        # Preview what would run
    python3 nightly_ingest.py --skip-rsync   # Skip remote rsync sources
"""

import argparse
import json
import logging
import shutil
import subprocess
import time
from datetime import datetime, timezone
import os
from pathlib import Path

from isma.config import NIGHTLY_MAC_HOST as CONFIG_NIGHTLY_MAC_HOST


# =============================================================================
# CONFIGURATION
# =============================================================================

SCRIPTS_DIR = Path(__file__).parent
DATA_DIR = Path(os.environ.get("ISMA_DATA_DIR", str(Path.home() / "data")))
STATE_FILE = DATA_DIR / "ingest_state.json"
LOG_FILE = DATA_DIR / "nightly_ingest.log"

MIRA_CLAUDE_PROJECTS = Path(os.environ.get("ISMA_CLAUDE_PROJECTS_DIR", str(Path.home() / ".claude/projects")))
MAC_STAGING = DATA_DIR / "staging/mac_claude_projects"
JETSON_STAGING = DATA_DIR / "jetson_transcripts"
INCOMING_DIR = DATA_DIR / "incoming"
PARSED_DIR = DATA_DIR / "transcripts/parsed"
CORPUS_DIR = DATA_DIR / "corpus"

MAC_HOST = CONFIG_NIGHTLY_MAC_HOST
MAC_CLAUDE_PROJECTS = os.environ.get("ISMA_MAC_CLAUDE_PROJECTS", "")  # operator rsync source; empty = skip
JETSON_RSYNC_SOURCE = os.environ.get("ISMA_JETSON_RSYNC_SOURCE", "")  # operator rsync source; empty = skip

SOURCE_NAMES = (
    "mira_local",
    "mac_remote",
    "jetson_remote",
    "incoming_transcripts",
    "incoming_corpus",
)

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nightly")


def utc_now_iso():
    return datetime.now(timezone.utc).isoformat()


def default_state():
    return {
        "last_run": None,
        "last_ingest_timestamp": {name: None for name in SOURCE_NAMES},
        "files_ingested": {},
    }


def load_state():
    state = default_state()
    if STATE_FILE.exists():
        with open(STATE_FILE, encoding="utf-8") as handle:
            raw = json.load(handle)
        if isinstance(raw, dict):
            state["last_run"] = raw.get("last_run")
            state["files_ingested"] = raw.get("files_ingested", {})

            last_ingest = raw.get("last_ingest_timestamp", {})
            if not last_ingest and raw.get("last_spark_mtime"):
                last_ingest["mira_local"] = datetime.fromtimestamp(
                    raw["last_spark_mtime"], tz=timezone.utc
                ).isoformat()
            for name in SOURCE_NAMES:
                state["last_ingest_timestamp"][name] = last_ingest.get(name)
    return state


def save_state(state):
    state["last_run"] = utc_now_iso()
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w", encoding="utf-8") as handle:
        json.dump(state, handle, indent=2, sort_keys=True)


def run_cmd(cmd, description, check_only=False):
    """Run a shell command with logging."""
    log.info("%sRunning: %s", "[CHECK] " if check_only else "", description)
    if check_only:
        log.info("  Would run: %s", cmd)
        return True

    try:
        result = subprocess.run(
            cmd,
            shell=True,
            capture_output=True,
            text=True,
            timeout=7200,
        )
    except subprocess.TimeoutExpired:
        log.error("  TIMEOUT after 2 hours")
        return False

    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        log.error("  FAILED: %s", stderr[:500] or "command returned non-zero exit")
        return False

    stdout = (result.stdout or "").strip()
    if stdout:
        for line in stdout.splitlines()[-5:]:
            log.info("  %s", line)
    return True


def is_main_session(path):
    path_str = str(path)
    return path.suffix == ".jsonl" and "/subagents/" not in path_str and not path.name.startswith("agent-")


def source_file_key(source_name, source_root, file_path):
    try:
        relative_path = file_path.resolve().relative_to(source_root.resolve())
    except ValueError:
        relative_path = Path(file_path.name)
    return f"{source_name}:{relative_path.as_posix()}"


def get_file_record(state, source_name, source_root, file_path):
    key = source_file_key(source_name, source_root, file_path)
    return key, state["files_ingested"].get(key)


def file_is_new(state, source_name, source_root, file_path):
    key, record = get_file_record(state, source_name, source_root, file_path)
    stat = file_path.stat()
    if not record:
        return True, key, stat

    same_size = record.get("size") == stat.st_size
    same_mtime = record.get("mtime_ns") == stat.st_mtime_ns
    return not (same_size and same_mtime), key, stat


def mark_file_ingested(state, source_name, key, file_path, stat):
    ingest_time = utc_now_iso()
    state["files_ingested"][key] = {
        "source": source_name,
        "path": str(file_path),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "ingested_at": ingest_time,
    }
    state["last_ingest_timestamp"][source_name] = ingest_time


def existing_parsed_output(file_path):
    return PARSED_DIR / "claude_code" / f"{file_path.stem[:16]}.json"


def ingest_transcript_source(state, source_name, source_dir, label, check_only=False):
    """Parse transcript sources incrementally with per-file checkpoints."""
    if not source_dir.exists():
        log.info("%s: source directory missing at %s", label, source_dir)
        return 0

    candidates = sorted(path for path in source_dir.rglob("*.jsonl") if is_main_session(path))
    new_files = []

    for file_path in candidates:
        is_new, key, stat = file_is_new(state, source_name, source_dir, file_path)
        if (
            not check_only
            and is_new
            and existing_parsed_output(file_path).exists()
            and key not in state["files_ingested"]
        ):
            mark_file_ingested(state, source_name, key, file_path, stat)
            is_new = False
        if is_new:
            new_files.append(file_path)

    log.info("%s: Found %d new session files", label, len(new_files))

    processed = 0
    for file_path in sorted(new_files):
        success = run_cmd(
            f"cd {SCRIPTS_DIR} && python3 parse_raw_exports.py "
            f"--input '{file_path}' --output '{PARSED_DIR}'",
            f"Parse {label} {file_path.name}",
            check_only=check_only,
        )
        if success:
            processed += 1
            if not check_only:
                _, key, stat = file_is_new(state, source_name, source_dir, file_path)
                mark_file_ingested(state, source_name, key, file_path, stat)

    return processed


def rsync_source(cmd, description, destination, check_only=False):
    destination.mkdir(parents=True, exist_ok=True)
    return run_cmd(cmd, description, check_only=check_only)


def rsync_and_ingest_mac(state, check_only=False, skip_rsync=False):
    """rsync Mac .claude/projects/ and parse new sessions."""
    if skip_rsync:
        log.info("Mac: Skipping rsync (--skip-rsync)")
        return 0

    success = rsync_source(
        f"rsync -avz --include='*.jsonl' --include='*/' --exclude='*' "
        f"{MAC_HOST}:{MAC_CLAUDE_PROJECTS} {MAC_STAGING}/",
        "rsync Mac .claude/projects/",
        MAC_STAGING,
        check_only=check_only,
    )
    if not success and not check_only:
        log.warning("Mac rsync failed; continuing without Mac updates")
        return 0

    return ingest_transcript_source(
        state,
        "mac_remote",
        MAC_STAGING,
        "Mac",
        check_only=check_only,
    )


def rsync_and_ingest_jetson(state, check_only=False, skip_rsync=False):
    """rsync Jetson .claude/projects/ and parse new sessions."""
    if skip_rsync:
        log.info("Jetson: Skipping rsync (--skip-rsync)")
        return 0

    success = rsync_source(
        f"rsync -avz {JETSON_RSYNC_SOURCE} {JETSON_STAGING}/",
        "rsync Jetson .claude/projects/",
        JETSON_STAGING,
        check_only=check_only,
    )
    if not success and not check_only:
        log.warning("Jetson rsync failed; continuing without Jetson updates")
        return 0

    return ingest_transcript_source(
        state,
        "jetson_remote",
        JETSON_STAGING,
        "Jetson",
        check_only=check_only,
    )


def process_incoming(state, check_only=False):
    """Process files dropped in $ISMA_DATA_DIR/incoming/."""
    transcripts_dir = INCOMING_DIR / "transcripts"
    corpus_drop_dir = INCOMING_DIR / "corpus"
    transcripts_dir.mkdir(parents=True, exist_ok=True)
    corpus_drop_dir.mkdir(parents=True, exist_ok=True)

    transcript_files = [path for path in transcripts_dir.rglob("*") if path.is_file()]
    corpus_files = [path for path in corpus_drop_dir.rglob("*") if path.is_file()]

    log.info(
        "Incoming: %d transcript files, %d corpus files in incoming/",
        len(transcript_files),
        len(corpus_files),
    )

    processed = 0
    archive = INCOMING_DIR / "processed" / datetime.now().strftime("%Y%m%d")

    for file_path in sorted(transcript_files):
        is_new, key, stat = file_is_new(state, "incoming_transcripts", transcripts_dir, file_path)
        if not is_new:
            log.info("Incoming transcript already ingested: %s", file_path.name)
        elif file_path.suffix in (".jsonl", ".json"):
            success = run_cmd(
                f"cd {SCRIPTS_DIR} && python3 parse_raw_exports.py "
                f"--input '{file_path}' --output '{PARSED_DIR}'",
                f"Parse incoming transcript {file_path.name}",
                check_only=check_only,
            )
            if success:
                processed += 1
                if not check_only:
                    mark_file_ingested(state, "incoming_transcripts", key, file_path, stat)
        else:
            log.info("Skipping unsupported incoming transcript file: %s", file_path.name)

        if not check_only:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), archive / file_path.name)

    for file_path in sorted(corpus_files):
        is_new, key, stat = file_is_new(state, "incoming_corpus", corpus_drop_dir, file_path)
        dest = CORPUS_DIR / "mira_loose" / file_path.name

        if not is_new:
            log.info("Incoming corpus already ingested: %s", file_path.name)
        else:
            if check_only:
                log.info("[CHECK] Would copy corpus file %s to %s", file_path, dest)
                processed += 1
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(file_path, dest)
                mark_file_ingested(state, "incoming_corpus", key, file_path, stat)
                processed += 1
                log.info("Copied corpus file %s to %s", file_path.name, dest)

        if not check_only:
            archive.mkdir(parents=True, exist_ok=True)
            shutil.move(str(file_path), archive / file_path.name)

    return processed


def rebuild_manifest_and_embed(check_only=False):
    """Rebuild dedup manifest then run unified_ingest.py --incremental."""
    success = run_cmd(
        f"cd {SCRIPTS_DIR} && python3 build_dedup_manifest.py",
        "Rebuild dedup manifest",
        check_only=check_only,
    )
    if not success and not check_only:
        log.error("Manifest rebuild failed; skipping embed")
        return False

    return run_cmd(
        f"cd {SCRIPTS_DIR} && python3 -u unified_ingest.py --incremental",
        "Unified ingest (incremental, dual-write Weaviate+Neo4j)",
        check_only=check_only,
    )


def main():
    parser = argparse.ArgumentParser(description="ISMA Nightly Ingestion")
    parser.add_argument("--check", action="store_true", help="Preview only")
    parser.add_argument("--skip-rsync", action="store_true", help="Skip remote rsync sources")
    args = parser.parse_args()

    log.info("=" * 60)
    log.info("ISMA Nightly Ingestion - %s", datetime.now().isoformat())
    log.info("=" * 60)

    state = load_state()
    if state.get("last_run"):
        log.info("Last run: %s", state["last_run"])

    start = time.time()

    n_mira = ingest_transcript_source(
        state,
        "mira_local",
        MIRA_CLAUDE_PROJECTS,
        "Mira local",
        check_only=args.check,
    )
    n_mac = rsync_and_ingest_mac(state, check_only=args.check, skip_rsync=args.skip_rsync)
    n_jetson = rsync_and_ingest_jetson(state, check_only=args.check, skip_rsync=args.skip_rsync)
    n_incoming = process_incoming(state, check_only=args.check)

    if (n_mira + n_mac + n_jetson + n_incoming) > 0 or not args.check:
        rebuild_manifest_and_embed(check_only=args.check)

    elapsed = time.time() - start

    if not args.check:
        save_state(state)

    log.info("")
    log.info("Complete in %.0fs (%.1f min)", elapsed, elapsed / 60)
    log.info("  Mira local sessions: %d", n_mira)
    log.info("  Mac sessions: %d", n_mac)
    log.info("  Jetson sessions: %d", n_jetson)
    log.info("  Incoming files processed: %d", n_incoming)


if __name__ == "__main__":
    main()
