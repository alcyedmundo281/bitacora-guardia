"""CLI y Agente de automatización para la Bitácora Médica de Guardia."""

import subprocess
import sys
from pathlib import Path

import typer
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bitacora.diff_engine import compare_bitacoras
from bitacora.email_notifier import extract_night_tasks, generate_email_content, send_night_email
from bitacora.gates import run_all_gates
from bitacora.models import Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.render_grid import generate_web_grid_html
from bitacora.render_qmd import render_qmd
from bitacora.schema import export_json_schema, load_bitacora, save_bitacora
from bitacora.sync_grid import export_to_editable_grid, import_from_editable_grid
from bitacora.whatsapp_bot import format_whatsapp_night_summary, parse_whatsapp_incoming_message
from bitacora.workflow import add_pendiente_flow, audit_guardia_cierre

app = typer.Typer(
    name="bitacora",
    help="Agente Antigravity para gestión inteligente de bitácoras de guardia médica.",
)
console = Console()


@app.command()
def validate(
    file_path: Path = typer.Argument(..., help="Ruta al archivo JSON de bitácora"),
) -> None:
    """Valida la sintaxis e integridad del archivo de bitácora."""
    try:
        bitacora = load_bitacora(file_path)
        console.print(
            Panel(
                f"[bold green]✔ Bitácora válida:[/bold green] {bitacora.periodo}\n"
                f"Total Pacientes: [cyan]{len(bitacora.pacientes)}[/cyan] | "
                f"Hospitalizados: [cyan]{bitacora.censo.hospitalizados}[/cyan] | "
                f"Periféricos: [cyan]{bitacora.censo.perifericos}[/cyan] | "
                f"Emergencia: [cyan]{bitacora.censo.emergencia}[/cyan]",
                title="Validación Exitosa",
            )
        )
    except Exception as e:
        console.print(f"[bold red]❌ Error de validación:[/bold red] {e}")
        sys.exit(1)


@app.command()
def diff(
    anterior: Path = typer.Argument(..., help="Bitácora del turno previo"),
    actual: Path = typer.Argument(..., help="Bitácora del turno actual"),
) -> None:
    """Compara dos bitácoras y resalta altas, ingresos y novedades clínicas."""
    bit_ant = load_bitacora(anterior)
    bit_act = load_bitacora(actual)
    result = compare_bitacoras(bit_ant, bit_act)

    console.print(
        Panel(
            f"Comparativa: [dim]{result.fecha_anterior}[/dim] ➔ [bold]{result.fecha_actual}[/bold]\n"
            f"Nuevos ingresos: [green]{len(result.ingresos_nuevos)}[/green] | "
            f"Egresos/Traslados: [yellow]{len(result.egresos_o_traslados)}[/yellow] | "
            f"Pacientes con cambios: [cyan]{len(result.pacientes_modificados)}[/cyan] | "
            f"Pendientes nocturnos activos: [magenta]{result.total_pendientes_nocturnos_activos}[/magenta]",
            title="Resumen del Diff Clínico",
        )
    )

    if result.ingresos_nuevos:
        console.print("\n[bold green]➕ Nuevos Ingresos:[/bold green]")
        for ing in result.ingresos_nuevos:
            console.print(f"  • Cama {ing.cama}: {ing.nombre_completo} (HC {ing.hc})")

    if result.egresos_o_traslados:
        console.print("\n[bold yellow]➖ Egresos / Traslados:[/bold yellow]")
        for eg in result.egresos_o_traslados:
            console.print(f"  • Cama {eg.cama}: {eg.nombre_completo} (HC {eg.hc})")

    if result.pacientes_modificados:
        table = Table(title="Detalle de Modificaciones Clínicas", show_lines=True)
        table.add_column("Cama", style="cyan", width=8)
        table.add_column("Paciente", style="bold")
        table.add_column("Nuevos Pendientes", style="red")
        table.add_column("Pendientes Resueltos", style="green")
        table.add_column("Alertas / Fármacos", style="magenta")

        for p in result.pacientes_modificados:
            nuevos = "\n".join(f"• {x.descripcion}" for x in p.nuevos_pendientes) or "-"
            resueltos = "\n".join(f"✔ {x.descripcion}" for x in p.pendientes_resueltos) or "-"
            farmacos = "\n".join(p.antibioticos_cambios + p.alertas_nuevas) or "-"
            table.add_row(p.cama, p.nombre, nuevos, resueltos, farmacos)

        console.print(table)


