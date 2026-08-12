"""Classifier table (plan 04 §4, plan 10 §2.1). ≥30 snippet→class cases."""

from __future__ import annotations

import pytest

from ifc_console.policy.classify import classify
from ifc_console.policy.modes import OpClass

Q, E, S = OpClass.QUERY, OpClass.EDIT, OpClass.SYSTEM

CASES = [
    # -- QUERY ------------------------------------------------------------------
    ("len(ifc.by_type('IfcWall'))", Q),
    ("[w.Name for w in query('IfcWall')]", Q),
    ("print(len(ifc.by_type('IfcWall')))", Q),
    ("import json\nprint(json.dumps({'n': 1}))", Q),
    ("import math\nmath.sqrt(2)", Q),
    ("total = 0\nfor w in ifc.by_type('IfcWall'):\n    total += 1\ntotal", Q),
    ("d = {}\nd['k'] = 1\nd", Q),  # local dict subscript is not a mutation
    ("nums = [1, 2, 3]\nnums[0] = 9\nnums", Q),  # local list subscript
    ("element_util.get_psets(ifc.by_type('IfcWall')[0])", Q),
    ("open('data.txt').read()", Q),  # read-mode open → QUERY (guard path-checks)
    ("x = ifc.by_type('IfcWall')[0]\nx.Name", Q),  # reading an attribute
    # -- EDIT -------------------------------------------------------------------
    ("ifc_api.run('root.create_entity', ifc, ifc_class='IfcWall')", E),
    ("import ifcopenshell.api\nifcopenshell.api.run('x', ifc)", E),
    ("ifc.create_entity('IfcWall')", E),
    ("ifc.remove(ifc.by_type('IfcWall')[0])", E),
    ("ifc.add(other_entity)", E),
    ("w = ifc.by_type('IfcWall')[0]\nw.Name = 'x'", E),
    ("setattr(ifc.by_type('IfcWall')[0], 'Name', 'x')", E),
    ("delattr(wall, 'Name')", E),
    ("del wall.Name", E),
    ("f = get_ifc_file()\nf.create_entity('IfcWall')", E),  # alias via get_ifc_file()
    ("m = ifc\nm.create_entity('IfcWall')", E),  # alias via assignment
    ("ifc.write('out.ifc')", E),
    ("ifc_api.pset.edit_pset(ifc, pset=p, properties={})", E),
    ("e = ifc.by_type('IfcWall')[0]\ne.file.remove(e)", E),  # .file escape hatch
    ("query('IfcWall')[0].file.create_entity('IfcWall')", E),
    # -- SYSTEM -----------------------------------------------------------------
    ("import os\nos.listdir('.')", S),
    ("from os import path", S),
    ("import subprocess", S),
    ("import socket", S),
    ("open('out.txt', 'w')", S),
    ("open('out.txt', 'a')", S),
    ("open(path, mode)", S),  # dynamic mode → SYSTEM
    ("__import__('os')", S),
    ("().__class__.__bases__[0].__subclasses__()", S),
    ("eval('1 + 1')", S),
    ("exec('x = 1')", S),
    ("compile('1', '<s>', 'eval')", S),
    ("getattr(x, '__globals__')", S),
    ("exc.__traceback__.tb_frame.f_globals", S),
    ("getattr(frame, 'f_locals')", S),
    ("import _thread", S),
    ("import sqlite3", S),
    ("import _xxsubinterpreters", S),
    ("import shutil\nshutil.rmtree('/')", S),
    # SYSTEM beats EDIT when both present
    ("import os\nifc.create_entity('IfcWall')", S),
]


@pytest.mark.parametrize("code, expected", CASES, ids=[repr(c[:40]) for c, _ in CASES])
def test_classification(code: str, expected: OpClass) -> None:
    result = classify(code)
    assert result.op_class is expected, (
        f"{code!r} → {result.op_class} (reasons={result.reasons}), expected {expected}"
    )


def test_edit_reasons_are_reported() -> None:
    result = classify("w.Name = 'x'")
    assert result.op_class is E
    assert result.reasons and "assign" in result.reasons[0].lower()


def test_model_serialization_is_identified_separately_from_memory_edits() -> None:
    assert classify("ifc.write('out.ifc')").model_write is True
    assert classify("m = ifc\nm.write('out.ifc')").model_write is True
    assert classify("getattr(ifc, 'write')('out.ifc')").model_write is True
    assert classify("w.Name = 'x'").model_write is False
    assert classify("buf.write('memory only')").model_write is False


def test_system_reasons_are_reported() -> None:
    result = classify("import os")
    assert result.op_class is S
    assert any("os" in r for r in result.reasons)


def test_syntax_error_propagates() -> None:
    with pytest.raises(SyntaxError):
        classify("for x in :")


def test_extra_system_modules_are_honored() -> None:
    assert classify("import numpy").op_class is Q  # not system by default
    assert classify("import numpy", extra_system_modules=("numpy",)).op_class is S
