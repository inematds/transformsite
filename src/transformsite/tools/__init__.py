"""Registro de ferramentas. Cada ferramenta é uma função Python com metadados.

Ferramentas do projeto: crie `tools/<nome>.py` com uma função `register(registry)` que chama
`registry.add("minha.tool", fn, side_effects=True, description="...")`.
A função recebe `(ctx, **args)` e devolve um dict.
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    fn: Callable[..., Any]
    side_effects: bool = False
    description: str = ""
    timeout: float = 30.0


@dataclass
class ToolContext:
    cfg: Any
    store: Any
    session: dict = field(default_factory=dict)
    service: str = ""


class ToolError(Exception):
    pass


class Registry(dict):
    def add(self, name: str, fn, side_effects=False, description="", timeout=30.0):
        self[name] = ToolSpec(name, fn, side_effects, description, timeout)
        return fn

    def call(self, tool: str, ctx: ToolContext, /, **args) -> Any:
        spec = self.get(tool)
        if spec is None:
            raise ToolError(f"ferramenta desconhecida: {tool}")
        return spec.fn(ctx, **args)


def default_registry(cfg=None) -> Registry:
    from . import agenda, builtin

    reg = Registry()
    builtin.register(reg)
    agenda.register(reg, cfg)
    if cfg is not None:
        load_project_tools(reg, cfg.tools_dir)
    return reg


def load_project_tools(reg: Registry, tools_dir: Path) -> None:
    if not tools_dir.exists():
        return
    for p in sorted(tools_dir.glob("*.py")):
        if p.name.startswith("_"):
            continue
        spec = importlib.util.spec_from_file_location(f"transformsite_project_tools.{p.stem}", p)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)  # type: ignore[union-attr]
        if hasattr(mod, "register"):
            mod.register(reg)
