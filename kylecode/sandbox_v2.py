import os
import shlex
import subprocess
import uuid
import fnmatch
import shutil
import stat as pystat
from typing import Any, Iterable, Optional

import ray

from adaptive_iter import encode_adaptive_iterable


def sandbox_env(image: str, mount_src: Optional[str] = None, network: str = "none"):
    opts: list[str] = [f"--network={network}"] if network else []
    if mount_src:
        opts.append(f"--mount=type=bind,src={mount_src},dst=/workspace,readonly=false")
    runtime = os.environ.get("RAY_DOCKER_RUNTIME", "runsc")
    return {
        "docker": {
            "image": image,
            "runtime": runtime,
            "options": opts,
        }
    }


@ray.remote
class DevSandboxV2:
    def __init__(self, image: str, session_id: Optional[str] = None, workspace: str = "/workspace", lsp_actor: Optional[ray.actor.ActorHandle] = None):
        self.image = image
        self.session_id = session_id or str(uuid.uuid4())
        self.workspace = workspace
        self.lsp = lsp_actor
        os.makedirs(self.workspace, exist_ok=True)

    # --- Path helpers
    def _resolve_path(self, path: str) -> str:
        """
        Resolve a path relative to the workspace and ensure it stays inside.
        """
        # Special case: "/" should refer to workspace root
        if path == "/":
            return os.path.normpath(self.workspace)
        
        ws = os.path.normpath(self.workspace)
        
        if os.path.isabs(path):
            # Check if path already starts with workspace (avoid duplication)
            if path.startswith(ws):
                full = os.path.normpath(path)
            else:
                # Strip leading slash and treat as relative to workspace
                path = path.lstrip("/")
                full = os.path.normpath(os.path.join(ws, path))
        else:
            full = os.path.normpath(os.path.join(ws, path))
            
        if not full.startswith(ws + os.sep) and full != ws:
            raise ValueError(f"Path escapes workspace: {path}")
        return full

    # --- File operations
    def put(self, path: str, content: bytes) -> None:
        full = self._resolve_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "wb") as f:
            f.write(content)

    def get(self, path: str) -> bytes:
        full = self._resolve_path(path)
        with open(full, "rb") as f:
            return f.read()

    def ls(self, path: str = ".", depth: int = 1) -> dict[str, Any]:
        """List directory contents with optional tree structure"""
        full = self._resolve_path(path)
        
        if depth == 1:
            # Simple listing
            try:
                items = sorted(os.listdir(full))
                return {
                    "path": path,
                    "items": items,
                    "depth": 1,
                    "tree_format": False
                }
            except (OSError, FileNotFoundError):
                return {
                    "path": path,
                    "items": [],
                    "depth": 1,
                    "tree_format": False,
                    "error": "Directory not found or inaccessible"
                }
        else:
            # Tree structure with depth > 1
            depth = min(max(depth, 1), 5)  # Cap depth between 1-5
            tree_structure = self._build_tree_structure(full, depth)
            return {
                "path": path,
                "tree_structure": tree_structure,
                "depth": depth,
                "tree_format": True
            }
    
    def _build_tree_structure(self, root_path: str, max_depth: int, current_depth: int = 0) -> list[dict]:
        """Build tree structure for directory listing"""
        if current_depth >= max_depth:
            return []
        
        items = []
        try:
            entries = sorted(os.listdir(root_path))
            for entry in entries:
                full_entry_path = os.path.join(root_path, entry)
                is_dir = os.path.isdir(full_entry_path)
                
                item = {
                    "name": entry,
                    "is_dir": is_dir,
                    "level": current_depth
                }
                
                if is_dir and current_depth + 1 < max_depth:
                    # Recursively add subdirectory contents
                    item["children"] = self._build_tree_structure(
                        full_entry_path, max_depth, current_depth + 1
                    )
                
                items.append(item)
        except (OSError, PermissionError):
            pass
        
        return items

    def exists(self, path: str) -> bool:
        try:
            full = self._resolve_path(path)
            return os.path.exists(full)
        except ValueError:
            return False

    def stat(self, path: str) -> dict[str, Any]:
        full = self._resolve_path(path)
        s = os.stat(full)
        return {
            "size": s.st_size,
            "mtime": s.st_mtime,
            "mode": s.st_mode,
            "is_dir": pystat.S_ISDIR(s.st_mode),
            "is_file": pystat.S_ISREG(s.st_mode),
        }

    def read_text(self, path: str, offset: Optional[int] = None, limit: Optional[int] = None, encoding: str = "utf-8") -> dict:
        full = self._resolve_path(path)
        with open(full, "r", encoding=encoding, errors="replace") as f:
            if offset is None and limit is None:
                content = f.read()
                return {"path": full, "content": content}
            # line-based window
            lines = f.readlines()
            start = max(offset or 0, 0)
            end = len(lines) if limit is None else min(start + max(limit, 0), len(lines))
            window = "".join(lines[start:end])
            return {"path": full, "content": window, "offset": start, "limit": (None if limit is None else end - start)}

    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> dict:
        full = self._resolve_path(path)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding=encoding) as f:
            f.write(content)
        s = os.stat(full)
        # LSP touch
        if self.lsp:
            try:
                ray.get(self.lsp.touch_file.remote(full, True))
            except Exception:
                pass
        return {"path": full, "size": s.st_size, "mtime": s.st_mtime}

    def edit_replace(self, path: str, old: str, new: str, count: int = 1, encoding: str = "utf-8") -> dict:
        full = self._resolve_path(path)
        with open(full, "r", encoding=encoding, errors="replace") as f:
            data = f.read()
        replaced = data.replace(old, new, count if count >= 0 else -1)
        num = data.count(old) if count < 0 else min(data.count(old), count)
        if replaced != data:
            with open(full, "w", encoding=encoding) as f:
                f.write(replaced)
        if self.lsp:
            try:
                ray.get(self.lsp.touch_file.remote(full, True))
            except Exception:
                pass
        return {"path": full, "replacements": num}

    def multiedit(self, edits: list[dict], encoding: str = "utf-8") -> dict:
        """
        Apply multiple replacements: [{"path","old","new","count"}]. Returns per-file stats.
        """
        results: list[dict] = []
        for e in edits:
            res = self.edit_replace(e["path"], e.get("old", ""), e.get("new", ""), int(e.get("count", 1)), encoding)
            results.append(res)
        if self.lsp and results:
            try:
                # touch all edited files
                futures = []
                for r in results:
                    futures.append(self.lsp.touch_file.remote(r["path"], True))
                ray.get(futures)
            except Exception:
                pass
        return {"results": results}

    def glob(self, pattern: str, root: str = ".", limit: Optional[int] = None) -> list[str]:
        base = self._resolve_path(root)
        out: list[str] = []
        for dirpath, dirnames, filenames in os.walk(base):
            rel_dir = os.path.relpath(dirpath, base)
            for name in filenames + dirnames:
                rel = name if rel_dir == "." else os.path.join(rel_dir, name)
                if fnmatch.fnmatch(rel, pattern):
                    out.append(os.path.join(base, rel))
                    if limit and len(out) >= limit:
                        return out
        return out

    def list_files(self, root: str = ".", include_hidden: bool = False, recursive: bool = True, limit: int = 1000) -> list[str]:
        base = self._resolve_path(root)
        results: list[str] = []
        if recursive:
            for dirpath, dirnames, filenames in os.walk(base):
                if not include_hidden:
                    dirnames[:] = [d for d in dirnames if not d.startswith(".")]
                    filenames = [f for f in filenames if not f.startswith(".")]
                for name in filenames:
                    results.append(os.path.join(dirpath, name))
                    if len(results) >= limit:
                        return results
        else:
            for name in os.listdir(base):
                if not include_hidden and name.startswith("."):
                    continue
                full = os.path.join(base, name)
                if os.path.isfile(full):
                    results.append(full)
                    if len(results) >= limit:
                        return results
        return results

    def grep(self, pattern: str, path: str = ".", include: Optional[str] = None, limit: int = 100) -> dict:
        """
        Try ripgrep if available in the container; fallback to Python scanning.
        Returns a dict with matches and truncated flag.
        """
        base = self._resolve_path(path)
        rg = shutil.which("rg")
        matches: list[dict] = []

        if rg:
            args = [rg, "-n", pattern, base]
            if include:
                args = [rg, "-n", "--glob", include, base]
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            stdout, stderr = proc.communicate()
            if proc.returncode not in (0, 1):
                return {"error": f"ripgrep failed: {stderr.strip()}"}
            for line in stdout.splitlines():
                if not line:
                    continue
                parts = line.split(":", 2)
                if len(parts) < 3:
                    continue
                fpath, lno, text = parts[0], parts[1], parts[2]
                if include and not fnmatch.fnmatch(os.path.relpath(fpath, base), include):
                    continue
                matches.append({"path": fpath, "line": int(lno), "text": text})
                if len(matches) >= limit:
                    return {"matches": matches, "truncated": True}
            return {"matches": matches, "truncated": False}

        # Fallback python search
        for dirpath, _, filenames in os.walk(base):
            for fname in filenames:
                fpath = os.path.join(dirpath, fname)
                if include and not fnmatch.fnmatch(os.path.relpath(fpath, base), include):
                    continue
                try:
                    with open(fpath, "r", encoding="utf-8", errors="ignore") as f:
                        for idx, line in enumerate(f, start=1):
                            if pattern in line:
                                matches.append({"path": fpath, "line": idx, "text": line.rstrip("\n")})
                                if len(matches) >= limit:
                                    return {"matches": matches, "truncated": True}
                except Exception:
                    continue
        return {"matches": matches, "truncated": False}

    # --- Command execution (adaptive streaming)
    def run(
        self,
        cmd: str,
        timeout: Optional[int] = None,
        stdin_data: Optional[str] = None,
        env: Optional[dict[str, str]] = None,
        stream: bool = True,
        shell: bool = True,
    ) -> Iterable[Any]:
        """
        Execute a command. If stream=True, returns an adaptive-encoded generator with
        line-by-line stdout+stderr. If stream=False, returns a single dict result encoded
        as NON-ITERABLE.
        """
        merged_env = os.environ.copy()
        if env:
            merged_env.update(env)

        if not shell:
            argv = shlex.split(cmd)
        else:
            argv = cmd

        proc = subprocess.Popen(
            argv,
            cwd=self.workspace,
            shell=shell,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=merged_env,
        )

        def _stream_iter():
            try:
                if stdin_data is not None and proc.stdin:
                    proc.stdin.write(stdin_data)
                    proc.stdin.flush()
                    proc.stdin.close()
            except Exception:
                pass

            if proc.stdout is not None:
                for line in proc.stdout:
                    yield line.rstrip("\n")
            # Wait for exit
            try:
                return_code = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                return_code = -9
            yield {"exit": return_code}

        if stream:
            # Materialize to a list so Ray returns a concrete object rather than a generator
            return list(encode_adaptive_iterable(_stream_iter()))

        # Non-streaming path: capture output fully
        try:
            out, _ = proc.communicate(input=stdin_data, timeout=timeout)
            exit_code = proc.returncode
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()
            exit_code = -9
        result = {"stdout": out, "exit": exit_code}
        return list(encode_adaptive_iterable(result))

    # --- Metadata
    def get_session_id(self) -> str:
        return self.session_id

    def get_workspace(self) -> str:
        return self.workspace

    # --- Git/VCS integration
    def _git_available(self) -> bool:
        return shutil.which("git") is not None

    def _run_git(self, args: list[str], input_text: Optional[str] = None) -> tuple[int, str, str]:
        proc = subprocess.Popen(
            ["git", *args],
            cwd=self.workspace,
            stdin=subprocess.PIPE if input_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        stdout, stderr = proc.communicate(input=input_text)
        return proc.returncode, stdout, stderr

    def _ensure_git_repo(self, user: Optional[dict[str, str]] = None) -> None:
        if not self._git_available():
            raise RuntimeError("git is not available in this sandbox image")
        # Detect repo
        code, _, _ = self._run_git(["rev-parse", "--git-dir"]) 
        if code != 0:
            # init
            code, out, err = self._run_git(["init"])
            if code != 0:
                raise RuntimeError(f"git init failed: {err.strip()}")
            # Set safe directory (for root or shared mounts)
            self._run_git(["config", "--global", "safe.directory", self.workspace])
        # Basic identity
        if user and user.get("name"):
            self._run_git(["config", "user.name", user["name"]])
        if user and user.get("email"):
            self._run_git(["config", "user.email", user["email"]])

    def _collect_rejects(self) -> dict[str, str]:
        rejects: dict[str, str] = {}
        for dirpath, _, filenames in os.walk(self.workspace):
            for name in filenames:
                if name.endswith(".rej"):
                    full = os.path.join(dirpath, name)
                    try:
                        with open(full, "r", encoding="utf-8", errors="replace") as f:
                            rejects[os.path.relpath(full, self.workspace)] = f.read()
                    except Exception:
                        continue
        return rejects

    def _parse_status_porcelain(self, text: str) -> dict[str, list[str]]:
        modified: list[str] = []
        added: list[str] = []
        deleted: list[str] = []
        untracked: list[str] = []
        for line in text.splitlines():
            if not line:
                continue
            status = line[:2]
            path = line[3:] if len(line) > 3 else ""
            if status == "??":
                untracked.append(path)
            elif "D" in status:
                deleted.append(path)
            elif "A" in status:
                added.append(path)
            else:
                modified.append(path)
        return {"modified": modified, "added": added, "deleted": deleted, "untracked": untracked}

    def git_status(self) -> dict[str, Any]:
        self._ensure_git_repo()
        code, out, err = self._run_git(["status", "--porcelain"])
        ok = code == 0
        summary = self._parse_status_porcelain(out) if ok else {}
        return {"ok": ok, "action": "status", "exit": code, "stdout": out, "stderr": err, "data": summary}

    def git_diff(self, paths: Optional[list[str]] = None, staged: bool = False, unified: int = 3) -> dict[str, Any]:
        self._ensure_git_repo()
        args = ["diff", f"-U{unified}"]
        if staged:
            args.insert(1, "--staged")
        if paths:
            args.extend(paths)
        code, out, err = self._run_git(args)
        return {"ok": code == 0, "action": "diff", "exit": code, "stdout": out, "stderr": err, "data": {"diff": out}}

    def git_add(self, paths: Optional[list[str]] = None) -> dict[str, Any]:
        self._ensure_git_repo()
        args = ["add"]
        if paths:
            args.extend(paths)
        else:
            args.append("-A")
        code, out, err = self._run_git(args)
        return {"ok": code == 0, "action": "add", "exit": code, "stdout": out, "stderr": err}

    def git_commit(self, message: str, author: Optional[str] = None) -> dict[str, Any]:
        self._ensure_git_repo()
        args = ["commit", "-m", message]
        env = os.environ.copy()
        if author:
            env["GIT_AUTHOR_NAME"] = author
            env["GIT_COMMITTER_NAME"] = author
        # Run commit
        proc = subprocess.Popen(["git", *args], cwd=self.workspace, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env)
        out, err = proc.communicate()
        # Get hash if success
        commit_hash = None
        if proc.returncode == 0:
            code2, out2, _ = self._run_git(["rev-parse", "HEAD"])
            if code2 == 0:
                commit_hash = out2.strip()
        return {"ok": proc.returncode == 0, "action": "commit", "exit": proc.returncode, "stdout": out, "stderr": err, "data": {"commit": commit_hash}}

    def git_apply_patch(
        self,
        unified_diff: str,
        three_way: bool = True,
        index: bool = True,
        whitespace: str = "fix",  # nowarn|fix|warn|error
        reverse: bool = False,
        keep_rejects: bool = True,
        check: bool = False,
    ) -> dict[str, Any]:
        """
        Apply a unified diff using git. Returns structured result with change summary and rejects.
        """
        self._ensure_git_repo()
        args = ["apply"]
        if index:
            args.append("--index")
        if three_way:
            args.append("--3way")
        if reverse:
            args.append("--reverse")
        if check:
            args.append("--check")
        if whitespace:
            args.extend(["--whitespace", whitespace])

        code, out, err = self._run_git(args, input_text=unified_diff)

        # Collect status and rejects
        status = self.git_status()
        rejects = self._collect_rejects() if keep_rejects else {}
        ok = code == 0 and status.get("ok", False)
        data = {"status": status.get("data", {}), "rejects": rejects}
        # Touch modified files in LSP
        if self.lsp and data["status"]:
            try:
                futures = []
                for group in ("modified", "added", "deleted"):
                    for p in data["status"].get(group, []):
                        full = os.path.join(self.workspace, p)
                        futures.append(self.lsp.touch_file.remote(full, True))
                if futures:
                    ray.get(futures)
            except Exception:
                pass
        return {"ok": ok, "action": "apply_patch", "exit": code, "stdout": out, "stderr": err, "data": data}

    def vcs(self, request: dict[str, Any]) -> dict[str, Any]:
        """
        Generic VCS endpoint for flexibility and forward compatibility.
        Schema (request):
          - action: one of [init, status, diff, add, commit, apply_patch]
          - params: dict of action-specific params
          - user: optional {name, email} for init/identity
        Returns a uniform envelope: {ok, action, exit, stdout, stderr, data, errors?}
        """
        action = request.get("action")
        params = request.get("params", {}) or {}
        user = request.get("user")
        if action in ("init",):
            self._ensure_git_repo(user)
            return {"ok": True, "action": "init", "exit": 0, "stdout": "", "stderr": "", "data": {}}
        if action == "status":
            return self.git_status()
        if action == "diff":
            return self.git_diff(paths=params.get("paths"), staged=bool(params.get("staged", False)), unified=int(params.get("unified", 3)))
        if action == "add":
            return self.git_add(paths=params.get("paths"))
        if action == "commit":
            return self.git_commit(message=params.get("message", ""), author=params.get("author"))
        if action == "apply_patch":
            return self.git_apply_patch(
                unified_diff=params.get("patch", ""),
                three_way=bool(params.get("three_way", True)),
                index=bool(params.get("index", True)),
                whitespace=str(params.get("whitespace", "fix")),
                reverse=bool(params.get("reverse", False)),
                keep_rejects=bool(params.get("keep_rejects", True)),
                check=bool(params.get("check", False)),
            )
        return {"ok": False, "action": action or "", "exit": 1, "stdout": "", "stderr": f"unknown action: {action}", "data": {}}


# Convenience factory
def new_dev_sandbox_v2(
    image: str,
    mount_src: str,
    network: str = "none",
    name: Optional[str] = None,
):
    use_docker = os.environ.get("RAY_USE_DOCKER_SANDBOX", "1") == "1"
    if use_docker:
        env = sandbox_env(image=image, mount_src=mount_src, network=network)
        return DevSandboxV2.options(runtime_env=env, name=name).remote(image=image, workspace="/workspace")
    # Fallback to local host workspace without docker runtime
    return DevSandboxV2.options(name=name).remote(image=image, workspace=mount_src)


