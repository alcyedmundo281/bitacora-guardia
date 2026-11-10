"""Generador y despachador de correos de recordatorio para la visita nocturna de guardia."""

import smtplib
from dataclasses import dataclass
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from bitacora.models import BitacoraGuardia, PendientePrioridad


@dataclass
class TareaNocturna:
    """Ítem accionable extraído para el reporte nocturno."""

    cama: str
    paciente_nombre: str
    hc: str
    prioridad: str
    tipo: str
    descripcion: str
    horario: str


def extract_night_tasks(bitacora: BitacoraGuardia) -> list[TareaNocturna]:
    """Filtra y ordena todos los pendientes activos para la visita nocturna."""
    tasks: list[TareaNocturna] = []

    priority_weights = {
        PendientePrioridad.STAT.value: 1,
        PendientePrioridad.URGENTE.value: 2,
        PendientePrioridad.CONTROL.value: 3,
        PendientePrioridad.RUTINA.value: 4,
    }

    for p in bitacora.pacientes:
        for pend in p.pendientes:
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE":
                tasks.append(
                    TareaNocturna(
                        cama=p.cama,
                        paciente_nombre=p.nombre_completo,
                        hc=p.hc,
                        prioridad=pend.prioridad.value,
                        tipo=pend.tipo.value,
                        descripcion=pend.descripcion,
                        horario=pend.horario_programado or pend.turno.value,
                    )
                )

    tasks.sort(key=lambda t: (priority_weights.get(t.prioridad, 99), t.cama))
    return tasks


