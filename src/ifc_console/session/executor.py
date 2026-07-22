"""execute_ifc_code mechanics: REPL semantics, stdout capture, tracebacks.

The final bare expression's value is returned like a REPL (matching Jupyter):
the body is compiled without the trailing Expr, which is compiled in 'eval'
mode separately.
"""

from __future__ import annotations

import ast
import contextlib
import io
import re
import traceback
from dataclasses import dataclass
from types import CodeType
from typing import Any


@dataclass
class CompiledCode:
    body: CodeType
    last_expr: CodeType | None


@dataclass
class ExecResult:
    stdout: str
    result_repr: str | None
    stdout_truncated: bool = False


def prepare(code: str, filename: str = "<execute_ifc_code>") -> CompiledCode:
    """Compile source with REPL semantics. Raises SyntaxError."""
    tree = ast.parse(code, filename=filename)
    last_expr: CodeType | None = None
    if tree.body and isinstance(tree.body[-1], ast.Expr):
        expr = ast.Expression(tree.body[-1].value)
        ast.copy_location(expr.body, tree.body[-1])
        last_expr = compile(expr, filename, "eval")
        tree.body = tree.body[:-1]
    body = compile(tree, filename, "exec")
    return CompiledCode(body=body, last_expr=last_expr)


def run(compiled: CompiledCode, namespace: dict[str, Any], *, output_limit: int) -> ExecResult:
    """Execute in `namespace`, capturing stdout. Exceptions propagate."""
    buffer = io.StringIO()
    result: Any = None
    with contextlib.redirect_stdout(buffer):
        exec(compiled.body, namespace)  # noqa: S102 - this is the product
        if compiled.last_expr is not None:
            result = eval(compiled.last_expr, namespace)  # noqa: S307
    stdout = buffer.getvalue()
    truncated = len(stdout) > output_limit
    if truncated:
        stdout = stdout[:output_limit] + "\n…[stdout truncated]"
    result_repr: str | None = None
    if result is not None:
        result_repr = repr(result)
        if len(result_repr) > output_limit:
            result_repr = result_repr[:output_limit] + "…[truncated]"
    return ExecResult(stdout=stdout, result_repr=result_repr, stdout_truncated=truncated)


_FILE_LINE = re.compile(r'File "([^"]+)"')


def format_traceback(exc: BaseException, max_frames: int = 3) -> str:
    """Trimmed traceback: header + last frames, paths redacted to basenames."""
    frames = traceback.format_exception(type(exc), exc, exc.__traceback__)
    if len(frames) > max_frames + 1:
        frames = [frames[0], f"  …[{len(frames) - max_frames - 1} frames hidden]…\n", *frames[-max_frames:]]
    text = "".join(frames)
    return _FILE_LINE.sub(lambda m: f'File "{m.group(1).replace(chr(92), "/").rsplit("/", 1)[-1]}"', text)
