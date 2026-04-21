"""Shared cloud execution orchestrator for strategy-backtester.

Centralizes project management, file sync, run submission, polling, and
result download. Used by run_remote.py, run_all_cloud.py, and cloud_sweep.py.

All low-level API calls go through lib/cr_client.py — this module composes
them into higher-level orchestration workflows.
"""

import hashlib
import json
import os
import time
from glob import glob

from lib.cr_client import TERMINAL_STATUSES

# ------------------------------------------------------------------ #
# Centralized file lists (previously duplicated across 3 scripts)
# ------------------------------------------------------------------ #

EOD_ENGINE_FILES = [
    "engine/__init__.py",
    "engine/pipeline.py",
    "engine/config_loader.py",
    "engine/config_sweep.py",
    "engine/simulator.py",
    "engine/ranking.py",
    "engine/scanner.py",
    "engine/order_generator.py",
    "engine/utils.py",
    "engine/charges.py",
    "engine/constants.py",
    "engine/data_provider.py",
]

INTRADAY_ENGINE_FILES = [
    "engine/__init__.py",
    "engine/intraday_pipeline.py",
    "engine/intraday_sql_builder.py",
    "engine/intraday_simulator.py",
    "engine/intraday_simulator_v2.py",
    "engine/charges.py",
    "engine/constants.py",
]

LIB_FILES = [
    "lib/__init__.py",
    "lib/cr_client.py",
    "lib/metrics.py",
    "lib/backtest_result.py",
    "lib/data_utils.py",
    "lib/indicators.py",
    "lib/data_fetchers.py",
]

DEFAULT_DEPENDENCIES = ["requests", "pyyaml", "polars==1.37.1", "pyarrow"]

