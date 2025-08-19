"""
Simplified agentic coder prototype.

This module provides a streamlined interface to the complex agent system,
abstracting away implementation details.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Optional
from pathlib import Path

from .agent_llm_openai import AgentOpenAI
from .provider_routing import provider_router
from .provider_adapters import provider_adapter_manager
from .tool_calling.tool_yaml_loader import load_yaml_tools
from .tool_calling.system_prompt_compiler import get_compiler


class AgenticCoder:
    """Simplified agentic coder interface."""
    
    def __init__(self, config_path: str, workspace_dir: Optional[str] = None):
        """Initialize the agentic coder with a config file."""
        self.config_path = config_path
        self.workspace_dir = workspace_dir or f"agent_ws_{os.path.basename(config_path).split('.')[0]}"
        self.config = self._load_config()
        self.agent = None
        
    def _load_config(self) -> Dict[str, Any]:
        """Load and validate configuration."""
        with open(self.config_path, 'r') as f:
            return json.load(f) if self.config_path.endswith('.json') else __import__('yaml').safe_load(f)
    
    def initialize(self) -> None:
        """Initialize the agent with the loaded configuration."""
        # Create workspace if it doesn't exist
        Path(self.workspace_dir).mkdir(exist_ok=True)
        
        # Initialize the underlying agent
        self.agent = AgentOpenAI(
            config=self.config,
            workspace_dir=self.workspace_dir
        )
    
    def run_task(self, task: str, max_iterations: Optional[int] = None) -> Dict[str, Any]:
        """Run a single task and return results."""
        if not self.agent:
            self.initialize()
        
        return self.agent.run_agent_session(
            initial_prompt=task,
            max_iterations=max_iterations or self.config.get('max_iterations', 20)
        )
    
    def interactive_session(self) -> None:
        """Start an interactive session with the agent."""
        if not self.agent:
            self.initialize()
        
        print(f"Starting interactive session in {self.workspace_dir}")
        print("Type 'exit' to quit")
        
        while True:
            try:
                user_input = input("\n> ")
                if user_input.lower() in ['exit', 'quit']:
                    break
                
                result = self.agent.run_agent_session(initial_prompt=user_input, max_iterations=5)
                print(f"Agent completed with status: {result.get('completion_reason', 'unknown')}")
                
            except KeyboardInterrupt:
                print("\nSession interrupted by user")
                break
            except Exception as e:
                print(f"Error: {e}")
    
    def get_workspace_files(self) -> List[str]:
        """Get list of files in the agent workspace."""
        if not Path(self.workspace_dir).exists():
            return []
        
        files = []
        for root, _, filenames in os.walk(self.workspace_dir):
            for filename in filenames:
                files.append(os.path.relpath(os.path.join(root, filename), self.workspace_dir))
        return files


def create_agent(config_path: str, workspace_dir: Optional[str] = None) -> AgenticCoder:
    """Convenient factory function to create an agentic coder."""
    return AgenticCoder(config_path, workspace_dir)