@app.command()
def render(
    file_path: Path = typer.Argument(..., help="Archivo JSON de la bitácora"),
    output_qmd: Path = typer.Option(
        Path("reports/bitacora_guardia.qmd"), "--output", "-o", help="Ruta de destino del .qmd"
    ),
    compile_html: bool = typer.Option(
        True, "--html/--no-html", help="Compilar automáticamente con Quarto a HTML"
    ),
) -> None:
    """Genera el instrumento Quarto (.qmd) y compila el dashboard HTML."""
    bitacora = load_bitacora(file_path)
    rendered_path = render_qmd(bitacora, output_qmd)
    console.print(f"[bold green]✔ Quarto .qmd generado en:[/bold green] {rendered_path}")

    if compile_html:
        console.print("[cyan]Compilando dashboard interactivo con Quarto...[/cyan]")
        try:
            res = subprocess.run(
                ["quarto", "render", str(rendered_path)],
                capture_output=True,
                text=True,
            )
            if res.returncode == 0:
                html_path = rendered_path.with_suffix(".html")
                console.print(f"[bold green]✔ Dashboard HTML compilado:[/bold green] {html_path}")
            else:
                console.print(f"[yellow]Advertencia Quarto:[/yellow] {res.stderr}")
        except FileNotFoundError:
            console.print(
                "[yellow]Quarto no está en el PATH del sistema; archivo .qmd listo.[/yellow]"
            )


@app.command()
def notify_night(
    file_path: Path = typer.Argument(..., help="Archivo JSON de la bitácora"),
    dry_run: bool = typer.Option(
        True, "--dry-run/--send", help="Modo simulación sin enviar correo real"
    ),
    recipient: list[str] = typer.Option(
        ["guardia.medicina.interna@hospital.org"], "--to", help="Destinatarios del correo"
    ),
) -> None:
    """Extrae pendientes de la visita nocturna y genera/envía el recordatorio por email."""
    bitacora = load_bitacora(file_path)
    tasks = extract_night_tasks(bitacora)

    table = Table(
        title=f"🌙 Checklist de Visita Nocturna ({len(tasks)} pendientes)",
        show_lines=True,
    )
    table.add_column("Prioridad", style="bold")
    table.add_column("Cama", style="cyan")
    table.add_column("Paciente")
    table.add_column("Pendiente Clínico")
    table.add_column("Horario / Ventana", style="yellow")

    for t in tasks:
        color = (
            "red" if t.prioridad == "STAT" else ("yellow" if t.prioridad == "URGENTE" else "blue")
        )
        table.add_row(
            f"[{color}]{t.prioridad}[/{color}]",
            t.cama,
            f"{t.paciente_nombre}\n(HC {t.hc})",
            f"[{t.tipo}] {t.descripcion}",
            t.horario,
        )

    console.print(table)

    subject, _, _ = generate_email_content(bitacora)
    console.print(f"\n[bold]Asunto de correo:[/bold] {subject}")
    console.print(f"[bold]Destinatarios:[/bold] {', '.join(recipient)}")

    if dry_run:
        console.print("[dim italic]Modo Dry-Run activo: no se ha transmitido SMTP.[/dim italic]")
    else:
        send_night_email(bitacora, recipient, dry_run=False)
        console.print("[bold green]✔ Correo transmitido exitosamente.[/bold green]")


@app.command()
def gate_check(
    bitacora_path: Path = typer.Argument(..., help="Bitácora actual a evaluar"),
    anterior_path: Path = typer.Option(None, "--anterior", "-a", help="Bitácora previa para diff"),
) -> None:
    """Ejecuta los Quality Gates (Schema, Reglas Clínicas, Diff, Mypy)."""
    project_root = Path.cwd()
    res = run_all_gates(bitacora_path, project_root, anterior_path)

    table = Table(title="Suite de Quality Gates", show_lines=True)
    table.add_column("Gate")
    table.add_column("Estado")
    table.add_column("Detalles / Diagnóstico")

    for c in res.checks:
        status = "[green]APROBADO[/green]" if c.aprobado else "[red]RECHAZADO[/red]"
        detalles = "\n".join(c.mensajes[:5]) if c.mensajes else "Sin observaciones"
        if len(c.mensajes) > 5:
            detalles += f"\n... ({len(c.mensajes) - 5} observaciones adicionales)"
        table.add_row(c.nombre, status, detalles)

    console.print(table)

    if not res.aprobado_global:
        console.print("\n[bold red]❌ Uno o más Quality Gates no se superaron.[/bold red]")
        sys.exit(1)
    else:
        console.print(
            "\n[bold green]✔ Todos los Quality Gates aprobados exitosamente.[/bold green]"
        )


