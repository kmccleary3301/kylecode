"""
Dialect Manager - Modular dialect selection system for different models
Provides configuration-driven filtering of tool calling dialects
"""
from __future__ import annotations

import logging
from typing import Dict, List, Optional, Any
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

@dataclass
class DialectProfile:
    """Configuration for dialect selection per model"""
    name: str
    preferred_dialects: Dict[str, str]  # operation_type -> dialect_id
    exclusions: List[str]  # dialect_ids to disable
    mode: str = "mixed"  # exclusive|mixed|fallback
    
    def __post_init__(self):
        """Validate profile configuration"""
        valid_modes = {"exclusive", "mixed", "fallback"}
        if self.mode not in valid_modes:
            raise ValueError(f"Invalid mode '{self.mode}'. Must be one of: {valid_modes}")

class DialectManager:
    """Manages dialect selection based on model profiles"""
    
    def __init__(self, config: Dict[str, Any]):
        """
        Initialize dialect manager from configuration
        
        Args:
            config: Full configuration dict containing optional 'dialect_profiles' section
        """
        self.config = config
        self.profiles = self._load_profiles(config)
        self.default_profile = DialectProfile(
            name="default",
            preferred_dialects={},
            exclusions=[],
            mode="mixed"
        )
        logger.info(f"Initialized DialectManager with {len(self.profiles)} profiles")
    
    def _load_profiles(self, config: Dict[str, Any]) -> Dict[str, DialectProfile]:
        """Load dialect profiles from configuration"""
        profiles = {}
        
        profile_configs = config.get("dialect_profiles", {})
        for model_id, profile_config in profile_configs.items():
            try:
                profile = DialectProfile(
                    name=model_id,
                    preferred_dialects=profile_config.get("preferred_dialects", {}),
                    exclusions=profile_config.get("exclusions", []),
                    mode=profile_config.get("mode", "mixed")
                )
                profiles[model_id] = profile
                logger.debug(f"Loaded profile for {model_id}: mode={profile.mode}, "
                           f"exclusions={profile.exclusions}")
            except Exception as e:
                logger.error(f"Failed to load profile for {model_id}: {e}")
                
        return profiles
    
    def get_dialects_for_model(self, model_id: str, requested_dialects: List[str]) -> List[str]:
        """
        Filter dialects based on model profile
        
        Args:
            model_id: ID of the model (e.g., 'gpt-5-nano', 'claude-3-sonnet')
            requested_dialects: List of dialect IDs to filter
            
        Returns:
            Filtered list of dialect IDs based on model profile
        """
        profile = self.profiles.get(model_id, self.default_profile)
        
        logger.debug(f"Filtering dialects for {model_id} using profile '{profile.name}' "
                    f"(mode={profile.mode})")
        
        if profile.mode == "exclusive":
            # Only allow preferred dialects or those not explicitly excluded
            allowed = []
            for dialect in requested_dialects:
                if dialect in profile.exclusions:
                    continue
                if profile.preferred_dialects:
                    # If preferences specified, only allow preferred ones
                    if dialect in profile.preferred_dialects.values():
                        allowed.append(dialect)
                else:
                    # No preferences, allow anything not excluded
                    allowed.append(dialect)
            
            logger.debug(f"Exclusive mode: {len(requested_dialects)} -> {len(allowed)} dialects")
            return allowed
        
        elif profile.mode == "mixed":
            # Default behavior - filter exclusions only
            filtered = [d for d in requested_dialects if d not in profile.exclusions]
            logger.debug(f"Mixed mode: excluded {len(requested_dialects) - len(filtered)} dialects")
            return filtered
        
        elif profile.mode == "fallback":
            # Use preferred dialects if available, otherwise fall back to filtered set
            if profile.preferred_dialects:
                preferred = [d for d in requested_dialects 
                           if d in profile.preferred_dialects.values()]
                if preferred:
                    return preferred
            # Fall back to mixed mode
            return [d for d in requested_dialects if d not in profile.exclusions]
        
        # Unknown mode, return original list
        logger.warning(f"Unknown profile mode '{profile.mode}' for {model_id}")
        return requested_dialects
    
    def get_profile(self, model_id: str) -> DialectProfile:
        """Get profile for a specific model"""
        return self.profiles.get(model_id, self.default_profile)
    
    def has_profile(self, model_id: str) -> bool:
        """Check if a model has a specific profile configured"""
        return model_id in self.profiles
    
    def get_operation_dialect(self, model_id: str, operation_type: str) -> Optional[str]:
        """
        Get preferred dialect for a specific operation type
        
        Args:
            model_id: Model identifier
            operation_type: Type of operation (e.g., 'file_creation', 'file_editing')
            
        Returns:
            Preferred dialect ID or None if no preference
        """
        profile = self.profiles.get(model_id, self.default_profile)
        return profile.preferred_dialects.get(operation_type)
    
    def explain_filtering(self, model_id: str, requested_dialects: List[str]) -> Dict[str, Any]:
        """
        Explain how dialects would be filtered for debugging/logging
        
        Returns:
            Dict with filtering explanation including original, filtered, and reasoning
        """
        profile = self.profiles.get(model_id, self.default_profile)
        filtered_dialects = self.get_dialects_for_model(model_id, requested_dialects)
        
        return {
            "model_id": model_id,
            "profile_name": profile.name,
            "profile_mode": profile.mode,
            "requested_dialects": requested_dialects,
            "filtered_dialects": filtered_dialects,
            "excluded_dialects": [d for d in requested_dialects if d not in filtered_dialects],
            "exclusion_rules": profile.exclusions,
            "preferred_dialects": profile.preferred_dialects
        }