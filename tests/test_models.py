"""Pruebas de modelos Pydantic de la Bitácora Médica."""

from pathlib import Path

from bitacora.models import BitacoraGuardia
from bitacora.schema import export_json_schema, load_bitacora


def test_load_sample_bitacora() -> None:
    path = Path("data/bitacora_2026_11_09.json")
    bitacora = load_bitacora(path)

    assert isinstance(bitacora, BitacoraGuardia)
    assert bitacora.version_schema == "1.0.0"
    assert len(bitacora.pacientes) >= 6
    assert bitacora.censo.hospitalizados == 18

    # Verificar paciente 6A
    paciente_6a = next(p for p in bitacora.pacientes if p.cama == "6A")
    assert paciente_6a.hc == "2013397"
    assert any(pend.prioridad.value == "STAT" for pend in paciente_6a.pendientes)


def test_export_schema(tmp_path: Path) -> None:
    schema_file = tmp_path / "schema.json"
    p = export_json_schema(schema_file)
    assert p.exists()
    content = p.read_text(encoding="utf-8")
    assert "BitacoraGuardia" in content
    assert "cama" in content
