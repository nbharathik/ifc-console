"""Static AST classifier for execute_ifc_code.

Purpose: routing (which gate applies) and explanation (what the error
reports). It is NOT the security boundary; guards.py is. Bias: false
EDIT/SYSTEM positives are acceptable (cost: an explained rejection in ask
mode); false negatives are what the runtime guards catch.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass

from ifc_console.policy.modes import OpClass

SYSTEM_MODULES = {
    "os",
    "sys",
    "subprocess",
    "shutil",
    "pathlib",
    "socket",
    "http",
    "urllib",
    "requests",
    "ftplib",
    "ctypes",
    "multiprocessing",
    "threading",
    "importlib",
    "builtins",
    "pickle",
    "marshal",
    "tempfile",
    "webbrowser",
    "asyncio",
    "_thread",
    "sqlite3",
    "mmap",
    "fcntl",
    "gc",
    "_xxsubinterpreters",
}

MUTATING_FILE_METHODS = {
    "create_entity",
    "add",
    "remove",
    "batch",
    "unbatch",
    "assign_inverse",
    "unassign_inverse",
}

DUNDER_ESCAPES = {
    "__globals__",
    "__subclasses__",
    "__bases__",
    "__mro__",
    "__code__",
    "__builtins__",
    "__loader__",
    "__spec__",
    "__dict__",
    "__traceback__",
    "__closure__",
}

INTROSPECTION_ESCAPES = {
    "tb_frame",
    "f_back",
    "f_globals",
    "f_locals",
    "gi_frame",
    "cr_frame",
    "ag_frame",
}


@dataclass
class Classification:
    op_class: OpClass
    reasons: list[str]


def classify(
    code: str,
    *,
    model_names: tuple[str, ...] = ("ifc",),
    extra_system_modules: tuple[str, ...] = (),
) -> Classification:
    """Classify source. Raises SyntaxError on unparseable code."""
    tree = ast.parse(code)
    visitor = _Visitor(set(model_names), SYSTEM_MODULES | set(extra_system_modules))
    visitor.visit(tree)
    if visitor.system:
        return Classification(OpClass.SYSTEM, _dedupe(visitor.system + visitor.edit))
    if visitor.edit:
        return Classification(OpClass.EDIT, _dedupe(visitor.edit))
    return Classification(OpClass.QUERY, [])


def _dedupe(items: list[str]) -> list[str]:
    seen: dict[str, None] = {}
    for it in items:
        seen.setdefault(it)
    return list(seen)


def _snippet(node: ast.AST) -> str:
    try:
        text = ast.unparse(node)
    except Exception:
        return "<code>"
    return text if len(text) <= 60 else text[:57] + "..."


class _Visitor(ast.NodeVisitor):
    def __init__(self, model_names: set[str], system_modules: set[str]) -> None:
        self.model_names = model_names
        self.system_modules = system_modules
        self.edit: list[str] = []
        self.system: list[str] = []

    # -- helpers ------------------------------------------------------------
    def _attr_chain(self, node: ast.AST) -> tuple[str | None, list[str]]:
        """('root_name', ['a','b']) for root.a.b; root None if base isn't a Name."""
        parts: list[str] = []
        cur = node
        while isinstance(cur, ast.Attribute):
            parts.append(cur.attr)
            cur = cur.value
        parts.reverse()
        if isinstance(cur, ast.Name):
            return cur.id, parts
        return None, parts

    def _base_is_model(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Name):
            return node.id in self.model_names
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            return node.func.id == "get_ifc_file"
        return False

    # -- E3/E4: attribute assignment / deletion ------------------------------
    def _flag_assign_target(self, target: ast.AST) -> None:
        if isinstance(target, ast.Attribute):
            self.edit.append(f"assigns attribute: {_snippet(target)}")
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                self._flag_assign_target(elt)
        elif isinstance(target, ast.Starred):
            self._flag_assign_target(target.value)

    def visit_Assign(self, node: ast.Assign) -> None:
        # alias tracking: x = ifc / x = get_ifc_file()
        if self._base_is_model(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self.model_names.add(t.id)
        for t in node.targets:
            self._flag_assign_target(t)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._flag_assign_target(node.target)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self._flag_assign_target(node.target)
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        for t in node.targets:
            if isinstance(t, ast.Attribute) or (
                isinstance(t, ast.Subscript) and isinstance(t.value, ast.Attribute)
            ):
                self.edit.append(f"deletes attribute: {_snippet(t)}")
        self.generic_visit(node)

    # -- S1: imports ----------------------------------------------------------
    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            top = alias.name.split(".")[0]
            if top in self.system_modules:
                self.system.append(f"imports {top}")
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        top = (node.module or "").split(".")[0]
        if top in self.system_modules:
            self.system.append(f"imports {top}")
        self.generic_visit(node)

    # -- calls: E1/E2/E5, S2/S3/S4 --------------------------------------------
    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        if isinstance(func, ast.Name):
            name = func.id
            if name in ("eval", "exec", "compile"):
                self.system.append(f"calls {name}()")
            elif name == "__import__":
                self.system.append("calls __import__")
            elif name == "setattr":
                self.edit.append(f"calls setattr: {_snippet(node)}")
            elif name == "delattr":
                self.edit.append(f"calls delattr: {_snippet(node)}")
            elif name == "getattr":
                if (
                    len(node.args) >= 2
                    and isinstance(node.args[1], ast.Constant)
                    and isinstance(node.args[1].value, str)
                    and (
                        node.args[1].value.startswith("__")
                        or node.args[1].value in INTROSPECTION_ESCAPES
                    )
                ):
                    self.system.append(f"dunder getattr: {_snippet(node)}")
            elif name == "open":
                self._check_open(node)
        elif isinstance(func, ast.Attribute):
            root, path = self._attr_chain(func)
            if root == "ifc_api":
                self.edit.append(f"calls ifc_api.{'.'.join(path)}")
            elif root == "ifcopenshell" and path and path[0] == "api":
                self.edit.append(f"calls ifcopenshell.{'.'.join(path)}")
            elif self._base_is_model(func.value) and func.attr in MUTATING_FILE_METHODS:
                self.edit.append(f"calls model.{func.attr}(...)")
            elif (
                isinstance(func.value, ast.Attribute)
                and func.value.attr == "file"
                and func.attr in MUTATING_FILE_METHODS
            ):
                # entity.file hands back the raw model; mutating through it
                # is an edit whatever the base expression is
                self.edit.append(f"calls .file.{func.attr}(...)")
            if func.attr == "write":
                self.edit.append(f"calls .write(): {_snippet(node)}")
        self.generic_visit(node)

    def _check_open(self, node: ast.Call) -> None:
        mode_node: ast.AST | None = None
        if len(node.args) >= 2:
            mode_node = node.args[1]
        for kw in node.keywords:
            if kw.arg == "mode":
                mode_node = kw.value
        if mode_node is None:
            return  # default read mode; runtime open-guard still path-checks it
        if isinstance(mode_node, ast.Constant) and isinstance(mode_node.value, str):
            if any(c in mode_node.value for c in "wax+"):
                self.system.append(f"opens file for writing: {_snippet(node)}")
        else:
            self.system.append("opens file with a dynamic mode")

    # -- S3: dunder escapes -----------------------------------------------------
    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr in DUNDER_ESCAPES or node.attr in INTROSPECTION_ESCAPES:
            self.system.append(f"introspection access: .{node.attr}")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == "__builtins__":
            self.system.append("touches __builtins__")
        self.generic_visit(node)
