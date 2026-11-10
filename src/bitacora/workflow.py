"""Extensión del Agente CLI para ingreso rápido de pendientes y cierre auditado de guardia."""

import sys
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from bitacora.models import Pendiente, PendientePrioridad, PendienteTipo, TurnoObjetivo
from bitacora.schema import load_bitacora, save_bitacora

console = Console()


def add_pendiente_flow(
    bitacora_path: Path,
    cama: str,
    descripcion: str,
    tipo: PendienteTipo = PendienteTipo.LABORATORIO,
    prioridad: PendientePrioridad = PendientePrioridad.URGENTE,
    turno: TurnoObjetivo = TurnoObjetivo.NOCTURNA_20_24,
    horario: str | None = None,
    es_nocturno: bool = True,
) -> None:
    """Permite al residente de la mañana inyectar una tarea obligatoria en segundos."""
    bitacora = load_bitacora(bitacora_path)

    paciente = next((p for p in bitacora.pacientes if p.cama.upper() == cama.upper()), None)
    if not paciente:
        console.print(
            f"[bold red]❌ Error:[/bold red] No se encontró paciente en la cama '{cama}'."
        )
        sys.exit(1)

    nuevo_id = f"PEND-{cama.upper()}-{len(paciente.pendientes) + 1:02d}"
    nuevo_pend = Pendiente(
        id=nuevo_id,
        tipo=tipo,
        descripcion=descripcion,
        prioridad=prioridad,
        turno=turno,
        horario_programado=horario,
        es_visita_nocturna=es_nocturno,
        estado="PENDIENTE",
    )

    # Reconstruir paciente inmutable con el nuevo pendiente
    pacientes_actualizados = []
    for p in bitacora.pacientes:
        if p.cama.upper() == cama.upper():
            nuevos_pends = list(p.pendientes) + [nuevo_pend]
            p_nuevo = p.model_copy(update={"pendientes": nuevos_pends})
            pacientes_actualizados.append(p_nuevo)
        else:
            pacientes_actualizados.append(p)

    nueva_bitacora = bitacora.model_copy(update={"pacientes": pacientes_actualizados})
    save_bitacora(nueva_bitacora, bitacora_path)

    console.print(
        Panel(
            f"✔ [bold green]Pendiente Obligatorio Registrado:[/bold green] [{nuevo_id}]\n"
            f"• Cama: [cyan]{paciente.cama}[/cyan] ({paciente.nombre_completo})\n"
            f"• Prioridad: [bold red]{prioridad.value}[/bold red] | Turno: {turno.value}\n"
            f"• Tarea: {descripcion}\n"
            f"• Horario exigido: {horario or 'Durante la visita nocturna'}\n"
            f"• Exigibilidad en la noche: [bold yellow]OBLIGATORIO PARA CIERRE DE GUARDIA[/bold yellow]",
            title="Carga Matutina de Pendiente",
        )
    )


def audit_guardia_cierre(bitacora_path: Path) -> bool:
    """Audita si la guardia nocturna completó y documentó todos los resultados exigidos."""
    bitacora = load_bitacora(bitacora_path)
    incumplidos: list[tuple[str, str, Pendiente]] = []

    for p in bitacora.pacientes:
        for pend in p.pendientes:
            if pend.es_visita_nocturna:
                # Condición de bloqueo: sigue PENDIENTE o está COMPLETADO pero sin resultado clínico
                if pend.estado == "PENDIENTE":
                    incumplidos.append((p.cama, p.nombre_completo, pend))
                elif pend.estado == "COMPLETADO" and not (pend.resultado_obtenido or "").strip():
                    incumplidos.append((p.cama, p.nombre_completo, pend))

    if not incumplidos:
        console.print(
            Panel(
                "[bold green]✔ CIERRE DE GUARDIA APROBADO:[/bold green]\n"
                "Todos los pendientes de la visita nocturna fueron ejecutados y cuentan con resultado documentado.",
                title="Auditoría de Cierre",
            )
        )
        return True

    table = Table(
        title="🚨 AUDITORÍA BLOQUEADA: Pendientes Nocturnos Omitidos o Sin Resultado",
        show_lines=True,
    )
    table.add_column("Cama", style="cyan", width=8)
    table.add_column("Paciente")
    table.add_column("Prioridad", style="bold red")
    table.add_column("Pendiente Exigido")
    table.add_column("Defecto Detectado", style="yellow")

    for cama, nom, pend in incumplidos:
        defecto = (
            "NO EJECUTADO (Continúa PENDIENTE)"
            if pend.estado == "PENDIENTE"
            else "SIN RESULTADO CLÍNICO REPORTADO"
        )
        table.add_row(cama, nom, pend.prioridad.value, pend.descripcion, defecto)

    console.print(table)
    console.print(
        "\n[bold red]⛔ RECHAZO DE GUARDIA:[/bold red] Los médicos de guardia deben ingresar el resultado de cada ítem antes de entregar el turno."
    )
    return False