@app.command()
def schema_export(
    output: Path = typer.Option(Path("data/bitacora.schema.json"), "--output", "-o"),
) -> None:
    """Exporta el JSON Schema formal para integración externa."""
    p = export_json_schema(output)
    console.print(f"[bold green]✔ JSON Schema exportado en:[/bold green] {p}")


@app.command()
def add_pendiente(
    bitacora_path: Path = typer.Argument(..., help="Ruta al archivo JSON de la bitácora"),
    cama: str = typer.Option(..., "--cama", "-c", help="Cama del paciente (ej. 6A, 8, AB 3B)"),
    descripcion: str = typer.Option(..., "--desc", "-d", help="Descripción del pendiente clínico"),
    tipo: PendienteTipo = typer.Option(
        PendienteTipo.LABORATORIO, "--tipo", "-t", help="Tipo de pendiente"
    ),
    prioridad: PendientePrioridad = typer.Option(
        PendientePrioridad.URGENTE, "--prioridad", "-p", help="STAT, URGENTE, CONTROL, RUTINA"
    ),
    turno: TurnoObjetivo = typer.Option(
        TurnoObjetivo.NOCTURNA_20_24, "--turno", help="NOCTURNA_20_24, MADRUGADA_04_06, MANANA"
    ),
    horario: str = typer.Option(
        None, "--horario", "-h", help="Horario específico (ej. 22:00, 05:00 AM)"
    ),
    nocturno: bool = typer.Option(
        True, "--nocturno/--no-nocturno", help="Exigir auditoría en la visita nocturna"
    ),
) -> None:
    """Permite al residente de la mañana registrar un pendiente obligatorio para la guardia."""
    add_pendiente_flow(
        bitacora_path=bitacora_path,
        cama=cama,
        descripcion=descripcion,
        tipo=tipo,
        prioridad=prioridad,
        turno=turno,
        horario=horario,
        es_nocturno=nocturno,
    )


@app.command()
def audit_cierre(
    bitacora_path: Path = typer.Argument(..., help="Bitácora entregada por la guardia nocturna"),
) -> None:
    """Audita si la guardia nocturna completó y documentó todos los resultados exigidos."""
    aprobado = audit_guardia_cierre(bitacora_path)
    if not aprobado:
        sys.exit(1)


@app.command()
def grid_export(
    bitacora_path: Path = typer.Argument(..., help="Ruta al archivo JSON de la bitácora"),
    output_csv: Path = typer.Option(
        Path("data/bitacora_grilla_guardia.csv"),
        "--output",
        "-o",
        help="Ruta del CSV/Grilla para Drive",
    ),
) -> None:
    """Exporta los pacientes a una grilla editable estilo Google Sheets / Excel."""
    out = export_to_editable_grid(bitacora_path, output_csv)
    console.print(f"[bold green]✔ Grilla editable exportada en:[/bold green] {out}")
    console.print(
        "[dim]Los médicos pueden abrirla y editarla en Google Drive / Excel sin escribir código.[/dim]"
    )


@app.command()
def grid_import(
    csv_path: Path = typer.Argument(
        ..., help="Ruta al archivo CSV editado por los médicos en Drive"
    ),
    base_bitacora: Path = typer.Argument(..., help="Bitácora JSON base"),
    output_json: Path = typer.Option(None, "--output", "-o", help="Destino del JSON actualizado"),
) -> None:
    """Sincroniza los cambios hechos por los médicos en la grilla de Drive hacia el JSON canónico."""
    dest = output_json or base_bitacora
    bit = import_from_editable_grid(csv_path, base_bitacora, dest)
    console.print(
        Panel(
            f"[bold green]✔ Sincronización exitosa desde la Grilla:[/bold green]\n"
            f"Archivo actualizado: [cyan]{dest.name}[/cyan]\n"
            f"Total pacientes sincronizados: {len(bit.pacientes)}",
            title="Importación de Grilla Drive",
        )
    )


@app.command()
def web_grid(
    bitacora_path: Path = typer.Argument(..., help="Ruta al archivo JSON de la bitácora"),
    output_html: Path = typer.Option(
        Path("reports/editor_grilla.html"), "--output", "-o", help="Destino del archivo HTML"
    ),
) -> None:
    """Genera la interfaz visual interactiva tipo app para abrir en Drive con doble clic."""
    out = generate_web_grid_html(bitacora_path, output_html)
    console.print(f"[bold green]✔ Interfaz visual de grilla generada en:[/bold green] {out}")
    console.print(
        "[dim]Cualquier médico o residente puede abrirla en Chrome desde Drive con un clic.[/dim]"
    )