# Root of the strategy-backtester repo
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class CloudOrchestrator:
    """High-level cloud execution orchestrator built on CetaResearch client.

    Manages project lifecycle, file sync, run submission, polling, and
    result download for running backtests on CR cloud compute.
    """

    def __init__(self, cr, project_name="sb-remote", dependencies=None,
                 verbose=True):
        """
        Args:
            cr: CetaResearch client instance.
            project_name: Cloud project name (for find/create).
            dependencies: pip packages for the project.
            verbose: Print progress messages.
        """
        self.cr = cr
        self.project_name = project_name
        self.dependencies = dependencies or list(DEFAULT_DEPENDENCIES)
        self.verbose = verbose
        self._project_cache_path = os.path.join(ROOT, ".remote_project.json")
        self._hash_cache_path = os.path.join(ROOT, ".remote_hashes.json")

    # ------------------------------------------------------------------ #
    # Project management
    # ------------------------------------------------------------------ #

    def find_or_create_project(self, entrypoint="_run_1.py", description=""):
        """Find existing project by name or create a new one.

        Caches project ID to .remote_project.json for fast subsequent lookups.

        Returns:
            dict with project details (includes 'id' key).
        """
        # Try cache first
        cached = self._load_project_cache()
        if cached and cached.get("name") == self.project_name:
            project_id = cached["id"]
            try:
                self.cr.get_project(project_id)  # validate project exists
                self.cr.update_project(project_id, dependencies=self.dependencies)
                if self.verbose:
                    print(f"  Project: {project_id} (cached)")
                return {"id": project_id, "name": self.project_name}
            except Exception:
                pass  # cache stale, fall through to search

        # Search by name
        projects = self.cr.list_projects(limit=100)
        for p in projects.get("projects", []):
            if p["name"] == self.project_name:
                self.cr.update_project(p["id"], dependencies=self.dependencies)
                self._save_project_cache(p)
                if self.verbose:
                    print(f"  Project: {p['id']} (found)")
                return p

        # Create new
        project = self.cr.create_project(
            name=self.project_name,
            language="python",
            entrypoint=entrypoint,
            dependencies=self.dependencies,
            description=description or "Auto-managed by strategy-backtester",
        )
        self._save_project_cache(project)
        if self.verbose:
            print(f"  Project: {project['id']} (created)")
        return project

    def _load_project_cache(self):
        try:
            with open(self._project_cache_path) as f:
                return json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return None

    def _save_project_cache(self, project):
        with open(self._project_cache_path, "w") as f:
            json.dump({"id": project["id"], "name": self.project_name}, f)

    # ------------------------------------------------------------------ #
    # File discovery
    # ------------------------------------------------------------------ #

    def discover_files(self, mode="eod"):
        """Discover files to upload based on execution mode.

        Args:
            mode: "eod" (EOD pipeline + signals), "intraday", or "all" (union).

        Returns:
            List of relative file paths (from repo root).
        """
        files = set()

        if mode in ("eod", "all"):
            files.update(EOD_ENGINE_FILES)
            # Dynamically discover signal generators
            for f in sorted(glob(os.path.join(ROOT, "engine", "signals", "*.py"))):
                files.add(os.path.relpath(f, ROOT))

        if mode in ("intraday", "all"):
            files.update(INTRADAY_ENGINE_FILES)

        files.update(LIB_FILES)

        # Filter to files that actually exist
        result = []
        for rel_path in sorted(files):
            if os.path.exists(os.path.join(ROOT, rel_path)):
                result.append(rel_path)
        return result

    # ------------------------------------------------------------------ #
    # File sync (hash-based diff)
    # ------------------------------------------------------------------ #

    def sync_files(self, project_id, file_paths, force=False):
        """Upload files to project, skipping unchanged ones (hash-based diff).

        Args:
            project_id: Project UUID.
            file_paths: List of relative paths to upload.
            force: Upload all files regardless of hash cache.

        Returns:
            Number of files uploaded.
        """
        old_hashes = {} if force else self._load_hash_cache()
        new_hashes = {}
        uploaded = 0
        skipped = 0

        for rel_path in file_paths:
            full_path = os.path.join(ROOT, rel_path)
            if not os.path.exists(full_path):
                continue

            content = self._read_file(rel_path)
            file_hash = hashlib.sha256(content.encode()).hexdigest()
            new_hashes[rel_path] = file_hash

            if not force and old_hashes.get(rel_path) == file_hash:
                skipped += 1
                continue

            self.upsert_with_retry(project_id, rel_path, content)
            uploaded += 1
            if self.verbose and uploaded % 10 == 0:
                print(f"    Uploaded {uploaded} files...")

        # Save updated hashes
        old_hashes.update(new_hashes)
        self._save_hash_cache(old_hashes)

        if self.verbose:
            print(f"  Synced: {uploaded} uploaded, {skipped} unchanged")
        return uploaded

    def _load_hash_cache(self):
        """Load the hash cache scoped to this orchestrator's `project_name`.

        Audit P6.3 (2026-04-21): the cache file is a dict-of-dicts:
            { "<project_name>": { "<path>": "<sha256>", ... }, ... }
        Pre-fix this was a flat {path: hash} dict, which meant switching
        `project_name` (e.g. "sb-remote" -> "sb-eod-sweep-v2") had the
        new project skip uploads claimed by the old cache → the cloud
        project ended up empty and runs failed with ImportError.
        Backward-compat: a flat file from before this change is
        migrated to the new layout under the current project_name on
        first write.
        """
        try:
            with open(self._hash_cache_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

        if not isinstance(data, dict):
            return {}

        # If the file has any values that are themselves dicts, assume
        # new layout; return the entry for our project (or empty).
        if any(isinstance(v, dict) for v in data.values()):
            return dict(data.get(self.project_name, {}))

        # Legacy flat layout: treat as belonging to our project so we
        # don't re-upload everything on first run after upgrade.
        return dict(data)

    def _save_hash_cache(self, hashes):
        """Save this project's hashes without clobbering other projects."""
        # Read the full file (may contain other projects' entries).
        try:
            with open(self._hash_cache_path) as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            data = {}

        if not isinstance(data, dict):
            data = {}

        # Detect legacy flat format; upgrade in place.
        if data and not any(isinstance(v, dict) for v in data.values()):
            data = {self.project_name: data}

        data[self.project_name] = hashes
        with open(self._hash_cache_path, "w") as f:
            json.dump(data, f)

    # ------------------------------------------------------------------ #
    # File upload with retry
    # ------------------------------------------------------------------ #

    def upsert_with_retry(self, project_id, path, content, max_retries=10):
        """Upload a file with rate-limit retry and exponential backoff.

        Extracted from run_all_cloud.py's _upsert(). Handles 429, RATE_LIMIT,
        and connection errors with up to 10 retries.
        """
        for attempt in range(max_retries):
            try:
                self.cr.upsert_file(project_id, path, content)
                return
            except Exception as e:
                err_str = str(e)
                if "429" in err_str or "RATE_LIMIT" in err_str or "Connection" in err_str:
                    wait = min(300, 60 * (attempt + 1))
                    if self.verbose:
                        print(f"    Rate limited on {path}, waiting {wait}s...")
                    time.sleep(wait)
                else:
                    raise
        raise RuntimeError(f"Failed to upload {path} after {max_retries} retries")

    # ------------------------------------------------------------------ #
    # Wrapper generation
    # ------------------------------------------------------------------ #

    def make_wrapper(self, script, config_file=None, env_vars=None):
        """Generate a wrapper script for cloud execution.

        Args:
            script: Entry script to execute (e.g. "cloud_main_eod.py" or "scripts/buy_2day_high.py").
            config_file: If set, injects CONFIG_FILE env var.
            env_vars: Dict of additional env vars to set.

        Returns:
            Wrapper Python source code as string.
        """
        lines = ["import sys, os"]

        # Set CR_API_KEY for cloud containers
        lines.append(f'os.environ["CR_API_KEY"] = "{self.cr.api_key}"')

        if config_file:
            lines.append(f'os.environ["CONFIG_FILE"] = {config_file!r}')

        if env_vars:
            for k, v in env_vars.items():
                lines.append(f'os.environ[{k!r}] = {v!r}')

        lines.append('sys.path.insert(0, os.getcwd())')

        lines.append("")
        lines.append(f'exec(open({script!r}).read())')
        return "\n".join(lines) + "\n"

    # ------------------------------------------------------------------ #
    # Run submission
    # ------------------------------------------------------------------ #

    def submit_run(self, project_id, entry_path="_run_1.py", cpu=8,
                   ram_mb=61440, timeout=7200, quiet=False):
        """Submit a project run (no polling).

        Args:
            quiet: Suppress the "Run submitted" print (for callers that log themselves).

        Returns:
            run_id (str or int).
        """
        result = self.cr.run_project(
            project_id,
            entry_path=entry_path,
            cpu_count=cpu,
            ram_mb=ram_mb,
            timeout_seconds=timeout,
            install_timeout_seconds=300,
            poll=False,
        )
        run_id = result.get("id") or result.get("taskId")
        if self.verbose and not quiet:
            print(f"  Run submitted: {run_id}")
        return run_id

    # ------------------------------------------------------------------ #
    # Polling
    # ------------------------------------------------------------------ #

    def poll_run(self, project_id, run_id, timeout=7200, poll_interval=30,
                 on_progress=None):
        """Poll a run until it reaches a terminal status.

        Args:
            project_id: Project UUID.
            run_id: Run ID (from submit_run).
            timeout: Max seconds to wait.
            poll_interval: Seconds between polls.
            on_progress: Optional callback(elapsed_secs, status, last_stdout_line).

        Returns:
            Final run result dict (with status, stdout, stderr, etc.)
        """
        start_time = time.time()
        deadline = start_time + timeout + 120  # generous buffer

        while time.time() < deadline:
            time.sleep(poll_interval)
            try:
                result = self.cr.get_run(project_id, run_id)
            except Exception as e:
                if self.verbose:
                    print(f"  Poll error: {e}")
                continue

            status = result.get("status", "unknown")
            elapsed = int(time.time() - start_time)

            if on_progress:
                stdout = result.get("stdout", "")
                lines = stdout.strip().split("\n") if stdout else []
                last_line = lines[-1] if lines else ""
                on_progress(elapsed, status, last_line)
            elif self.verbose:
                stdout = result.get("stdout", "")
                lines = stdout.strip().split("\n") if stdout else []
                last_line = lines[-1][:120] if lines else ""
                print(f"  [{elapsed}s] {status} | {last_line}")

            if status in TERMINAL_STATUSES:
                return result

        raise RuntimeError(f"Polling timed out after {timeout}s")

    # ------------------------------------------------------------------ #
    # Result download
    # ------------------------------------------------------------------ #

    def download_results(self, run_id, path="results.json"):
        """Download and parse results from a completed run.

        Uses the Code Execution files API (get_execution_files) which is
        compatible with project run IDs.

        Args:
            run_id: Run ID from a completed run.
            path: Remote file path to download.

        Returns:
            List of config result dicts (extracted from SweepResult or legacy list).
        """
        content = self.cr.get_execution_files(run_id, path=path)
        data = json.loads(content)

        # SweepResult format: extract all_configs list
        if isinstance(data, dict) and data.get("type") == "sweep":
            return data.get("all_configs", [])

        # Legacy flat list format
        if isinstance(data, list):
            return data

        return []

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    @staticmethod
    def _read_file(rel_path):
        """Read a file relative to the repo root."""
        with open(os.path.join(ROOT, rel_path)) as f:
            return f.read()
