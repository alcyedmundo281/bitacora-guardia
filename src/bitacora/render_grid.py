"""Generador del Frontend de Grilla Web Interactiva para médicos en Drive."""

import json
from pathlib import Path

from bitacora.schema import load_bitacora


def generate_web_grid_html(bitacora_path: Path, output_html_path: Path) -> Path:
    """Genera un archivo HTML interactivo con tabla editable, filtros y exportación."""
    bitacora = load_bitacora(bitacora_path)

    # Aplanar pacientes y pendientes para la grilla
    grid_data = []
    for p in bitacora.pacientes:
        atb_str = ", ".join(
            f"{m.nombre} ({m.dias_tratamiento})" for m in p.medicaciones if m.dias_tratamiento
        )
        for pend in p.pendientes:
            grid_data.append(
                {
                    "cama": p.cama,
                    "paciente": p.nombre_completo,
                    "hc": p.hc,
                    "atb": atb_str,
                    "id": pend.id,
                    "tipo": pend.tipo.value,
                    "descripcion": pend.descripcion,
                    "prioridad": pend.prioridad.value,
                    "horario": pend.horario_programado or "",
                    "es_nocturna": pend.es_visita_nocturna,
                    "estado": pend.estado,
                    "resultado": pend.resultado_obtenido or "",
                }
            )

    json_embedded = json.dumps(grid_data, ensure_ascii=False)

    html_content = f"""<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Grilla Médica de Guardia | Drive</title>
    <link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.0/dist/css/bootstrap.min.css" rel="stylesheet">
    <style>
        body {{ background-color: #f4f6f9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; }}
        .header-bar {{ background: linear-gradient(135deg, #1e3c72 0%, #2a5298 100%); color: white; padding: 18px 25px; border-radius: 0 0 12px 12px; margin-bottom: 25px; }}
        .badge-stat {{ background-color: #dc3545; color: white; }}
        .badge-urgente {{ background-color: #fd7e14; color: white; }}
        .badge-control {{ background-color: #0dcaf0; color: black; }}
        .badge-rutina {{ background-color: #6c757d; color: white; }}
        .table-container {{ background: white; border-radius: 10px; padding: 20px; box-shadow: 0 2px 8px rgba(0,0,0,0.06); }}
        .editable-cell {{ cursor: pointer; transition: background 0.2s; }}
        .editable-cell:hover {{ background-color: #fff3cd; }}
        .row-completed {{ opacity: 0.6; text-decoration: line-through; }}
    </style>
</head>
<body>
    <div class="container-fluid px-4">
        <div class="header-bar d-flex justify-content-between align-items-center">
            <div>
                <h3 class="mb-1">📋 Grilla Interactiva de Guardia Médica</h3>
                <p class="mb-0 opacity-75">Periodo: <strong>{bitacora.periodo}</strong> | Médicos: {", ".join(bitacora.medicos_guardia)}</p>
            </div>
            <div>
                <button class="btn btn-light btn-sm me-2" onclick="copyWhatsAppDigest()">📲 Copiar Resumen WhatsApp</button>
                <button class="btn btn-success btn-sm" onclick="exportDataJSON()">💾 Descargar JSON Actualizado</button>
            </div>
        </div>

        <div class="table-container mb-4">
            <div class="row g-2 mb-3 align-items-center">
                <div class="col-md-4">
                    <input type="text" id="searchInput" class="form-control" placeholder="🔍 Buscar por cama, paciente o tarea..." onkeyup="filterTable()">
                </div>
                <div class="col-md-8 text-end">
                    <button class="btn btn-outline-secondary btn-sm active" onclick="setFilter('ALL')">Todos</button>
                    <button class="btn btn-outline-danger btn-sm" onclick="setFilter('STAT')">🔴 Solo STAT</button>
                    <button class="btn btn-outline-warning btn-sm" onclick="setFilter('URGENTE')">🟠 Solo Urgentes</button>
                    <button class="btn btn-outline-primary btn-sm" onclick="setFilter('NOCTURNA')">🌙 Solo Visita Nocturna</button>
                    <button class="btn btn-outline-success btn-sm" onclick="setFilter('PENDIENTE')">⏳ Solo Pendientes</button>
                </div>
            </div>

            <div class="table-responsive">
                <table class="table table-hover align-middle" id="gridTable">
                    <thead class="table-light">
                        <tr>
                            <th style="width: 50px;">Estado</th>
                            <th style="width: 70px;">Cama</th>
                            <th>Paciente</th>
                            <th style="width: 100px;">Prioridad</th>
                            <th style="width: 120px;">Tipo</th>
                            <th>Pendiente Clínico</th>
                            <th style="width: 110px;">Horario</th>
                            <th>Resultado Clínico Obtenido (Obligatorio en Noche)</th>
                        </tr>
                    </thead>
                    <tbody id="gridBody">
                        <!-- Renderizado por JS -->
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <script>
        let data = {json_embedded};
        let currentFilter = 'ALL';

        function renderRows() {{
            const tbody = document.getElementById('gridBody');
            tbody.innerHTML = '';
            const search = document.getElementById('searchInput').value.toLowerCase();

            data.forEach((item, index) => {{
                // Filtrado
                if (currentFilter === 'STAT' && item.prioridad !== 'STAT') return;
                if (currentFilter === 'URGENTE' && item.prioridad !== 'URGENTE') return;
                if (currentFilter === 'NOCTURNA' && !item.es_nocturna) return;
                if (currentFilter === 'PENDIENTE' && item.estado === 'COMPLETADO') return;

                const textToSearch = (item.cama + ' ' + item.paciente + ' ' + item.descripcion + ' ' + item.resultado).toLowerCase();
                if (search && !textToSearch.includes(search)) return;

                const isDone = item.estado === 'COMPLETADO';
                const tr = document.createElement('tr');
                if (isDone) tr.classList.add('table-light');

                const badgeClass = item.prioridad === 'STAT' ? 'badge-stat' : (item.prioridad === 'URGENTE' ? 'badge-urgente' : 'badge-control');

                tr.innerHTML = `
                    <td class="text-center">
                        <input type="checkbox" class="form-check-input" ${{isDone ? 'checked' : ''}} onchange="toggleDone(${{index}})">
                    </td>
                    <td><strong class="text-primary">${{item.cama}}</strong></td>
                    <td>
                        <strong>${{item.paciente}}</strong><br>
                        <small class="text-muted">HC: ${{item.hc}}</small>
                    </td>
                    <td><span class="badge ${{badgeClass}}">${{item.prioridad}}</span></td>
                    <td><span class="badge bg-secondary">${{item.tipo}}</span></td>
                    <td>${{item.descripcion}}</td>
                    <td><code>${{item.horario || 'N/E'}}</code></td>
                    <td>
                        <input type="text" class="form-control form-control-sm editable-cell" 
                               value="${{item.resultado}}" 
                               placeholder="Escribir resultado..." 
                               onchange="updateResultado(${{index}}, this.value)">
                    </td>
                `;
                tbody.appendChild(tr);
            }});
        }}

        function toggleDone(index) {{
            data[index].estado = data[index].estado === 'COMPLETADO' ? 'PENDIENTE' : 'COMPLETADO';
            renderRows();
        }}

        function updateResultado(index, val) {{
            data[index].resultado = val;
            if (val.trim() !== '') {{
                data[index].estado = 'COMPLETADO';
            }}
            renderRows();
        }}

        function setFilter(filter) {{
            currentFilter = filter;
            renderRows();
        }}

        function filterTable() {{
            renderRows();
        }}

        function exportDataJSON() {{
            const blob = new Blob([JSON.stringify(data, null, 2)], {{ type: 'application/json' }});
            const url = URL.createObjectURL(blob);
            const a = document.createElement('a');
            a.href = url;
            a.download = 'bitacora_actualizada.json';
            a.click();
        }}

        function copyWhatsAppDigest() {{
            let msg = "*🚨 PENDIENTES DE GUARDIA - VISITA NOCTURNA*\\n";
            data.filter(i => i.es_nocturna && i.estado === 'PENDIENTE').forEach(i => {{
                msg += `\\n🔴 *Cama ${{i.cama}}* (${{i.paciente}}):\\n  • [${{i.prioridad}}] ${{i.descripcion}} (Hora: ${{i.horario || 'Nocturna'}})\\n`;
            }});
            navigator.clipboard.writeText(msg).then(() => {{
                alert('✔ Resumen para WhatsApp copiado al portapapeles!');
            }});
        }}

        renderRows();
    </script>
</body>
</html>
"""
    output_html_path.parent.mkdir(parents=True, exist_ok=True)
    output_html_path.write_text(html_content, encoding="utf-8")
    return output_html_path