@app.command()
def whatsapp_digest(
    bitacora_path: Path = typer.Argument(..., help="Ruta al archivo JSON de la bitácora"),
) -> None:
    """Genera el mensaje formateado para enviar al grupo de WhatsApp de la guardia."""
    bitacora = load_bitacora(bitacora_path)
    resumen = format_whatsapp_night_summary(bitacora)
    console.print(Panel(resumen, title="📲 Mensaje para WhatsApp de Guardia"))


@app.command()
def whatsapp_receive(
    bitacora_path: Path = typer.Argument(..., help="Ruta al archivo JSON de la bitácora"),
    mensaje: str = typer.Argument(..., help="Mensaje textual recibido por WhatsApp"),
) -> None:
    """Procesa un mensaje informal de WhatsApp y actualiza la bitácora automáticamente."""
    parsed = parse_whatsapp_incoming_message(mensaje)
    if not parsed:
        console.print("[bold red]❌ No se pudo interpretar el mensaje.[/bold red]")
        sys.exit(1)

    cama = parsed["cama"]
    bitacora = load_bitacora(bitacora_path)
    paciente = next((p for p in bitacora.pacientes if p.cama.upper() == cama.upper()), None)
    if not paciente:
        console.print(f"[bold red]❌ No se encontró la cama '{cama}' en la bitácora.[/bold red]")
        sys.exit(1)

    if parsed["accion"] == "REGISTRAR_RESULTADO":
        resultado = parsed["resultado"]
        # Buscar el primer pendiente nocturno activo de esa cama para adjudicarle el resultado
        modificado = False
        pends_nuevos = []
        for pend in paciente.pendientes:
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE" and not modificado:
                pends_nuevos.append(
                    pend.model_copy(
                        update={"estado": "COMPLETADO", "resultado_obtenido": resultado}
                    )
                )
                modificado = True
            else:
                pends_nuevos.append(pend)

        if not modificado:
            # Si no tenía pendiente abierto, agregarlo como completado para registro
            nuevo_p = Pendiente(
                id=f"PEND-{cama}-AUTO",
                tipo=PendienteTipo.LABORATORIO,
                descripcion="Reporte de guardia vía WhatsApp",
                prioridad=PendientePrioridad.STAT,
                turno=TurnoObjetivo.NOCTURNA_20_24,
                es_visita_nocturna=True,
                estado="COMPLETADO",
                resultado_obtenido=resultado,
            )
            pends_nuevos.append(nuevo_p)

        p_nuevo = paciente.model_copy(update={"pendientes": pends_nuevos})
        pacs_actualizados = [
            p_nuevo if p.cama.upper() == cama.upper() else p for p in bitacora.pacientes
        ]
        nueva_bit = bitacora.model_copy(update={"pacientes": pacs_actualizados})
        save_bitacora(nueva_bit, bitacora_path)

        console.print(
            Panel(
                f"✔ [bold green]Resultado registrado vía WhatsApp:[/bold green]\n"
                f"• Cama: [cyan]{cama}[/cyan] ({paciente.nombre_completo})\n"
                f"• Resultado documentado: {resultado}",
                title="Actualización por WhatsApp",
            )
        )
    elif parsed["accion"] == "CREAR_PENDIENTE":
        add_pendiente_flow(
            bitacora_path=bitacora_path,
            cama=cama,
            descripcion=parsed["descripcion"],
            tipo=parsed["tipo"],
            prioridad=parsed["prioridad"],
            turno=parsed["turno"],
            horario=parsed["horario"],
            es_nocturno=parsed["es_nocturna"],
        )


@app.command()
def start_server(
    host: str = typer.Option("127.0.0.1", "--host", "-h", help="Host de escucha"),
    port: int = typer.Option(8000, "--port", "-p", help="Puerto HTTP"),
) -> None:
    """Inicia el servidor Webhook para recibir mensajes de WhatsApp en tiempo real."""
    import uvicorn

    console.print(
        Panel(
            f"🚀 [bold green]Servidor Webhook WhatsApp Activo[/bold green]\n"
            f"• Endpoint local: [cyan]http://{host}:{port}/whatsapp/webhook[/cyan]\n"
            f"• Endpoint resumen nocturno: [cyan]http://{host}:{port}/whatsapp/resumen-nocturno[/cyan]\n"
            f"• Documentación interactiva: [cyan]http://{host}:{port}/docs[/cyan]",
            title="Bridge WhatsApp - Antigravity",
        )
    )
    uvicorn.run("bitacora.webhook_server:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    app()
