"""Ceta Research data provider and platform API client.

Primary client for the Ceta Research platform. Used by default in backtest
scripts. Provides access to:
  - SQL execution API (data queries, backtests)
  - Code Execution API (run code on cloud compute)
  - Projects API (manage multi-file projects, run on cloud)

Usage:

    from cr_client import CetaResearch

    cr = CetaResearch(api_key="your_api_key")

    # SQL queries (JSON or Parquet)
    results = cr.query("SELECT symbol, piotroskiScore FROM scores WHERE piotroskiScore >= 7")

    # Parquet format for bulk data
    parquet_bytes = cr.query("SELECT * FROM stock_eod WHERE symbol = 'AAPL'", format="parquet")

    # Code Execution API
    result = cr.execute_code("import pandas as pd; print(pd.__version__)", dependencies=["pandas"])
    print(result["stdout"])

    # Projects API
    project = cr.create_project("my-analysis", language="python")
    cr.upsert_file(project["id"], "main.py", "print('hello')")
    run = cr.run_project(project["id"])
    print(run["stdout"])
"""

import os
import time
import json
import base64
import requests

DEFAULT_BASE_URL = "https://api.cetaresearch.com/api/v1"
DEFAULT_POLL_INTERVAL = 5.0  # seconds (keep low to avoid 1000 req/hr rate limit on polls)
DEFAULT_TIMEOUT = 300  # seconds


class CetaResearchError(Exception):
    """Base exception for Ceta Research API errors."""
    pass


class QueryTimeoutError(CetaResearchError):
    pass


class QueryFailedError(CetaResearchError):
    pass


class ExecutionError(CetaResearchError):
    """Error from Code Execution or Projects API."""
    pass


