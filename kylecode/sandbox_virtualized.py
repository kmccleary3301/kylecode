"""
Virtualized Sandbox - Path Virtualization Layer
Provides complete workspace isolation while maintaining backward compatibility.
"""

import os
import shutil
import uuid
import tempfile
from pathlib import Path
from typing import Optional, Dict, Any, List, Tuple
from enum import Enum
import ray
import logging

logger = logging.getLogger(__name__)

class DeploymentMode(Enum):
    """Deployment modes for different isolation levels"""
    DEVELOPMENT = "development"    # Mirror to visible directory for testing
    TESTING = "testing"           # Isolated but extractable
    PRODUCTION = "production"     # Fully contained volumes

class SessionManager:
    """Manages isolated session directories"""
    
    def __init__(self, base_sessions_dir: str = "/tmp/agent_sessions"):
        self.sessions_dir = Path(base_sessions_dir)
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.active_sessions: Dict[str, Path] = {}
        
    def create_session(self, session_id: Optional[str] = None) -> tuple[str, str]:
        """Create a new isolated session directory"""
        if session_id is None:
            session_id = str(uuid.uuid4())
            
        session_path = self.sessions_dir / session_id / "workspace"
        session_path.mkdir(parents=True, exist_ok=True)
        
        self.active_sessions[session_id] = session_path
        logger.info(f"Created isolated session: {session_id} at {session_path}")
        
        return session_id, str(session_path)
        
    def get_session_path(self, session_id: str) -> Optional[str]:
        """Get the real filesystem path for a session"""
        if session_id in self.active_sessions:
            return str(self.active_sessions[session_id])
        return None
        
    def cleanup_session(self, session_id: str):
        """Clean up session directory"""
        if session_id in self.active_sessions:
            session_base = self.active_sessions[session_id].parent
            try:
                shutil.rmtree(session_base)
                del self.active_sessions[session_id]
                logger.info(f"Cleaned up session: {session_id}")
            except Exception as e:
                logger.warning(f"Failed to cleanup session {session_id}: {e}")
                
    def list_sessions(self) -> List[str]:
        """List all active sessions"""
        return list(self.active_sessions.keys())

