"""The code follows docs/api-layers.md: dependencies only point downward.

Layer 1 (api) -> layer 2 (client) -> layer 3 (pty_bridge -> debugger); the
listener (serve) and the target share only the wire module (protocol). This
AST-scans every intra-package import, deferred in-function ones included, so
a layer can never reach up or skip a layer without failing the gate.
"""

import ast
import pathlib

LIB = pathlib.Path(__file__).resolve().parent.parent / "reverse_ipdb"

ALLOWED = {
    "__init__": {"api"},
    "api": {"client"},                     # layer 1 -> layer 2
    "client": {"protocol", "pty_bridge"},  # layer 2 -> the wire, layer 3
    "pty_bridge": {"debugger"},            # layer 3, deferred
    "debugger": set(),
    "protocol": set(),
    "serve": {"protocol"},                 # the peer: only the wire is shared
    "agent": {"protocol"},                 # the peer for a program: the wire too
    "mcp_server": {"agent"},               # MCP tools over the agent's listener
}


def _imports(tree):
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("reverse_ipdb."):
                    yield alias.name.split(".")[1]
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "reverse_ipdb":
                yield from (alias.name for alias in node.names)
            elif node.module.startswith("reverse_ipdb."):
                yield node.module.split(".")[1]


def test_every_module_is_listed_and_imports_only_downward():
    modules = {path.stem for path in LIB.glob("*.py")}
    assert modules == set(ALLOWED), "update ALLOWED when adding a module"
    for name in sorted(modules):
        found = set(_imports(ast.parse((LIB / f"{name}.py").read_text())))
        assert found <= ALLOWED[name], f"{name} imports {found - ALLOWED[name]}: a layer jump"
