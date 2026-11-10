"""Generador de documentos Quarto (.qmd) a partir de la bitácora médica estructurada."""

from pathlib import Path

from bitacora.models import BitacoraGuardia, Paciente


def render_qmd(bitacora: BitacoraGuardia, output_path: Path) -> Path:
    """Transforma el objeto BitacoraGuardia en un documento Quarto (.qmd) interactivo y pulido."""
    lines: list[str] = []

    # Quarto YAML Header
    lines.extend(
        [
            "---",
            'title: "Bitácora Médica de Guardia"',
            f'subtitle: "Periodo: {bitacora.periodo} | Hospitalización, Periféricos y Emergencia"',
            f'date: "{bitacora.fecha_guardia}"',
            'author: "' + ", ".join(bitacora.medicos_guardia) + '"',
            "format:",
            "  html:",
            "    theme: flatly",
            "    toc: true",
            "    toc-depth: 3",
            "    toc-title: 'Índice Clínico'",
            "    number-sections: false",
            "    embed-resources: true",
            "    page-layout: full",
            "    code-tools: false",
            "---",
            "",
        ]
    )

    # Estilos CSS embebidos para badges médicos
    lines.extend(
        [
            "```{=html}",
            "<style>",
            ".badge-stat { background-color: #dc3545; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85em; }",
            ".badge-urgente { background-color: #fd7e14; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85em; }",
            ".badge-control { background-color: #0dcaf0; color: black; padding: 2px 8px; border-radius: 4px; font-weight: bold; font-size: 0.85em; }",
            ".badge-rutina { background-color: #6c757d; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }",
            ".badge-alergia { background-color: #d63384; color: white; padding: 2px 8px; border-radius: 4px; font-weight: bold; }",
            ".badge-atb { background-color: #198754; color: white; padding: 2px 8px; border-radius: 4px; font-size: 0.85em; }",
            ".patient-card { border: 1px solid #dee2e6; border-radius: 8px; padding: 15px; margin-bottom: 20px; background-color: #fdfdfd; box-shadow: 0 2px 4px rgba(0,0,0,0.03); }",
            ".patient-header { border-bottom: 2px solid #0d6efd; padding-bottom: 6px; margin-bottom: 12px; }",
            "</style>",
            "```",
            "",
        ]
    )

    # Resumen Ejecutivo del Censo
    c = bitacora.censo
    lines.extend(
        [
            "## Resumen Ejecutivo de la Guardia",
            "",
            "::: {.callout-note appearance='simple'}",
            f"**Censo Operativo:** Total: **{c.total_pacientes}** pacientes | "
            f"Hospitalizados: **{c.hospitalizados}** | "
            f"Periféricos: **{c.perifericos}** | "
            f"Emergencia/Obs: **{c.emergencia}** | "
            f"Nuevos ingresos de guardia: **{c.ingresos_guardia}**",
            ":::",
            "",
        ]
    )

    if bitacora.enlaces_utiles:
        enlaces_md = " | ".join(
            f"[{k.replace('_', ' ').capitalize()}]({v})" for k, v in bitacora.enlaces_utiles.items()
        )
        lines.extend([f"**Accesos Rápidos:** {enlaces_md}", ""])

    # SECCIÓN CLAVE: CHECKLIST PRIORIZADO DE LA VISITA NOCTURNA
    lines.extend(
        [
            "## Checklist de la Visita Nocturna (Pendientes Críticos)",
            "",
            "> [!IMPORTANT]",
            "> Esta lista compila todas las tareas con horario crítico, exámenes STAT, controles de madrugada y cambios farmacológicos programados para el equipo de guardia.",
            "",
            "| Prioridad | Cama | Paciente | Tarea / Pendiente | Horario / Ventana | Estado |",
            "|---|---|---|---|---|---|",
        ]
    )

    pendientes_encontrados = False
    for p in bitacora.pacientes:
        for pend in p.pendientes:
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE":
                pendientes_encontrados = True
                badge_class = f"badge-{pend.prioridad.value.lower()}"
                horario = pend.horario_programado or pend.turno.value
                lines.append(
                    f'| <span class="{badge_class}">{pend.prioridad.value}</span> | **{p.cama}** | {p.nombre_completo} (HC {p.hc}) | {pend.descripcion} | `{horario}` | ☐ Pendiente |'
                )

    if not pendientes_encontrados:
        lines.append("| - | - | - | Sin pendientes nocturnos registrados | - | - |")
    lines.extend(["", ""])

    # MAPA DE MICROBIOLOGÍA Y RESISTENCIA
    lines.extend(
        [
            "## Mapa Microbiológico y Aislamientos de Alerta",
            "",
            "| Cama | Paciente | Muestra | Microorganismo | Patrón / Resistencia | Terapia Sugerida / Sensible |",
            "|---|---|---|---|---|---|",
        ]
    )
    hay_cultivos = False
    for p in bitacora.pacientes:
        for cult in p.cultivos:
            hay_cultivos = True
            sens_txt = ", ".join(cult.sensible) if cult.sensible else "Ver antibiograma"
            res_txt = cult.patron_resistencia or (
                ", ".join(cult.resistente) if cult.resistente else "Sensible"
            )
            lines.append(
                f"| **{p.cama}** | {p.nombre_completo} | {cult.muestra} | *{cult.microorganismo}* | **{res_txt}** | {sens_txt} |"
            )
    if not hay_cultivos:
        lines.append("| - | - | - | No se registran cultivos activos | - | - |")
    lines.extend(["", ""])

    # CENSO DETALLADO POR ÁREAS EN TABSETS
    lines.extend(
        [
            "## Detalle Clínico por Áreas y Camas",
            "",
            "::: {.panel-tabset}",
            "",
        ]
    )

    areas = [
        ("Hospitalización", "HOSPITALIZACION"),
        ("Periféricos", "PERIFERICOS"),
        ("Emergencia / Observación", "EMERGENCIA_OBSERVACION"),
    ]

    for label_area, key_area in areas:
        pacs_area = [p for p in bitacora.pacientes if p.area == key_area]
        lines.extend([f"### {label_area} ({len(pacs_area)} pacientes)", ""])

        if not pacs_area:
            lines.extend(["*No hay pacientes asignados en esta área en este corte.*", ""])
            continue

        for p in pacs_area:
            lines.extend(_render_paciente_qmd(p))

    lines.extend([":::", ""])

    content = "\n".join(lines)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(content, encoding="utf-8")
    return output_path


