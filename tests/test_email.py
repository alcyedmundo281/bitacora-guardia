"""Pruebas del generador de recordatorios por email para la visita nocturna."""

from pathlib import Path

from bitacora.email_notifier import extract_night_tasks, generate_email_content
from bitacora.schema import load_bitacora


def test_extract_night_tasks() -> None:
    bitacora = load_bitacora(Path("data/bitacora_2026_11_09.json"))
    tasks = extract_night_tasks(bitacora)

    assert len(tasks) > 0
    # Los STAT deben estar primeros en orden de prioridad
    assert tasks[0].prioridad == "STAT"

    # Verificar que tareas críticas nocturnas estén presentes
    descripciones = [t.descripcion for t in tasks]
    assert any("Gasometría" in d for d in descripciones)
    assert any("ELECTROLITOS" in d for d in descripciones)


def test_generate_email_html_and_text() -> None:
    bitacora = load_bitacora(Path("data/bitacora_2026_11_09.json"))
    subject, text, html = generate_email_content(bitacora)

    assert "Recordatorio de Visita Nocturna" in subject
    assert "RECORDATORIO DE PENDIENTES" in text
    assert "<!DOCTYPE html>" in html
    assert "Visita Nocturna de Guardia" in html
    assert "RODRIGUEZ MORENO" in html