def generate_email_content(bitacora: BitacoraGuardia) -> tuple[str, str, str]:
    """Genera (subject, body_text, body_html) para el correo de la visita nocturna."""
    tasks = extract_night_tasks(bitacora)
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    stat_count = sum(1 for t in tasks if t.prioridad == PendientePrioridad.STAT.value)
    urgente_count = sum(1 for t in tasks if t.prioridad == PendientePrioridad.URGENTE.value)
    control_count = sum(1 for t in tasks if t.prioridad == PendientePrioridad.CONTROL.value)

    subject = (
        f"🚨 [GUARDIA {bitacora.fecha_guardia}] Recordatorio de Visita Nocturna: "
        f"{len(tasks)} pendientes ({stat_count} STAT, {urgente_count} Urgentes)"
    )

    # Versión Texto Plano
    text_lines = [
        "RECORDATORIO DE PENDIENTES - VISITA NOCTURNA",
        f"Periodo: {bitacora.periodo}",
        f"Médicos de Guardia: {', '.join(bitacora.medicos_guardia)}",
        f"Generado: {now_str}",
        f"Total de tareas pendientes: {len(tasks)} (STAT: {stat_count}, Urgentes: {urgente_count}, Controles: {control_count})",
        "=" * 60,
        "",
    ]
    for t in tasks:
        text_lines.append(f"[{t.prioridad}] Cama {t.cama} - {t.paciente_nombre} (HC: {t.hc})")
        text_lines.append(f"  Tarea ({t.tipo}): {t.descripcion}")
        text_lines.append(f"  Horario: {t.horario}")
        text_lines.append("-" * 40)

    body_text = "\n".join(text_lines)

    # Versión HTML Responsive
    html_rows = []
    for t in tasks:
        color = (
            "#dc3545"
            if t.prioridad == "STAT"
            else ("#fd7e14" if t.prioridad == "URGENTE" else "#0dcaf0")
        )
        text_color = "black" if t.prioridad == "CONTROL" else "white"
        row = f"""
        <tr style="border-bottom: 1px solid #e9ecef;">
            <td style="padding: 10px; text-align: center;">
                <span style="background-color: {color}; color: {text_color}; padding: 3px 8px; border-radius: 4px; font-weight: bold; font-size: 11px;">
                    {t.prioridad}
                </span>
            </td>
            <td style="padding: 10px; font-weight: bold; color: #0d6efd;">{t.cama}</td>
            <td style="padding: 10px;">
                <strong>{t.paciente_nombre}</strong><br>
                <small style="color: #6c757d;">HC: {t.hc}</small>
            </td>
            <td style="padding: 10px;">
                <span style="color: #495057; font-size: 12px; font-weight: 600;">[{t.tipo}]</span> {t.descripcion}
            </td>
            <td style="padding: 10px; color: #212529; font-family: monospace;">{t.horario}</td>
            <td style="padding: 10px; text-align: center;">☐</td>
        </tr>
        """
        html_rows.append(row)

    body_html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <title>{subject}</title>
    </head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f4f6f9; margin: 0; padding: 20px;">
        <div style="max-width: 850px; margin: 0 auto; background: #ffffff; border-radius: 8px; overflow: hidden; box-shadow: 0 4px 6px rgba(0,0,0,0.05);">
            <div style="background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); color: white; padding: 24px;">
                <h1 style="margin: 0 0 8px 0; font-size: 22px;">🌙 Visita Nocturna de Guardia</h1>
                <p style="margin: 0; opacity: 0.9; font-size: 14px;">Periodo: <strong>{bitacora.periodo}</strong> | Guardia: {", ".join(bitacora.medicos_guardia)}</p>
            </div>
            
            <div style="padding: 20px;">
                <div style="display: flex; gap: 15px; margin-bottom: 20px;">
                    <div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px 16px; border-radius: 4px; flex: 1;">
                        <strong>Resumen Operativo:</strong> Se registran <strong>{len(tasks)}</strong> tareas para la ronda nocturna y madrugada.<br>
                        <span style="color: #dc3545; font-weight: bold;">{stat_count} STAT</span> | 
                        <span style="color: #fd7e14; font-weight: bold;">{urgente_count} Urgentes</span> | 
                        <span style="color: #0dcaf0; font-weight: bold;">{control_count} Controles</span>
                    </div>
                </div>

                <table style="width: 100%; border-collapse: collapse; font-size: 13px; text-align: left;">
                    <thead>
                        <tr style="background-color: #f8f9fa; border-bottom: 2px solid #dee2e6;">
                            <th style="padding: 10px; text-align: center;">Prioridad</th>
                            <th style="padding: 10px;">Cama</th>
                            <th style="padding: 10px;">Paciente</th>
                            <th style="padding: 10px;">Pendiente Clínico</th>
                            <th style="padding: 10px;">Horario</th>
                            <th style="padding: 10px; text-align: center;">Hecho</th>
                        </tr>
                    </thead>
                    <tbody>
                        {"".join(html_rows)}
                    </tbody>
                </table>

                <div style="margin-top: 25px; padding: 12px; background: #eef2f7; border-radius: 4px; font-size: 12px; color: #495057;">
                    🤖 <em>Generado automáticamente por el Agente Antigravity de Bitácora Médica. Documento sincronizado en Google Drive.</em>
                </div>
            </div>
        </div>
    </body>
    </html>
    """
    return subject, body_text, body_html


def send_night_email(
    bitacora: BitacoraGuardia,
    recipients: list[str],
    smtp_host: str = "smtp.gmail.com",
    smtp_port: int = 587,
    sender_email: str | None = None,
    sender_password: str | None = None,
    dry_run: bool = True,
) -> bool:
    """Envía el correo a los médicos de guardia o realiza una simulación dry-run."""
    subject, body_text, body_html = generate_email_content(bitacora)

    if dry_run or not sender_email or not sender_password:
        return True

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = sender_email
    msg["To"] = ", ".join(recipients)

    msg.attach(MIMEText(body_text, "plain", "utf-8"))
    msg.attach(MIMEText(body_html, "html", "utf-8"))

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(sender_email, sender_password)
        server.sendmail(sender_email, recipients, msg.as_string())

    return True