def _render_paciente_qmd(p: Paciente) -> list[str]:
    """Genera la ficha clínica estructurada de un paciente para Quarto."""
    out: list[str] = []
    out.extend(
        [
            "```{=html}",
            '<div class="patient-card">',
            '  <div class="patient-header">',
            f'    <h4 style="margin:0; display:inline-block;"><strong>Cama {p.cama}</strong>: {p.nombre_completo}</h4>',
        ]
    )

    if p.alergias:
        alerg_str = ", ".join(p.alergias)
        out.append(f'    <span class="badge-alergia">⚠ ALERGIA: {alerg_str}</span>')
    if p.ckd_epi:
        out.append(f'    <span class="badge-control">CKD-EPI: {p.ckd_epi}</span>')

    out.extend(
        [
            "  </div>",
            "```",
            "",
            f"- **HC:** `{p.hc}` | **Cédula:** {p.cedula or 'N/R'} | **Edad:** {p.edad} | **Sexo:** {p.sexo or 'N/R'}",
        ]
    )

    if p.app:
        out.append(f"- **Antecedentes (APP):** {'; '.join(p.app)}")
    if p.aqx:
        out.append(f"- **Quirúrgicos (AQX):** {'; '.join(p.aqx)}")

    out.extend(
        [
            "",
            "**Diagnósticos Activos:**",
            "",
        ]
    )
    for dx in p.diagnosticos_activos:
        out.append(f"- {dx}")

    if p.dieta or p.hidratacion:
        out.extend(["", "**Soporte y Vía:**"])
        if p.dieta:
            out.append(f"- *Dieta:* {p.dieta}")
        if p.hidratacion:
            out.append(f"- *Hidratación / Infusiones:* {p.hidratacion}")

    if p.medicaciones:
        out.extend(["", "**Medicación Activa:**", ""])
        for m in p.medicaciones:
            status = ""
            if m.suspendido:
                status = " **[SUSPENDIDO]**"
            elif m.diferido:
                status = " *[DIFERIDO]*"
            atb = (
                f' <span class="badge-atb">{m.dias_tratamiento}</span>'
                if m.dias_tratamiento
                else ""
            )
            out.append(f"- {m.nombre} {m.dosis} {m.via} {m.frecuencia}{atb}{status}")

    if p.pendientes:
        out.extend(["", "**Pendientes:**", ""])
        for pend in p.pendientes:
            badge = (
                f'<span class="badge-{pend.prioridad.value.lower()}">{pend.prioridad.value}</span>'
            )
            horario = f" (`{pend.horario_programado}`)" if pend.horario_programado else ""
            checkbox = "☑" if pend.estado == "COMPLETADO" else "☐"
            out.append(f"- {checkbox} {badge} **[{pend.tipo.value}]** {pend.descripcion}{horario}")

    if p.laboratorios:
        out.extend(["", "**Últimos Laboratorios:**", ""])
        for lab in p.laboratorios:
            val_strs = [f"{k}: {v}" for k, v in lab.valores.items()]
            out.append(f"- *{lab.fecha_hora} ({lab.tipo_estudio}):* {', '.join(val_strs)}")

    if p.observaciones:
        out.extend(["", "**Observaciones / Novedades:**", ""])
        for obs in p.observaciones:
            out.append(f"> {obs}")

    out.extend(
        [
            "```{=html}",
            "</div>",
            "```",
            "",
        ]
    )
    return out
