"""Pruebas para los Quality Gates clínicos y de sistema."""

from pathlib import Path

from bitacora.gates import run_all_gates, run_clinical_safety_gate
from bitacora.schema import load_bitacora


def test_clinical_safety_gate() -> None:
    bitacora = load_bitacora(Path("data/bitacora_2026_11_09.json"))
    res = run_clinical_safety_gate(bitacora)

    assert res.aprobado
    assert res.nombre == "Clinical Safety Gate"


def test_run_all_gates_suite() -> None:
    root = Path.cwd()
    act = Path("data/bitacora_2026_11_10.json")
    ant = Path("data/bitacora_2026_11_09.json")

    suite_res = run_all_gates(bitacora_path=act, project_root=root, anterior_path=ant)
    assert suite_res.aprobado_global
    assert len(suite_res.checks) >= 3