@ray.remote
class VirtualizedSandbox:
    """
    Virtualized sandbox that provides path isolation and translation.
    Models see only relative virtual paths, while real operations happen in isolated directories.
    """
    
    def __init__(self, 
                 base_sandbox: ray.ObjectRef,
                 session_id: str,
                 mode: DeploymentMode = DeploymentMode.DEVELOPMENT,
                 mirror_path: Optional[str] = None):
        """
        Initialize virtualized sandbox
        
        Args:
            base_sandbox: Ray reference to underlying DevSandboxV2
            session_id: Unique session identifier
            mode: Deployment mode for isolation level
            mirror_path: Optional path for test-time mirroring
        """
        self.base_sandbox = base_sandbox
        self.session_id = session_id
        self.mode = mode
        self.mirror_path = Path(mirror_path) if mirror_path else None
        
        print("VIRTUALIZED SANDBOX MIRROR PATH", mirror_path)
        
        # Create mirroring directory if needed
        if self.mirror_path and mode == DeploymentMode.DEVELOPMENT:
            self.mirror_path.mkdir(parents=True, exist_ok=True)
            logger.info(f"Mirroring enabled to: {self.mirror_path}")
            print("VIRTUALIZED SANDBOX MIRROR PATH CREATED:", self.mirror_path)
        # Capture underlying workspace to normalize paths
        try:
            ws = ray.get(self.base_sandbox.get_workspace.remote())
            self._base_ws_abs = str(Path(ws).resolve())
            self._base_ws_name = Path(self._base_ws_abs).name
        except Exception:
            self._base_ws_abs = None
            self._base_ws_name = None
        # Cache names for normalization with mirror
        self._mirror_name = self.mirror_path.name if self.mirror_path else None
            
    def _virtualize_path(self, virtual_path: str) -> str:
        """Convert virtual paths into workspace-relative real paths without duplicate prefixes."""
        try:
            raw = str(virtual_path or "").strip()
            if not raw:
                return ""

            # Absolute paths: resolve relative to workspace when possible
            try:
                candidate = Path(raw)
                if candidate.is_absolute() and self._base_ws_abs:
                    base_path = Path(self._base_ws_abs)
                    try:
                        rel = candidate.resolve(strict=False).relative_to(base_path)
                        parts = list(rel.parts)
                    except Exception:
                        parts = [seg for seg in candidate.parts if seg and seg != base_path.anchor]
                else:
                    raise ValueError
            except Exception:
                normalized = raw.replace("\\", "/")
                while normalized.startswith("./"):
                    normalized = normalized[2:]
                parts = [seg for seg in normalized.split('/') if seg and seg != "."]

            # Remove duplicate workspace or mirror prefixes (agent_ws/.../agent_ws/...)
            def _strip_leading(matches: Tuple[str, ...], items: List[str]) -> List[str]:
                out = list(items)
                changed = True
                while out and changed:
                    changed = False
                    head = out[0]
                    if head in matches:
                        out.pop(0)
                        changed = True
                return out

            matches = tuple(filter(None, {self._base_ws_name, self._mirror_name}))
            parts = _strip_leading(matches, parts)

            # Collapse directory backtracks to keep within workspace
            cleaned: List[str] = []
            for seg in parts:
                if seg == "..":
                    if cleaned:
                        cleaned.pop()
                    continue
                if seg == ".":
                    continue
                cleaned.append(seg)

            return "/".join(cleaned)
        except Exception:
            return str(virtual_path or "")
    
    def _devirtualize_path(self, real_path: str) -> str:
        """
        Convert real path back to virtual path for model.
        This ensures the model always sees clean relative paths.
        """
        # Extract just the relative portion that the model should see
        # If real_path is absolute, extract the workspace-relative portion
        if os.path.isabs(real_path):
            # Try to find the workspace portion and return relative path
            parts = Path(real_path).parts
            if 'workspace' in parts:
                workspace_idx = parts.index('workspace')
                relative_parts = parts[workspace_idx + 1:]
                if relative_parts:
                    return str(Path(*relative_parts))
                else:
                    return "."
            else:
                # Fallback: just return the filename
                return Path(real_path).name
        else:
            # Already relative, ensure it starts with ./
            if not real_path.startswith('./'):
                return f"./{real_path}"
            return real_path
    
    def _mirror_file(self, virtual_path: str, content: str = None):
        """Mirror file to test-time visible directory"""
        if not self.mirror_path or self.mode != DeploymentMode.DEVELOPMENT:
            return
            
        try:
            print("MIRROR FILE VIRTUAL PATH", virtual_path)
            
            clean_path = self._virtualize_path(virtual_path)
            # Normalize away a leaked workspace or mirror root at the head
            try:
                parts = list(Path(clean_path).parts)
            except Exception:
                parts = []
            while parts:
                head = parts[0]
                if self._base_ws_name and head == self._base_ws_name:
                    parts = parts[1:]
                    continue
                if self._mirror_name and head == self._mirror_name:
                    parts = parts[1:]
                    continue
                break
            normalized_rel = Path(*parts) if parts else Path("")
            mirror_file = (self.mirror_path / normalized_rel) if (normalized_rel.parts) else self.mirror_path
            mirror_file.parent.mkdir(parents=True, exist_ok=True)
            
            print("MIRROR FILE CLEAN_PATH", clean_path)
            print("MIRROR FILE MIRROR_FILE", mirror_file)
            
            if content is not None:
                # Write content directly
                mirror_file.write_text(content)
            else:
                # Copy from real location
                real_result = ray.get(self.base_sandbox.get.remote(clean_path))
                if isinstance(real_result, bytes):
                    mirror_file.write_bytes(real_result)
                else:
                    mirror_file.write_text(str(real_result))
                    
            logger.debug(f"Mirrored {virtual_path} to {mirror_file}")
            
        except Exception as e:
            logger.warning(f"Failed to mirror {virtual_path}: {e}")
    
    def _mirror_directory_structure(self):
        """Mirror entire directory structure for test-time visibility"""
        if not self.mirror_path or self.mode != DeploymentMode.DEVELOPMENT:
            return
        
        print("MIRROR DIRECTORY STRUCTURE WITH MIRROR PATH", self.mirror_path)
        
        try:
            # Get directory listing (depth=1)
            listing = ray.get(self.base_sandbox.ls.remote(".", 1))
            if isinstance(listing, dict):
                items = listing.get("items", [])
            else:
                items = listing or []
            for name in items:
                try:
                    rel = self._virtualize_path(name)
                    if ray.get(self.base_sandbox.exists.remote(rel)):
                        stat_info = ray.get(self.base_sandbox.stat.remote(rel))
                        if stat_info.get("is_file", False):
                            self._mirror_file(rel)
                except Exception as e:
                    logger.debug(f"Failed to mirror {name}: {e}")
                    
        except Exception as e:
            logger.warning(f"Failed to mirror directory structure: {e}")

    # === Virtualized API Methods ===
    
    def _prefix_items(self, items: List[str], base_path: str) -> List[str]:
        """Apply the virtual path prefix when listing nested directories."""
        if base_path in (".", "./", ""):
            return list(items)
        prefix = base_path.rstrip("/") + "/"
        return [f"{prefix}{item}" for item in items]

    def ls(self, path: str = ".", depth: int = 1) -> Dict[str, Any]:
        """List directory contents - mirrors base sandbox structure with virtualized paths."""
        clean_path = self._virtualize_path(path)

        print("VIRTUALIZED SANDBOX LS:", clean_path, "depth=", depth)

        base_result = ray.get(self.base_sandbox.ls.remote(clean_path, depth))

        if isinstance(base_result, dict):
            result = dict(base_result)
            result["path"] = path
            if result.get("tree_format"):
                # Tree structures already encode relative names; just ensure path is mirrored
                return result
            items = result.get("items", []) or []
            result["items"] = self._prefix_items(items, path)
            return result

        # Fallback: base sandbox returned a simple list
        items = base_result or []
        items = self._prefix_items(items, path)
        return {
            "path": path,
            "items": items,
            "depth": depth,
            "tree_format": False,
        }
    
    def exists(self, path: str) -> bool:
        """Check if file exists"""
        clean_path = self._virtualize_path(path)
        return ray.get(self.base_sandbox.exists.remote(clean_path))
    
    def stat(self, path: str) -> dict[str, Any]:
        """Get file stats"""
        clean_path = self._virtualize_path(path)
        return ray.get(self.base_sandbox.stat.remote(clean_path))
    
    def read_text(self, path: str, offset: Optional[int] = None, 
                  limit: Optional[int] = None, encoding: str = "utf-8") -> dict:
        """Read text file - returns virtual path"""
        clean_path = self._virtualize_path(path)
        result = ray.get(self.base_sandbox.read_text.remote(clean_path, offset, limit, encoding))
        
        # Virtualize the path in response
        if "path" in result:
            result["path"] = self._devirtualize_path(result["path"])
            
        return result
    
    def write_text(self, path: str, content: str, encoding: str = "utf-8") -> dict:
        """Write text file - handles virtual paths and mirroring"""
        clean_path = self._virtualize_path(path)
        result = ray.get(self.base_sandbox.write_text.remote(clean_path, content, encoding))
        
        # Virtualize the path in response
        if "path" in result:
            result["path"] = self._devirtualize_path(result["path"])
        
        # Mirror for test-time visibility
        self._mirror_file(path, content)
        
        # Add virtualization metadata
        result["virtual_path"] = path
        result["session_id"] = self.session_id
        
        return result
    
    def edit_replace(self, path: str, old: str, new: str, count: int = 1, encoding: str = "utf-8") -> dict:
        """Edit file with find/replace - handles virtual paths"""
        clean_path = self._virtualize_path(path)
        result = ray.get(self.base_sandbox.edit_replace.remote(clean_path, old, new, count, encoding))
        
        # Virtualize path
        if "path" in result:
            result["path"] = self._devirtualize_path(result["path"])
        
        # Mirror updated file
        self._mirror_file(path)
        
        result["virtual_path"] = path
        return result
    
    def multiedit(self, edits: list[dict], encoding: str = "utf-8") -> dict:
        """Apply multiple edits - handles virtual paths"""
        # Clean up paths in edits
        clean_edits = []
        for edit in edits:
            clean_edit = edit.copy()
            if "path" in clean_edit:
                clean_edit["path"] = self._virtualize_path(clean_edit["path"])
            clean_edits.append(clean_edit)
        
        result = ray.get(self.base_sandbox.multiedit.remote(clean_edits, encoding))
        
        # Virtualize paths in results
        if "results" in result:
            for res in result["results"]:
                if "path" in res:
                    res["path"] = self._devirtualize_path(res["path"])
        
        # Mirror affected files
        for edit in edits:
            if "path" in edit:
                self._mirror_file(edit["path"])
        
        return result
    
    def run(self, cmd: str, timeout: Optional[int] = None, 
            stdin_data: Optional[str] = None, **kwargs) -> dict:
        """Execute command in sandbox"""
        result = ray.get(self.base_sandbox.run.remote(cmd, timeout, stdin_data, **kwargs))
        
        # Mirror any output files that might have been created
        if self.mode == DeploymentMode.DEVELOPMENT:
            try:
                self._mirror_directory_structure()
            except Exception as e:
                logger.debug(f"Failed to auto-mirror after command: {e}")
        
        return result
    
    # === Session Management ===
    
    def get_session_info(self) -> dict:
        """Get session information"""
        return {
            "session_id": self.session_id,
            "mode": self.mode.value,
            "mirror_path": str(self.mirror_path) if self.mirror_path else None,
            "virtual_root": "./"
        }
    
    def export_session(self, target_path: str) -> dict:
        """Export session contents to target directory"""
        target = Path(target_path)
        target.mkdir(parents=True, exist_ok=True)
        
        try:
            # Get all files in session
            files = ray.get(self.base_sandbox.ls.remote("."))
            exported_files = []
            
            for file in files:
                try:
                    if ray.get(self.base_sandbox.exists.remote(file)):
                        stat_info = ray.get(self.base_sandbox.stat.remote(file))
                        if stat_info.get("is_file", False):
                            content = ray.get(self.base_sandbox.get.remote(file))
                            target_file = target / file
                            target_file.parent.mkdir(parents=True, exist_ok=True)
                            
                            if isinstance(content, bytes):
                                target_file.write_bytes(content)
                            else:
                                target_file.write_text(str(content))
                                
                            exported_files.append(file)
                            
                except Exception as e:
                    logger.warning(f"Failed to export {file}: {e}")
            
            return {
                "exported_to": str(target),
                "files_exported": exported_files,
                "count": len(exported_files)
            }
            
        except Exception as e:
            return {"error": f"Export failed: {e}"}
    
    def cleanup(self):
        """Cleanup sandbox resources"""
        logger.info(f"Cleaning up virtualized sandbox session: {self.session_id}")
        
        # Final mirror if in development mode
        if self.mode == DeploymentMode.DEVELOPMENT:
            try:
                self._mirror_directory_structure()
            except Exception as e:
                logger.debug(f"Failed final mirror: {e}")

