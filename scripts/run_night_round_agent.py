#!/usr/bin/env python3
"""Script de automatización en la nube / runner para Antigravity.

Ejecución diaria programada (ej. a las 19:30 o 20:00 hora local):
1. Detecta la bitácora más reciente en `data/`.
2. Ejecuta los Quality Gates (validación, diff con el turno previo, reglas de seguridad).
3. Genera y compila el reporte Quarto interactivo (.qmd / .html).
4. Extrae los pendientes de la visita nocturna y despacha el correo de recordatorio a los residentes de guardia.
"""

from datetime import datetime
from pathlib import Path
import sys
from rich.console import Console
from bitacora.email_notifier import generate_email_content, send_night_email
from bitacora.gates import run_all_gates
from bitacora.render_qmd import render_qmd
from bitacora.schema import load_bitacora
import subprocess

console = Console()


def run_agent() -> int:
    console.print("[bold blue]🤖 Iniciando Agente Antigravity de Bitácora Médica...[/bold blue]")

    data_dir = Path("data")
    json_files = sorted(data_dir.glob("bitacora_*.json"))

    if not json_files:
        console.print("[bold red]❌ No se encontraron archivos de bitácora en data/[/bold red]")
        return 1

    actual_file = json_files[-1]
    anterior_file = json_files[-2] if len(json_files) > 1 else None

    console.print(f"📄 Bitácora actual detectada: [cyan]{actual_file.name}[/cyan]")
    if anterior_file:
        console.print(f"📄 Bitácora previa para diff: [cyan]{anterior_file.name}[/cyan]")

    # 1. Quality Gates
    console.print("\n[yellow]⏳ Ejecutando Quality Gates...[/yellow]")
    gates_res = run_all_gates(
        bitacora_path=actual_file,
        project_root=Path.cwd(),
        anterior_path=anterior_file,
    )

    for check in gates_res.checks:
        icon = "✔" if check.aprobado else "❌"
        color = "green" if check.aprobado else "red"
        console.print(f"  [{color}]{icon} {check.nombre}[/{color}]")

    if not gates_res.aprobado_global:
        console.print("[bold red]❌ Quality Gates fallidos. Abortando despacho nocturno por seguridad clínica.[/bold red]")
        return 1

    # 2. Renderizar Quarto .qmd y HTML
    console.print("\n[yellow]📊 Generando Reporte Quarto...[/yellow]")
    bitacora = load_bitacora(actual_file)
    qmd_out = Path("reports/bitacora_guardia.qmd")
    render_qmd(bitacora, qmd_out)
    console.print(f"  [green]✔ Documento Quarto generado en {qmd_out}[/green]")

    try:
        res = subprocess.run(["quarto", "render", str(qmd_out)], capture_output=True, text=True)
        if res.returncode == 0:
            console.print("  [green]✔ Dashboard HTML Quarto compilado exitosamente.[/green]")
        else:
            console.print(f"  [yellow]Advertencia Quarto:[/yellow] {res.stderr}")
    except Exception as e:
        console.print(f"  [yellow]Quarto render omitido:[/yellow] {e}")

    # 3. Extraer y despachar recordatorios para la Visita Nocturna
    console.print("\n[yellow]🌙 Generando Digest para la Visita Nocturna...[/yellow]")
    subject, body_text, body_html = generate_email_content(bitacora)

    # Guardar copia del correo generado en reports/
    digest_path = Path("reports/recordatorio_visita_nocturna.html")
    digest_path.write_text(body_html, encoding="utf-8")
    console.print(f"  [green]✔ Digest HTML guardado en {digest_path}[/green]")

    # Simulación o envío
    destinatarios = [
        "guardia.mi@hospital.org",
        "residente.reumatologia@hospital.org",
    ]
    send_night_email(bitacora, destinatarios, dry_run=True)
    console.print(f"  [bold green]✔ Digest nocturno listo para: {', '.join(destinatarios)}[/bold green]")
    console.print(f"  [dim]Asunto: {subject}[/dim]")

    console.print("\n[bold green]✨ Agente finalizó todas sus tareas con éxito.[/bold green]")
    return 0


if __name__ == "__main__":
    sys.exit(run_agent())

