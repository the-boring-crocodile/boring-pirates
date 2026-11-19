"""Literal Refused/Failed causes in src; dynamic calls have short reviewed exceptions."""
import ast
from pathlib import Path
from boring_outcomes import OUR_CODES

EXTRA_CODES = {
    "pirate_failed": "an unexpected local Pirate operation failed; consult its log",
    "save_to_failed": "the service answered but saving its answer failed or is uncertain",
    "save_to_refused": "the service answered but saving its answer was refused",
}
DYNAMIC_CODES = {
    ("src/boring_pirates/common/assignment.py", "save"):
        "selected from the finite SAVE_PROBLEMS causes after a service answer",
    ("src/boring_pirates/common/ipc.py", "code"):
        "refusal wrapper forwards its caller-owned cause",
}


def test_local_cause_vocabulary():
    root = Path(__file__).resolve().parents[1]
    paths = sorted(root.glob("src/**/*.py"))
    assert paths and not OUR_CODES.keys() & EXTRA_CODES.keys()
    allowed, reviewed = OUR_CODES.keys() | EXTRA_CODES.keys(), set()
    for path in paths:
        for call in ast.walk(ast.parse(path.read_text(), filename=str(path))):
            if not isinstance(call, ast.Call):
                continue
            name = (call.func.id if isinstance(call.func, ast.Name) else
                    call.func.attr if isinstance(call.func, ast.Attribute) else "")
            if name not in ("Refused", "Failed"):
                continue
            code = next((kw.value for kw in call.keywords if kw.arg == "code"),
                        call.args[0] if call.args else None)
            where = f"{path.relative_to(root)}:{call.lineno}"
            if isinstance(code, ast.Constant):
                assert isinstance(code.value, str) and code.value in allowed, where
            else:
                key = (str(path.relative_to(root)), ast.unparse(code))
                assert key in DYNAMIC_CODES and DYNAMIC_CODES[key], where
                reviewed.add(key)
    assert reviewed == DYNAMIC_CODES.keys()
