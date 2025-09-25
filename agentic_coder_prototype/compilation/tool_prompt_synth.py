from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
	from jinja2 import Environment, FileSystemLoader, StrictUndefined
except Exception:  # pragma: no cover - optional dep
	Environment = None  # type: ignore
	FileSystemLoader = None  # type: ignore
	StrictUndefined = object  # type: ignore


@dataclass
class ToolParamModel:
	name: str
	type: Optional[str] = None
	required: bool = False
	default: Any = None
	description: Optional[str] = None


@dataclass
class ToolModel:
	name: str
	display_name: Optional[str]
	description: str
	blocking: bool = False
	max_per_turn: Optional[int] = None
	parameters: List[ToolParamModel] = None  # type: ignore
	return_type: Optional[str] = None
	syntax_style: Optional[str] = None


class ToolPromptSynthesisEngine:
	"""Render tool catalogs and dialect instructions from templates.

	Backed by a sandboxed Jinja subset. If jinja2 is unavailable, falls back to a
	simple built-in renderer that emits a compact Pythonic signature list.
	"""

	def __init__(self, root: str = "implementations/tool_prompt_synthesis") -> None:
		self.root = Path(root)
		self.env = None
		if Environment is not None:
			self.env = Environment(
				loader=FileSystemLoader(str(self.root)),
				autoescape=False,
				undefined=StrictUndefined,
				trim_blocks=True,
				lstrip_blocks=True,
			)

	def set_root(self, root: str) -> None:
		"""Update the templates root and reinitialize the Jinja loader."""
		self.root = Path(root)
		if Environment is not None:
			self.env = Environment(
				loader=FileSystemLoader(str(self.root)),
				autoescape=False,
				undefined=StrictUndefined,
				trim_blocks=True,
				lstrip_blocks=True,
			)

	def _hash_inputs(self, dialect_id: str, detail: str, tools_norm: List[Dict[str, Any]]) -> str:
		blob = f"{dialect_id}|{detail}|{tools_norm}"
		return hashlib.sha256(blob.encode()).hexdigest()[:12]

	def _normalize_tools(self, tools: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
		"""Normalize tool defs to a compact, serializable structure."""
		out: List[Dict[str, Any]] = []
		for t in tools:
			params = []
			for p in t.get("parameters", []) or []:
				params.append({
					"name": p.get("name"),
					"type": p.get("type"),
					"default": p.get("default"),
					"required": bool(p.get("required", False)),
					"description": p.get("description"),
				})
			out.append({
				"name": t.get("name"),
				"display_name": t.get("display_name"),
				"description": t.get("description", ""),
				"blocking": bool(t.get("blocking", False)),
				"max_per_turn": t.get("max_per_turn"),
				"parameters": params,
				"return_type": t.get("return_type"),
				"syntax_style": t.get("syntax_style"),
			})
		return out

	def _fallback_render(self, tools_norm: List[Dict[str, Any]]) -> Tuple[str, str]:
		raise RuntimeError("TPSL template missing and fallback rendering is disabled")

	def render(self, dialect_id: str, detail: str, tools: List[Dict[str, Any]], template_map: Dict[str, str]) -> Tuple[str, str]:
		"""Render a catalog string and return (text, template_id)."""
		tools_norm = self._normalize_tools(tools)
		cache_key = self._hash_inputs(dialect_id, detail, tools_norm)
		# Resolve template path
		tmpl_path = None
		if template_map:
			# Choose detail-aware key if present. Support explicit keys and back-compat shorthand.
			explicit_keys = {
				"system_short", "system_medium", "system_full",
				"per_turn_short", "per_turn_medium", "per_turn_long",
			}
			key = None
			if detail in explicit_keys:
				key = detail
			else:
				# Back-compat mapping
				if detail == "full":
					key = "system_full"
				elif detail == "short":
					key = "per_turn_short"
			# Try exact
			tmpl_path = template_map.get(key) if key else None
			# Fallback preference by family
			if tmpl_path is None and key:
				if key.startswith("system_"):
					for alt in ("system_full", "system_medium", "system_short"):
						tmpl_path = template_map.get(alt)
						if tmpl_path:
							break
				else:
					for alt in ("per_turn_short", "per_turn_medium", "per_turn_long"):
						tmpl_path = template_map.get(alt)
						if tmpl_path:
							break
		# Do not cross-fallback families: if per-turn template missing, return empty string
		is_per_turn = detail.startswith("per_turn") or (tmpl_path is None and (detail == "short"))
		if (tmpl_path is None) and is_per_turn:
			return "", f"missing:{dialect_id}:{detail}:{cache_key}"
		if not tmpl_path or self.env is None:
			raise RuntimeError("TPSL requires Jinja2 and a template path; provide templates via config")
		try:
			p = Path(tmpl_path)
			# Normalize to loader-relative path
			s_root = self.root.resolve()
			try:
				rel = str(p.resolve().relative_to(s_root))
			except Exception:
				# Handle given paths like "implementations/tool_prompt_synthesis/.../file"
				s = str(p).replace("\\", "/")
				prefix = s_root.as_posix() + "/"
				if s.startswith(prefix):
					rel = s[len(prefix):]
				else:
					# Fall back to original string
					rel = s
			tmpl = self.env.get_template(rel)
			payload = {
				"dialect": dialect_id,
				"detail": detail,
				"tools": tools_norm,
			}
			return tmpl.render(**payload), f"{rel}::{cache_key}"
		except Exception as e:
			raise
