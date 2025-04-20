import os
import json
import time
import shutil
from pathlib import Path
from typing import Dict, List, Any, Optional, Set, Tuple
import pickle
import hashlib
import logging
from dataclasses import dataclass, field, asdict
import uuid
import tempfile

# Create logger
logger = logging.getLogger("QuantumOrchestrator.StateManager")

@dataclass
class Savepoint:
    """Represents a savepoint in the execution pipeline"""
    id: str
    step_id: str
    timestamp: float
    file_changes: Dict[str, str] = field(default_factory=dict)  # path -> hash
    backup_files: Dict[str, str] = field(default_factory=dict)  # path -> backup path
    temp_files: List[str] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)

@dataclass
class FileState:
    """Represents the state of a file"""
    path: str
    exists: bool
    hash: Optional[str] = None
    size: Optional[int] = None
    modified_time: Optional[float] = None

class StateManager:
    """
    Manages the state of the project, including tracking file changes,
    creating savepoints, and handling rollbacks.
    """
    
    def __init__(self, project_root: Path):
        """Initialize the state manager"""
        self.project_root = project_root
        self.state_dir = project_root / ".quantum_state"
        self.savepoints_dir = self.state_dir / "savepoints"
        self.backups_dir = self.state_dir / "backups"
        self.current_state: Dict[str, FileState] = {}
        self.savepoints: Dict[str, Savepoint] = {}
        self.watched_paths: Set[str] = set()
        self.active_savepoints: List[Savepoint] = []
        
        # Create state directory if it doesn't exist
        self._ensure_state_dir()
        
        # Load existing state if available
        self._load_state()
        
        # Load existing savepoints
        self._load_savepoints()
        
        # Track critical project files by default
        self._track_critical_files()
        
        logger.info(f"State manager initialized with {len(self.current_state)} tracked files")
    
    def _ensure_state_dir(self):
        """Ensure the state directory exists"""
        self.state_dir.mkdir(exist_ok=True)
        self.savepoints_dir.mkdir(exist_ok=True)
        self.backups_dir.mkdir(exist_ok=True)
        
        # Create a .gitignore file in the state directory
        gitignore_path = self.state_dir / ".gitignore"
        if not gitignore_path.exists():
            gitignore_path.write_text("*\n!.gitignore\n")
    
    def _load_state(self):
        """Load existing state if available"""
        state_file = self.state_dir / "current_state.pickle"
        if state_file.exists():
            try:
                with open(state_file, "rb") as f:
                    self.current_state = pickle.load(f)
                logger.info(f"Loaded existing state with {len(self.current_state)} tracked files")
            except Exception as e:
                logger.error(f"Error loading state: {e}")
                # Initialize with an empty state
                self.current_state = {}
    
    def _load_savepoints(self):
        """Load existing savepoints"""
        try:
            for sp_file in self.savepoints_dir.glob("*.pickle"):
                with open(sp_file, "rb") as f:
                    savepoint = pickle.load(f)
                    self.savepoints[savepoint.id] = savepoint
            logger.info(f"Loaded {len(self.savepoints)} existing savepoints")
        except Exception as e:
            logger.error(f"Error loading savepoints: {e}")
    
    def save_state(self):
        """Save current state to disk"""
        state_file = self.state_dir / "current_state.pickle"
        try:
            with open(state_file, "wb") as f:
                pickle.dump(self.current_state, f)
            logger.debug(f"Saved state with {len(self.current_state)} tracked files")
            return True
        except Exception as e:
            logger.error(f"Error saving state: {e}")
            return False
    
    def _track_critical_files(self):
        """Track critical project files automatically"""
        # Track Python files in the project
        for py_file in self.project_root.glob("**/*.py"):
            # Skip files in .git, venv, etc.
            if any(part.startswith(".") for part in py_file.parts):
                continue
            # Add to watched paths
            rel_path = str(py_file.relative_to(self.project_root))
            self.watched_paths.add(rel_path)
        
        # Track config files
        for config_file in self.project_root.glob("**/*.json"):
            # Skip files in .git, venv, etc.
            if any(part.startswith(".") for part in config_file.parts):
                continue
            # Add to watched paths
            rel_path = str(config_file.relative_to(self.project_root))
            self.watched_paths.add(rel_path)
        
        # Update state for all watched paths
        self.update_state()
    
    def update_state(self, specific_paths: Optional[List[str]] = None):
        """Update the state of watched files"""
        paths_to_check = specific_paths if specific_paths else list(self.watched_paths)
        
        for rel_path in paths_to_check:
            abs_path = self.project_root / rel_path
            
            if abs_path.exists():
                # Calculate file hash
                try:
                    file_hash = self._calculate_file_hash(abs_path)
                    file_stat = abs_path.stat()
                    
                    # Update state
                    self.current_state[rel_path] = FileState(
                        path=rel_path,
                        exists=True,
                        hash=file_hash,
                        size=file_stat.st_size,
                        modified_time=file_stat.st_mtime
                    )
                except Exception as e:
                    logger.warning(f"Failed to update state for {rel_path}: {e}")
            else:
                # File doesn't exist
                self.current_state[rel_path] = FileState(
                    path=rel_path,
                    exists=False
                )
        
        # Save the updated state
        self.save_state()
    
    def _calculate_file_hash(self, file_path: Path) -> str:
        """Calculate a hash of the file content"""
        hash_md5 = hashlib.md5()
        
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(4096), b""):
                hash_md5.update(chunk)
                
        return hash_md5.hexdigest()
    
    def track_file(self, file_path: str):
        """Track a specific file"""
        rel_path = self._normalize_path(file_path)
        self.watched_paths.add(rel_path)
        self.update_state([rel_path])
    
    def _normalize_path(self, path: str) -> str:
        """Normalize path to be relative to project root"""
        path_obj = Path(path)
        if path_obj.is_absolute():
            try:
                return str(path_obj.relative_to(self.project_root))
            except ValueError:
                raise ValueError(f"Path {path} is outside project root {self.project_root}")
        return path
    
    def create_savepoint(self, step_id: str) -> Savepoint:
        """Create a savepoint for rollback capability"""
        savepoint_id = str(uuid.uuid4())
        savepoint = Savepoint(
            id=savepoint_id,
            step_id=step_id,
            timestamp=time.time(),
            file_changes={},
            backup_files={},
            temp_files=[],
            metadata={"created_at": time.strftime("%Y-%m-%d %H:%M:%S")}
        )
        
        # Store the current state of all tracked files
        for rel_path, file_state in self.current_state.items():
            if file_state.exists:
                savepoint.file_changes[rel_path] = file_state.hash
                
                # Create a backup of the file
                backup_path = self._create_backup(rel_path)
                if backup_path:
                    savepoint.backup_files[rel_path] = backup_path
        
        # Add to active savepoints
        self.active_savepoints.append(savepoint)
        
        logger.info(f"Created savepoint {savepoint_id} for step {step_id} with {len(savepoint.file_changes)} files")
        return savepoint
    
    def _create_backup(self, rel_path: str) -> Optional[str]:
        """Create a backup of a file"""
        src_path = self.project_root / rel_path
        if not src_path.exists():
            return None
        
        # Create a unique backup filename
        backup_name = f"{rel_path.replace('/', '_')}_{int(time.time())}"
        backup_path = self.backups_dir / backup_name
        
        try:
            shutil.copy2(src_path, backup_path)
            return str(backup_path)
        except Exception as e:
            logger.error(f"Failed to create backup of {rel_path}: {e}")
            return None
    
    def register_temp_file(self, rel_path: str, savepoint: Savepoint):
        """Register a temporary file with a savepoint"""
        normalized_path = self._normalize_path(rel_path)
        savepoint.temp_files.append(normalized_path)
        logger.debug(f"Registered temp file {normalized_path} with savepoint {savepoint.id}")
    
    def track_file_change(self, rel_path: str, savepoint: Optional[Savepoint] = None):
        """Track a file change with the most recent savepoint"""
        normalized_path = self._normalize_path(rel_path)
        
        # Update the state for this file
        self.update_state([normalized_path])
        
        # If a savepoint was provided, update it
        if savepoint:
            if normalized_path not in savepoint.file_changes:
                # This is the first change to this file in this savepoint
                file_state = self.current_state.get(normalized_path)
                if file_state and file_state.exists:
                    savepoint.file_changes[normalized_path] = file_state.hash
                    
                    # Create a backup if one doesn't exist
                    if normalized_path not in savepoint.backup_files:
                        backup_path = self._create_backup(normalized_path)
                        if backup_path:
                            savepoint.backup_files[normalized_path] = backup_path
        
        # If no savepoint was provided but we have active savepoints
        elif self.active_savepoints:
            # Update the most recent savepoint
            most_recent = self.active_savepoints[-1]
            self.track_file_change(normalized_path, most_recent)
    
    def commit_savepoint(self, savepoint: Savepoint):
        """Commit a savepoint, making changes permanent"""
        if savepoint in self.active_savepoints:
            # Remove from active savepoints
            self.active_savepoints.remove(savepoint)
            
            # Save the savepoint for historical reference
            self.savepoints[savepoint.id] = savepoint
            self._save_savepoint(savepoint)
            
            # Clean up temporary files
            # In this case we keep them since the operation succeeded
            
            # Clean up backups after a grace period
            self._schedule_backup_cleanup(savepoint)
            
            logger.info(f"Committed savepoint {savepoint.id} for step {savepoint.step_id}")
            return True
        else:
            logger.warning(f"Cannot commit savepoint {savepoint.id}: not active")
            return False
    
    def _save_savepoint(self, savepoint: Savepoint):
        """Save a savepoint to disk"""
        sp_file = self.savepoints_dir / f"{savepoint.id}.pickle"
        try:
            with open(sp_file, "wb") as f:
                pickle.dump(savepoint, f)
            return True
        except Exception as e:
            logger.error(f"Failed to save savepoint {savepoint.id}: {e}")
            return False
    
    def _schedule_backup_cleanup(self, savepoint: Savepoint):
        """Schedule cleanup of backups (in a real implementation, this would use a background job)"""
        # For now, we'll just log that this would happen
        logger.debug(f"Scheduled cleanup of {len(savepoint.backup_files)} backups from savepoint {savepoint.id}")
    
    def rollback_savepoint(self, savepoint: Savepoint) -> bool:
        """Rollback to a savepoint, undoing all changes since its creation"""
        if savepoint not in self.active_savepoints and savepoint.id not in self.savepoints:
            logger.warning(f"Cannot rollback savepoint {savepoint.id}: not found")
            return False
        
        success = True
        
        # Restore files from backups
        for rel_path, backup_path in savepoint.backup_files.items():
            backup = Path(backup_path)
            dest = self.project_root / rel_path
            
            if backup.exists():
                try:
                    # Create directory if it doesn't exist
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    
                    # Copy backup back to original location
                    shutil.copy2(backup, dest)
                    logger.debug(f"Restored {rel_path} from backup")
                except Exception as e:
                    logger.error(f"Failed to restore {rel_path}: {e}")
                    success = False
            else:
                logger.warning(f"Backup for {rel_path} not found at {backup_path}")
                success = False
        
        # Delete temporary files
        for temp_file in savepoint.temp_files:
            try:
                temp_path = self.project_root / temp_file
                if temp_path.exists():
                    if temp_path.is_file():
                        temp_path.unlink()
                    elif temp_path.is_dir():
                        shutil.rmtree(temp_path)
                    logger.debug(f"Deleted temporary file/directory {temp_file}")
            except Exception as e:
                logger.error(f"Failed to delete temporary file {temp_file}: {e}")
                success = False
        
        # Remove from active savepoints
        if savepoint in self.active_savepoints:
            self.active_savepoints.remove(savepoint)
        
        # Update state after rollback
        self.update_state()
        
        if success:
            logger.info(f"Successfully rolled back savepoint {savepoint.id} for step {savepoint.step_id}")
        else:
            logger.warning(f"Partially rolled back savepoint {savepoint.id} for step {savepoint.step_id}")
        
        return success
    
    def get_file_state(self, rel_path: str) -> Optional[FileState]:
        """Get the current state of a file"""
        normalized_path = self._normalize_path(rel_path)
        return self.current_state.get(normalized_path)
    
    def file_exists(self, rel_path: str) -> bool:
        """Check if a file exists"""
        normalized_path = self._normalize_path(rel_path)
        file_state = self.current_state.get(normalized_path)
        if file_state:
            return file_state.exists
        
        # If not in state, check filesystem
        abs_path = self.project_root / normalized_path
        return abs_path.exists()
    
    def create_temp_file(self, prefix="quantum_temp_", suffix=None) -> Tuple[str, Path]:
        """Create a temporary file and return its relative path and absolute path"""
        # Create a temp file in a subdirectory of the project
        temp_dir = self.project_root / ".quantum_temp"
        temp_dir.mkdir(exist_ok=True)
        
        # Create the temp file
        fd, abs_path = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=temp_dir)
        os.close(fd)  # Close the file descriptor
        
        # Get relative path
        rel_path = str(Path(abs_path).relative_to(self.project_root))
        
        return rel_path, Path(abs_path)
    
    def get_file_diffs_since_savepoint(self, savepoint: Savepoint) -> Dict[str, str]:
        """Get a dictionary of files that have changed since the savepoint"""
        changed_files = {}
        
        # Check all files in the savepoint
        for rel_path, original_hash in savepoint.file_changes.items():
            current_state = self.get_file_state(rel_path)
            if not current_state or not current_state.exists:
                changed_files[rel_path] = "deleted"
            elif current_state.hash != original_hash:
                changed_files[rel_path] = "modified"
        
        # Check for new files that weren't in the savepoint
        for rel_path, current_state in self.current_state.items():
            if current_state.exists and rel_path not in savepoint.file_changes:
                changed_files[rel_path] = "created"
        
        return changed_files
    
    def cleanup_old_savepoints(self, max_age_days=7):
        """Clean up old savepoints and their backups"""
        cutoff_time = time.time() - (max_age_days * 86400)
        savepoints_to_remove = []
        
        for sp_id, savepoint in self.savepoints.items():
            if savepoint.timestamp < cutoff_time:
                savepoints_to_remove.append(sp_id)
                
                # Clean up backup files
                for backup_path in savepoint.backup_files.values():
                    try:
                        backup = Path(backup_path)
                        if backup.exists():
                            backup.unlink()
                    except Exception as e:
                        logger.warning(f"Failed to delete backup {backup_path}: {e}")
        
        # Remove savepoints
        for sp_id in savepoints_to_remove:
            try:
                # Delete the savepoint file
                sp_file = self.savepoints_dir / f"{sp_id}.pickle"
                if sp_file.exists():
                    sp_file.unlink()
                
                # Remove from dictionary
                del self.savepoints[sp_id]
            except Exception as e:
                logger.warning(f"Failed to delete savepoint {sp_id}: {e}")
        
        logger.info(f"Cleaned up {len(savepoints_to_remove)} old savepoints")
