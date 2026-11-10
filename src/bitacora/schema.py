"""Gestión de esquemas JSON y validación estricta de Bitácoras."""

import json
from pathlib import Path

from bitacora.models import BitacoraGuardia


def export_json_schema(output_path: Path) -> Path:
    """Genera el archivo JSON Schema a partir de los modelos Pydantic."""
    schema = BitacoraGuardia.model_json_schema()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(schema, indent=2, ensure_ascii=False), encoding="utf-8")
    return output_path


def load_bitacora(file_path: Path) -> BitacoraGuardia:
    """Carga y valida un archivo JSON de bitácora."""
    data = json.loads(file_path.read_text(encoding="utf-8"))
    return BitacoraGuardia.model_validate(data)


def save_bitacora(bitacora: BitacoraGuardia, file_path: Path) -> Path:
    """Persiste la bitácora en JSON formateado."""
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text(
        bitacora.model_dump_json(indent=2, by_alias=True),
        encoding="utf-8",
    )
    return file_path
