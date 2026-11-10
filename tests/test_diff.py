"""Pruebas del motor de diferencias clínicas (Diff Engine)."""

from pathlib import Path

from bitacora.diff_engine import compare_bitacoras
from bitacora.schema import load_bitacora


def test_diff_two_days() -> None:
    ant = load_bitacora(Path("data/bitacora_2026_11_09.json"))
    act = load_bitacora(Path("data/bitacora_2026_11_10.json"))

    diff = compare_bitacoras(ant, act)

    # Debe detectar que LOPEZ PAZMINO (6D nuevo) ingresó
    assert len(diff.ingresos_nuevos) == 1
    assert diff.ingresos_nuevos[0].cama == "6D"
    assert diff.ingresos_nuevos[0].hc == "2199041"

    # Debe detectar que INTRIAGO (6D anterior) egresó
    assert len(diff.egresos_o_traslados) == 1
    assert diff.egresos_o_traslados[0].hc == "1249372"

    # Paciente 6A debe tener pendientes resueltos y antibiótico avanzado a 5/5
    pac_6a_diff = next(p for p in diff.pacientes_modificados if p.cama == "6A")
    assert len(pac_6a_diff.pendientes_resueltos) >= 2
    assert any("5/5" in c for c in pac_6a_diff.antibioticos_cambios)