class SandboxFactory:
    """Factory for creating virtualized sandboxes with different deployment modes"""
    
    def __init__(self):
        self.session_manager = SessionManager()
    
    def create_sandbox(
        self, 
        mode: DeploymentMode,
        config: dict,
        base_sandbox_class=None
    ) -> tuple[ray.ObjectRef, str]:
        """
        Create a virtualized sandbox based on deployment mode
        
        Returns:
            tuple: (virtualized_sandbox_ref, session_id)
        """
        # Create session
        session_id, real_workspace = self.session_manager.create_session()
        
        # Import here to avoid circular dependencies
        from kylecode.sandbox_v2 import DevSandboxV2, sandbox_env
        if base_sandbox_class is None:
            base_sandbox_class = DevSandboxV2
            
        # Create base sandbox with isolated workspace
        image = config.get("runtime", {}).get("image", "python-dev:latest")
        
        if mode == DeploymentMode.PRODUCTION:
            # Production: Use container volumes
            env = sandbox_env(image, mount_src=None, network="none")
        else:
            # Development/Testing: Mount session directory
            env = sandbox_env(image, mount_src=real_workspace, network="none")
        
        base_sandbox = base_sandbox_class.options(
            runtime_env=env,
            name=f"base-sandbox-{session_id}"
        ).remote(
            image=image,
            session_id=session_id,
            workspace="/workspace" if mode == DeploymentMode.PRODUCTION else real_workspace
        )
        
        # Determine mirror path for development mode
        mirror_path = None
        if mode == DeploymentMode.DEVELOPMENT:
            # Use configured workspace for mirroring
            workspace_config = config.get("workspace", "./agent_ws")
            if not Path(workspace_config).is_absolute():
                mirror_path = str(Path.cwd() / workspace_config)
            else:
                mirror_path = workspace_config
            print("MIRROR PATH", mirror_path)
        
        # Create virtualized wrapper
        virtualized_sandbox = VirtualizedSandbox.remote(
            base_sandbox=base_sandbox,
            session_id=session_id,
            mode=mode,
            mirror_path=mirror_path
        )
        
        logger.info(f"Created {mode.value} sandbox: {session_id}")
        return virtualized_sandbox, session_id
    
    def cleanup_session(self, session_id: str):
        """Cleanup a session"""
        self.session_manager.cleanup_session(session_id)
    
    def list_sessions(self) -> List[str]:
        """List active sessions"""
        return self.session_manager.list_sessions()

# Convenience functions
def create_development_sandbox(config: dict) -> tuple[ray.ObjectRef, str]:
    """Create a development sandbox with mirroring"""
    factory = SandboxFactory()
    return factory.create_sandbox(DeploymentMode.DEVELOPMENT, config)

def create_production_sandbox(config: dict) -> tuple[ray.ObjectRef, str]:
    """Create a production sandbox with full isolation"""
    factory = SandboxFactory()
    return factory.create_sandbox(DeploymentMode.PRODUCTION, config)
