"""Motor de diferencias clínicas (Diff Engine) entre bitácoras consecutivas."""

from dataclasses import dataclass, field

from bitacora.models import BitacoraGuardia, Paciente, Pendiente


@dataclass
class PacienteDiff:
    """Cambios detectados para un paciente en particular."""

    cama: str
    nombre: str
    hc: str
    nuevos_pendientes: list[Pendiente] = field(default_factory=list)
    pendientes_resueltos: list[Pendiente] = field(default_factory=list)
    pendientes_persistentes: list[Pendiente] = field(default_factory=list)
    alertas_nuevas: list[str] = field(default_factory=list)
    antibioticos_cambios: list[str] = field(default_factory=list)


@dataclass
class BitacoraDiffResult:
    """Resultado consolidado del análisis comparativo entre dos turnos."""

    fecha_anterior: str
    fecha_actual: str
    ingresos_nuevos: list[Paciente] = field(default_factory=list)
    egresos_o_traslados: list[Paciente] = field(default_factory=list)
    pacientes_modificados: list[PacienteDiff] = field(default_factory=list)
    total_pendientes_nocturnos_activos: int = 0


def compare_bitacoras(anterior: BitacoraGuardia, actual: BitacoraGuardia) -> BitacoraDiffResult:
    """Compara la bitácora anterior con la actual y detecta cambios clínicamente relevantes."""
    map_anterior = {p.hc: p for p in anterior.pacientes}
    map_actual = {p.hc: p for p in actual.pacientes}

    # Ingresos y egresos
    ingresos = [p for hc, p in map_actual.items() if hc not in map_anterior]
    egresos = [p for hc, p in map_anterior.items() if hc not in map_actual]

    pacientes_modificados: list[PacienteDiff] = []
    total_nocturnos = 0

    for hc, pac_actual in map_actual.items():
        # Contabilizar pendientes nocturnos
        total_nocturnos += sum(
            1
            for pend in pac_actual.pendientes
            if pend.es_visita_nocturna and pend.estado == "PENDIENTE"
        )

        if hc not in map_anterior:
            continue

        pac_ant = map_anterior[hc]
        diff_pac = PacienteDiff(
            cama=pac_actual.cama,
            nombre=pac_actual.nombre_completo,
            hc=pac_actual.hc,
        )

        # Mapeo de pendientes por ID
        pends_ant_map = {p.id: p for p in pac_ant.pendientes}
        pends_act_map = {p.id: p for p in pac_actual.pendientes}

        # Nuevos pendientes
        for pid, p in pends_act_map.items():
            if pid not in pends_ant_map:
                diff_pac.nuevos_pendientes.append(p)
            elif p.estado == "PENDIENTE" and pends_ant_map[pid].estado == "PENDIENTE":
                diff_pac.pendientes_persistentes.append(p)

        # Pendientes resueltos
        for pid, p_ant in pends_ant_map.items():
            if pid not in pends_act_map or (
                pends_act_map[pid].estado in ("COMPLETADO", "CANCELADO")
                and p_ant.estado == "PENDIENTE"
            ):
                diff_pac.pendientes_resueltos.append(p_ant)

        # Nuevas alertas
        alertas_ant_set = set(pac_ant.alertas_criticas)
        for alerta in pac_actual.alertas_criticas:
            if alerta not in alertas_ant_set:
                diff_pac.alertas_nuevas.append(alerta)

        # Cambios en antibióticos y suspensiones
        meds_ant = {m.nombre.lower(): m for m in pac_ant.medicaciones}
        for med in pac_actual.medicaciones:
            m_ant = meds_ant.get(med.nombre.lower())
            if not m_ant:
                if med.dias_tratamiento:
                    diff_pac.antibioticos_cambios.append(
                        f"NUEVO ANTIBIÓTICO: {med.nombre} {med.dosis} ({med.dias_tratamiento})"
                    )
            else:
                if med.dias_tratamiento != m_ant.dias_tratamiento and med.dias_tratamiento:
                    diff_pac.antibioticos_cambios.append(
                        f"AVANCE TERAPIA: {med.nombre} progresó a {med.dias_tratamiento}"
                    )
                if med.suspendido and not m_ant.suspendido:
                    diff_pac.antibioticos_cambios.append(f"FARMACO SUSPENDIDO: {med.nombre}")

        if (
            diff_pac.nuevos_pendientes
            or diff_pac.pendientes_resueltos
            or diff_pac.alertas_nuevas
            or diff_pac.antibioticos_cambios
        ):
            pacientes_modificados.append(diff_pac)

    return BitacoraDiffResult(
        fecha_anterior=anterior.fecha_guardia,
        fecha_actual=actual.fecha_guardia,
        ingresos_nuevos=ingresos,
        egresos_o_traslados=egresos,
        pacientes_modificados=pacientes_modificados,
        total_pendientes_nocturnos_activos=total_nocturnos,
    )
