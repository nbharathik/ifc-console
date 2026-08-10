"""Print a compact JSON report for one IFC model."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ifc_console import Workbench


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("model", type=Path)
    parser.add_argument("--home", type=Path, default=None)
    args = parser.parse_args()

    with Workbench.open(args.model, home=args.home) as workbench:
        validation = workbench.validation_result()
        report = {
            "model": workbench.model,
            "project": workbench.info()["project"],
            "wall_rows_returned": len(workbench.query("IfcWall", limit=500)),
            "valid": validation.valid,
            "issue_count": validation.issue_count,
            "revision_id": workbench.context.active_model.revision_id,
        }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