class CetaResearch:
    """Client for the Ceta Research platform APIs.

    Args:
        api_key: Your API key (from cetaresearch.com).
                 Falls back to CR_API_KEY, then TS_API_KEY environment variable.
        base_url: API base URL. Defaults to production.
    """

    def __init__(self, api_key=None, base_url=None):
        self.api_key = (
            api_key
            or os.environ.get("CR_API_KEY")
            or os.environ.get("TS_API_KEY")
        )
        if not self.api_key:
            raise CetaResearchError(
                "No API key provided. Pass api_key= or set CR_API_KEY environment variable.\n"
                "Get your key at: https://cetaresearch.com"
            )
        self.base_url = (
            base_url
            or os.environ.get("CR_BASE_URL")
            or os.environ.get("TS_BASE_URL")
            or DEFAULT_BASE_URL
        ).rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": self.api_key,
            "Content-Type": "application/json",
        })

    # ------------------------------------------------------------------ #
    # SQL / Data Explorer API
    # ------------------------------------------------------------------ #

    def query(self, sql, timeout=DEFAULT_TIMEOUT, limit=100000, format="json",
              verbose=False, memory_mb=None, threads=None, disk_mb=None):
        """Execute SQL and return results.

        Args:
            sql: SQL query string.
            timeout: Max execution time in seconds.
            limit: Max rows to return.
            format: Output format (json, csv, parquet).
            verbose: Print progress messages.
            memory_mb: Server-side memory allocation in MB (default: server decides).
                       Use 16384 for large backtests, None for simple queries.
            threads: Server-side thread count (default: server decides).
                     Use 6 for large backtests, None for simple queries.
            disk_mb: Server-side disk allocation in MB (default: server decides).
                     Use 40960 for large backtests, None for simple queries.

        Returns:
            List of dicts (one per row) for JSON format.
            Raw text for CSV format.
            Raw bytes for parquet format.
        """
        task_id = self._submit(sql, timeout=timeout, limit=limit, format=format,
                               memory_mb=memory_mb, threads=threads, disk_mb=disk_mb)
        if verbose:
            print(f"  Query submitted (task: {task_id[:8]}...)")

        task = self._poll(task_id, timeout=timeout, verbose=verbose)

        if task["status"] == "completed":
            return self._download(task, format=format)
        elif task["status"] in ("failed", "execution_timed_out", "wait_timed_out", "cancelled"):
            error_msg = task.get("error", task["status"])
            raise QueryFailedError(f"Query failed: {error_msg}")
        else:
            raise QueryTimeoutError(f"Query timed out after {timeout}s (status: {task['status']})")

    def _submit(self, sql, timeout=300, limit=100000, format="json",
                memory_mb=None, threads=None, disk_mb=None):
        """Submit a query and return the task ID."""
        body = {
            "query": sql,
            "options": {
                "timeout": timeout,
                "limit": limit,
                "format": format,
            },
        }
        if memory_mb is not None or threads is not None or disk_mb is not None:
            resources = {}
            if memory_mb is not None:
                resources["memoryMb"] = memory_mb
            if threads is not None:
                resources["threads"] = threads
            if disk_mb is not None:
                resources["diskMb"] = disk_mb
            body["resources"] = resources
        resp = self.session.post(f"{self.base_url}/data-explorer/execute", json=body)
        if resp.status_code == 429:
            raise CetaResearchError(f"Rate limited. Retry after {resp.headers.get('Retry-After', '60')}s")
        if resp.status_code not in (200, 201, 202):
            raise CetaResearchError(f"Submit failed ({resp.status_code}): {resp.text[:500]}")
        data = resp.json()
        return data.get("taskId") or resp.headers.get("X-Task-ID")

    def _poll(self, task_id, timeout=300, verbose=False):
        """Poll task status until completion or timeout."""
        deadline = time.time() + timeout + 30  # extra buffer for poll overhead
        backoff = DEFAULT_POLL_INTERVAL
        while time.time() < deadline:
            resp = self.session.get(f"{self.base_url}/data-explorer/tasks/{task_id}")
            if resp.status_code == 429:
                wait = int(resp.headers.get("Retry-After", 60))
                if verbose:
                    print(f"  Rate limited on poll, waiting {wait}s...")
                time.sleep(wait)
                backoff = min(backoff * 2, 30)  # increase poll interval after rate limit
                continue
            if resp.status_code != 200:
                raise CetaResearchError(f"Poll failed ({resp.status_code}): {resp.text[:500]}")
            task = resp.json()
            status = task.get("status", "unknown")

            if status in ("completed", "failed", "execution_timed_out", "wait_timed_out", "cancelled"):
                return task

            if verbose:
                print(f"  Status: {status}...")
            time.sleep(backoff)

        return {"status": "poll_timeout"}

    def _download(self, task, format="json"):
        """Download results from a completed task.

        The server stores results as parquet (native). JSON/CSV are converted
        on demand. The download endpoint may return:
          1. Inline data (the actual content)
          2. A presigned URL response: {"available": true, "url": "..."}
          3. A preparing response: {"available": false, "retryAfter": 5}
        """
        artifact_id = task.get("artifactId")
        data_url = task.get("dataUrl")

        # Build download URL. dataUrl always points to result.json, so for
        # other formats we construct the URL from artifactId directly.
        ext_map = {"json": "result.json", "csv": "result.csv", "parquet": "result.parquet"}
        filename = ext_map.get(format, "result.json")

        if artifact_id:
            url = f"{self.base_url}/data-explorer/artifacts/{artifact_id}/download/{filename}"
        elif data_url:
            if format != "json" and "result.json" in data_url:
                data_url = data_url.replace("result.json", filename)
            if data_url.startswith("http"):
                url = data_url
            elif data_url.startswith("/"):
                origin = self.base_url.split("/api/")[0]
                url = f"{origin}/api{data_url}"
            else:
                url = f"{self.base_url}/{data_url}"
        else:
            raise CetaResearchError("No artifact ID or data URL in completed task")

        resp = self._fetch_with_retry(url, format)
        return self._parse_response(resp, format)

    def _fetch_with_retry(self, url, format, max_retries=20, initial_wait=2):
        """Fetch URL, following presigned URLs and retrying if file is preparing.

        Args:
            max_retries: Max retry attempts (default 20 = ~2 min for large files)
            initial_wait: Initial retry wait time in seconds (grows with backoff)
        """
        wait_time = initial_wait
        for attempt in range(max_retries):
            resp = self.session.get(url)

            if resp.status_code != 200:
                raise CetaResearchError(f"Download failed ({resp.status_code}): {resp.text[:500]}")

            # Check if this is a presigned URL / preparing response.
            # Inline data for parquet is binary (not JSON), so only check
            # for JSON responses that look like metadata.
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" in content_type or (format != "parquet" and not content_type):
                try:
                    body = resp.json()
                    if isinstance(body, dict):
                        if body.get("available") and body.get("url"):
                            # Presigned URL - fetch the actual file
                            presigned_resp = requests.get(body["url"], timeout=300)
                            if presigned_resp.status_code != 200:
                                raise CetaResearchError(
                                    f"Presigned URL fetch failed ({presigned_resp.status_code})"
                                )
                            return presigned_resp
                        if body.get("available") is False:
                            # File is being prepared (JSON/CSV conversion from parquet)
                            # Use server's suggested retry time, or exponential backoff
                            wait = body.get("retryAfter", wait_time)
                            time.sleep(wait)
                            # Exponential backoff, capped at 10s
                            wait_time = min(wait_time * 1.5, 10)
                            continue
                except (json.JSONDecodeError, ValueError):
                    pass

            # Got inline data (parquet binary or JSON array)
            return resp

        raise CetaResearchError(f"File still preparing after {max_retries} retries (~{max_retries * initial_wait}s)")

    def _parse_response(self, resp, format):
        """Parse the final response based on requested format."""
        if format == "json":
            return resp.json()
        elif format == "csv":
            return resp.text
        else:
            return resp.content

    def query_saved(self, query_id, parameters=None, timeout=300, limit=100000):
        """Execute a saved query by ID.

        Args:
            query_id: The saved query ID (e.g., from a shared URL).
            parameters: Optional dict of parameter overrides.
            timeout: Max execution time.
            limit: Max rows.

        Returns:
            List of dicts.
        """
        body = {"options": {"timeout": timeout, "limit": limit}}
        if parameters:
            body["parameters"] = parameters

        resp = self.session.post(
            f"{self.base_url}/data-explorer/queries/{query_id}/execute",
            json=body,
        )
        if resp.status_code not in (200, 201, 202):
            raise CetaResearchError(f"Submit failed ({resp.status_code}): {resp.text[:500]}")
        task_id = resp.json().get("taskId") or resp.headers.get("X-Task-ID")

        task = self._poll(task_id, timeout=timeout)
        if task["status"] == "completed":
            return self._download(task)
        raise QueryFailedError(f"Saved query failed: {task.get('error', task['status'])}")

    # ------------------------------------------------------------------ #
    # Code Execution API
    # ------------------------------------------------------------------ #

    def execute_code(self, code, language="python", dependencies=None,
                     cpu_count=None, ram_mb=None, disk_mb=None,
                     timeout_seconds=None, install_timeout_seconds=None,
                     wait_timeout_seconds=None, poll=True, verbose=False):
        """Submit code for cloud execution.

        Args:
            code: Source code string.
            language: Programming language (default: "python").
            dependencies: List of pip packages to install.
            cpu_count: CPU cores (default: server decides).
            ram_mb: RAM in MB (default: server decides).
            disk_mb: Disk in MB (default: server decides).
            timeout_seconds: Execution timeout.
            install_timeout_seconds: Dependency install timeout.
            wait_timeout_seconds: Queue wait timeout.
            poll: If True, poll until completion and return full result.
                  If False, return immediately with {"taskId": ...}.
            verbose: Print progress.

        Returns:
            dict with taskId, status, stdout, stderr, exitCode, generatedFiles, etc.
        """
        body = {"code": code, "language": language}
        if dependencies:
            body["dependencies"] = dependencies
        if cpu_count is not None:
            body["cpuCount"] = cpu_count
        if ram_mb is not None:
            body["ramMb"] = ram_mb
        if disk_mb is not None:
            body["diskMb"] = disk_mb
        if timeout_seconds is not None:
            body["timeoutSeconds"] = timeout_seconds
        if install_timeout_seconds is not None:
            body["installTimeoutSeconds"] = install_timeout_seconds
        if wait_timeout_seconds is not None:
            body["waitTimeoutSeconds"] = wait_timeout_seconds

        resp = self._post_with_csrf(f"{self.base_url}/code-executions", json=body)
        if resp.status_code not in (200, 201, 202):
            raise ExecutionError(f"Execute failed ({resp.status_code}): {resp.text[:500]}")

        result = resp.json()
        task_id = result.get("taskId")

        if not poll:
            return result

        return self._poll_execution(task_id, timeout=timeout_seconds or 300, verbose=verbose)

    def get_execution_status(self, task_id):
        """Get status of a code execution task.

        Args:
            task_id: Integer task ID.

        Returns:
            dict with taskId, status, stdout, stderr, exitCode, generatedFiles, etc.
        """
        resp = self.session.get(f"{self.base_url}/code-executions/{task_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Get execution failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def get_execution_files(self, task_id, path=None):
        """Download output file(s) from a completed code execution.

        Args:
            task_id: Integer task ID.
            path: File path to download. If None, returns file list from status.

        Returns:
            bytes (file content) if path specified.
            list of file dicts if path is None.
        """
        if path is None:
            status = self.get_execution_status(task_id)
            return status.get("generatedFiles", [])

        resp = self.session.get(
            f"{self.base_url}/code-executions/{task_id}/files",
            params={"path": path},
        )
        if resp.status_code != 200:
            raise ExecutionError(f"File download failed ({resp.status_code}): {resp.text[:500]}")
        return resp.content

    def cancel_execution(self, task_id):
        """Cancel a running or queued code execution.

        Args:
            task_id: Integer task ID.

        Returns:
            dict with taskId, status, message.
        """
        resp = self._delete_with_csrf(f"{self.base_url}/code-executions/{task_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Cancel failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def execute_from_repo(self, repo_url, ref=None, entry_path=None,
                          dependencies=None, cpu_count=None, ram_mb=None,
                          disk_mb=None, timeout_seconds=None,
                          install_timeout_seconds=None, wait_timeout_seconds=None,
                          poll=True, verbose=False):
        """Run code from a GitHub repository.

        Args:
            repo_url: GitHub repository URL (e.g. "https://github.com/owner/repo").
            ref: Branch, tag, or commit hash. Defaults to repo's default branch.
            entry_path: Path to entry Python file. Auto-detected if omitted.
            dependencies: Additional pip packages (merged with auto-detected).
            cpu_count, ram_mb, disk_mb, timeout_seconds: Compute resources.
            poll: If True, poll until completion.
            verbose: Print progress.

        Returns:
            dict with taskId, status, stdout, stderr, etc.
        """
        body = {"repoUrl": repo_url}
        if ref:
            body["ref"] = ref
        if entry_path:
            body["entryPath"] = entry_path
        if dependencies:
            body["dependencies"] = dependencies

        compute = {}
        if cpu_count is not None:
            compute["cpuCount"] = cpu_count
        if ram_mb is not None:
            compute["ramMb"] = ram_mb
        if disk_mb is not None:
            compute["diskMb"] = disk_mb
        if timeout_seconds is not None:
            compute["timeoutSeconds"] = timeout_seconds
        if install_timeout_seconds is not None:
            compute["installTimeoutSeconds"] = install_timeout_seconds
        if wait_timeout_seconds is not None:
            compute["waitTimeoutSeconds"] = wait_timeout_seconds
        if compute:
            body["compute"] = compute

        resp = self._post_with_csrf(f"{self.base_url}/code-executions/run-from-repo", json=body)
        if resp.status_code not in (200, 201, 202):
            raise ExecutionError(f"Run from repo failed ({resp.status_code}): {resp.text[:500]}")

        result = resp.json()
        task_id = result.get("taskId")

        if not poll:
            return result

        return self._poll_execution(task_id, timeout=timeout_seconds or 300, verbose=verbose)

    def get_execution_limits(self):
        """Get resource limits for the current user's tier.

        Returns:
            dict with tier, maxCpuCores, maxMemoryGb, maxDiskGb, etc.
        """
        resp = self.session.get(f"{self.base_url}/code-executions/limits")
        if resp.status_code != 200:
            raise ExecutionError(f"Get limits failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def list_executions(self, limit=20, offset=0):
        """List code executions.

        Args:
            limit: Max results (1-100).
            offset: Pagination offset.

        Returns:
            dict with executions list and totalCount.
        """
        resp = self.session.get(
            f"{self.base_url}/code-executions",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code != 200:
            raise ExecutionError(f"List executions failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _poll_execution(self, task_id, timeout=300, verbose=False):
        """Poll a code execution task until completion."""
        deadline = time.time() + timeout + 60  # generous buffer for queue + install
        while time.time() < deadline:
            result = self.get_execution_status(task_id)
            status = result.get("status", "unknown")

            if status in ("completed", "failed", "execution_timed_out",
                          "wait_timed_out", "cancelled"):
                return result

            if verbose:
                pos = result.get("queuePosition")
                extra = f" (queue position: {pos})" if pos is not None else ""
                print(f"  Execution status: {status}{extra}")

            time.sleep(DEFAULT_POLL_INTERVAL)

        raise ExecutionError(f"Execution polling timed out after {timeout}s")

    # ------------------------------------------------------------------ #
    # Projects API
    # ------------------------------------------------------------------ #

    def create_project(self, name, language="python", entrypoint=None,
                       dependencies=None, description=None):
        """Create a new project.

        Args:
            name: Project name.
            language: Programming language (default: "python").
            entrypoint: Entry file path (e.g. "main.py").
            dependencies: List of pip packages.
            description: Project description.

        Returns:
            dict with id, name, language, entrypoint, dependencies, etc.
        """
        body = {"name": name, "language": language}
        if entrypoint:
            body["entrypoint"] = entrypoint
        if dependencies:
            body["dependencies"] = dependencies
        if description:
            body["description"] = description

        resp = self._post_with_csrf(f"{self.base_url}/projects", json=body)
        if resp.status_code not in (200, 201):
            raise ExecutionError(f"Create project failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def list_projects(self, limit=20, offset=0):
        """List projects.

        Returns:
            dict with projects list and totalCount.
        """
        resp = self.session.get(
            f"{self.base_url}/projects",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code != 200:
            raise ExecutionError(f"List projects failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def get_project(self, project_id):
        """Get project details.

        Args:
            project_id: UUID string.

        Returns:
            dict with project details.
        """
        resp = self.session.get(f"{self.base_url}/projects/{project_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Get project failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def update_project(self, project_id, name=None, entrypoint=None,
                       dependencies=None, description=None, visibility=None):
        """Update project settings.

        Args:
            project_id: UUID string.
            name, entrypoint, dependencies, description, visibility: Fields to update.

        Returns:
            dict with updated project.
        """
        body = {}
        if name is not None:
            body["name"] = name
        if entrypoint is not None:
            body["entrypoint"] = entrypoint
        if dependencies is not None:
            body["dependencies"] = dependencies
        if description is not None:
            body["description"] = description
        if visibility is not None:
            body["visibility"] = visibility

        resp = self._patch_with_csrf(f"{self.base_url}/projects/{project_id}", json=body)
        if resp.status_code != 200:
            raise ExecutionError(f"Update project failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def delete_project(self, project_id):
        """Delete a project.

        Args:
            project_id: UUID string.
        """
        resp = self._delete_with_csrf(f"{self.base_url}/projects/{project_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Delete project failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def upsert_file(self, project_id, path, content, content_encoding=None):
        """Create or update a file in a project.

        Args:
            project_id: UUID string.
            path: File path within the project (e.g. "main.py", "utils/helpers.py").
            content: File content (string for text, base64 string for binary).
            content_encoding: Set to "base64" if content is base64-encoded.

        Returns:
            dict with id, path, sizeBytes, contentHash, updatedAt.
        """
        body = {"path": path, "content": content}
        if content_encoding:
            body["contentEncoding"] = content_encoding

        resp = self._put_with_csrf(f"{self.base_url}/projects/{project_id}/files", json=body)
        if resp.status_code not in (200, 201):
            raise ExecutionError(f"Upsert file failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def get_file(self, project_id, file_path):
        """Get a file from a project.

        Args:
            project_id: UUID string.
            file_path: File path within the project.

        Returns:
            dict with path, content (base64), contentEncoding, sizeBytes, contentHash.
        """
        resp = self.session.get(f"{self.base_url}/projects/{project_id}/files/{file_path}")
        if resp.status_code != 200:
            raise ExecutionError(f"Get file failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def delete_file(self, project_id, file_path):
        """Delete a file from a project.

        Args:
            project_id: UUID string.
            file_path: File path within the project.
        """
        resp = self._delete_with_csrf(f"{self.base_url}/projects/{project_id}/files/{file_path}")
        if resp.status_code != 200:
            raise ExecutionError(f"Delete file failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def run_project(self, project_id, entry_path=None, cpu_count=None,
                    ram_mb=None, disk_mb=None, timeout_seconds=None,
                    install_timeout_seconds=None, wait_timeout_seconds=None,
                    poll=True, verbose=False):
        """Run a project on cloud compute.

        Args:
            project_id: UUID string.
            entry_path: Override entry file (defaults to project's entrypoint).
            cpu_count, ram_mb, disk_mb, timeout_seconds: Compute resources.
            poll: If True, poll until completion.
            verbose: Print progress.

        Returns:
            dict with run details (id/taskId, status, stdout, stderr, etc.)
        """
        body = {}
        if entry_path:
            body["entryPath"] = entry_path
        if cpu_count is not None:
            body["cpuCount"] = cpu_count
        if ram_mb is not None:
            body["ramMb"] = ram_mb
        if disk_mb is not None:
            body["diskMb"] = disk_mb
        if timeout_seconds is not None:
            body["timeoutSeconds"] = timeout_seconds
        if install_timeout_seconds is not None:
            body["installTimeoutSeconds"] = install_timeout_seconds
        if wait_timeout_seconds is not None:
            body["waitTimeoutSeconds"] = wait_timeout_seconds

        resp = self._post_with_csrf(f"{self.base_url}/projects/{project_id}/run", json=body)
        if resp.status_code not in (200, 201, 202):
            raise ExecutionError(f"Run project failed ({resp.status_code}): {resp.text[:500]}")

        result = resp.json()

        if not poll:
            return result

        # The run response may use "id" or "taskId" for the run identifier
        run_id = result.get("id") or result.get("taskId")
        return self._poll_project_run(project_id, run_id, timeout=timeout_seconds or 300,
                                      verbose=verbose)

    def list_runs(self, project_id, limit=20, offset=0):
        """List runs for a project.

        Returns:
            dict with runs list and totalCount.
        """
        resp = self.session.get(
            f"{self.base_url}/projects/{project_id}/runs",
            params={"limit": limit, "offset": offset},
        )
        if resp.status_code != 200:
            raise ExecutionError(f"List runs failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def get_run(self, project_id, run_id):
        """Get run details.

        Args:
            project_id: UUID string.
            run_id: Integer run ID.

        Returns:
            dict with run details.
        """
        resp = self.session.get(f"{self.base_url}/projects/{project_id}/runs/{run_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Get run failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def cancel_run(self, project_id, run_id):
        """Cancel a running or queued project run.

        Args:
            project_id: UUID string.
            run_id: Integer run ID.
        """
        resp = self._delete_with_csrf(f"{self.base_url}/projects/{project_id}/runs/{run_id}")
        if resp.status_code != 200:
            raise ExecutionError(f"Cancel run failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def get_run_files(self, project_id, run_id):
        """Get output files from a completed project run.

        Args:
            project_id: UUID string.
            run_id: Integer run ID.

        Returns:
            list of file metadata dicts.
        """
        resp = self.session.get(f"{self.base_url}/projects/{project_id}/runs/{run_id}/files")
        if resp.status_code != 200:
            raise ExecutionError(f"Get run files failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def import_project_from_git(self, repo_url, ref=None):
        """Import a project from a GitHub repository.

        Args:
            repo_url: GitHub repository URL.
            ref: Branch, tag, or commit hash.

        Returns:
            dict with imported project details.
        """
        body = {"repoUrl": repo_url}
        if ref:
            body["ref"] = ref

        resp = self._post_with_csrf(f"{self.base_url}/projects/import-git", json=body)
        if resp.status_code not in (200, 201):
            raise ExecutionError(f"Import from git failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def pull_project_from_git(self, project_id):
        """Pull latest changes from linked git repository.

        Args:
            project_id: UUID string.

        Returns:
            dict with pull status.
        """
        resp = self._post_with_csrf(f"{self.base_url}/projects/{project_id}/git-link/pull", json={})
        if resp.status_code != 200:
            raise ExecutionError(f"Git pull failed ({resp.status_code}): {resp.text[:500]}")
        return resp.json()

    def _poll_project_run(self, project_id, run_id, timeout=300, verbose=False):
        """Poll a project run until completion with retry on transient errors."""
        deadline = time.time() + timeout + 60
        backoff = DEFAULT_POLL_INTERVAL
        conn_errors = 0
        while time.time() < deadline:
            try:
                result = self.get_run(project_id, run_id)
                conn_errors = 0  # reset on success
            except ExecutionError as e:
                if "429" in str(e) or "RATE_LIMIT" in str(e):
                    wait = min(backoff * 2, 60)
                    if verbose:
                        print(f"  Rate limited, waiting {wait}s...")
                    time.sleep(wait)
                    backoff = wait
                    continue
                raise
            except (ConnectionError, OSError) as e:
                conn_errors += 1
                if conn_errors > 5:
                    raise ExecutionError(f"Too many connection errors polling run: {e}")
                wait = min(10 * conn_errors, 30)
                if verbose:
                    print(f"  Connection error ({e.__class__.__name__}), retry in {wait}s...")
                time.sleep(wait)
                continue
            except Exception as e:
                # Catch requests.exceptions.ConnectionError (subclass of IOError)
                if "ConnectionError" in type(e).__name__ or "Connection" in str(type(e)):
                    conn_errors += 1
                    if conn_errors > 5:
                        raise ExecutionError(f"Too many connection errors polling run: {e}")
                    wait = min(10 * conn_errors, 30)
                    if verbose:
                        print(f"  Connection error ({e.__class__.__name__}), retry in {wait}s...")
                    time.sleep(wait)
                    continue
                raise

            status = result.get("status", "unknown")

            if status in ("completed", "failed", "execution_timed_out",
                          "wait_timed_out", "cancelled"):
                return result

            if verbose:
                print(f"  Run status: {status}")

            time.sleep(backoff)
            backoff = DEFAULT_POLL_INTERVAL  # reset after success

        raise ExecutionError(f"Project run polling timed out after {timeout}s")

    # ------------------------------------------------------------------ #
    # CSRF helpers (required for POST/PUT/PATCH/DELETE with API key auth)
    # ------------------------------------------------------------------ #

    def _get_csrf_token(self):
        """Fetch a CSRF token from the auth endpoint."""
        if hasattr(self, "_csrf_token") and self._csrf_token:
            return self._csrf_token
        resp = self.session.get(f"{self.base_url}/auth/csrf-token")
        if resp.status_code == 200:
            data = resp.json()
            self._csrf_token = data.get("token") or data.get("csrfToken")
            return self._csrf_token
        # CSRF may not be required for API key auth on some endpoints
        return None

    def _csrf_headers(self):
        """Get headers with CSRF token if available."""
        token = self._get_csrf_token()
        if token:
            return {"X-CSRF-Token": token}
        return {}

    def _post_with_csrf(self, url, **kwargs):
        """POST with CSRF token."""
        headers = kwargs.pop("headers", {})
        headers.update(self._csrf_headers())
        return self.session.post(url, headers=headers, **kwargs)

    def _put_with_csrf(self, url, **kwargs):
        """PUT with CSRF token."""
        headers = kwargs.pop("headers", {})
        headers.update(self._csrf_headers())
        return self.session.put(url, headers=headers, **kwargs)

    def _patch_with_csrf(self, url, **kwargs):
        """PATCH with CSRF token."""
        headers = kwargs.pop("headers", {})
        headers.update(self._csrf_headers())
        return self.session.patch(url, headers=headers, **kwargs)

    def _delete_with_csrf(self, url, **kwargs):
        """DELETE with CSRF token."""
        headers = kwargs.pop("headers", {})
        headers.update(self._csrf_headers())
        return self.session.delete(url, headers=headers, **kwargs)
