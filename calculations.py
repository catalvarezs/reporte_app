from calendar import monthrange
from dataclasses import dataclass, field, asdict
from datetime import date
from typing import Optional


@dataclass
class Insight:
    lbl: str
    texto: str
    tipo: str = ""


@dataclass
class Accion:
    actividad: str
    detalle: str
    deadline: str
    estado: str = "En proceso"


@dataclass
class ClienteRow:
    cliente: str
    canal: str
    presupuesto_mes: float
    proforma_mtd: float
    venta_mtd_real: float
    mes_anterior: float
    cumplimiento_pct: float
    mom_pct: float
    cierre_estimado: float
    gap: float
    estado: str
    insights: list = field(default_factory=list)
    acciones: list = field(default_factory=list)


def _estado(cumplimiento: float) -> str:
    if cumplimiento >= 0.95:
        return "on_track"
    if cumplimiento >= 0.80:
        return "en_riesgo"
    return "por_debajo"


def calcular_cliente(
    cliente: str,
    fecha_corte: date,
    plan_mensual: float,
    mtod: float,
    mes_anterior_mtd: float,
    canal: str = "",
    insights: Optional[list[Insight]] = None,
    acciones: Optional[list[Accion]] = None,
) -> ClienteRow:
    # Dias transcurridos incluyendo fecha_corte (coherente con que la venta MTD
    # incluye todo el dia de hoy). fecha_corte=18 → 18/31.
    dias_del_mes = monthrange(fecha_corte.year, fecha_corte.month)[1]
    dias_transcurridos = fecha_corte.day
    frac = dias_transcurridos / dias_del_mes if dias_del_mes else 0

    proforma_mtd = plan_mensual * frac
    cumplimiento = (mtod / proforma_mtd) if proforma_mtd else 0.0
    cierre_estimado = (mtod / frac) if frac else 0.0
    gap = cierre_estimado - plan_mensual
    mom_pct = ((mtod - mes_anterior_mtd) / mes_anterior_mtd) if mes_anterior_mtd else 0.0

    return ClienteRow(
        cliente=cliente,
        canal=canal,
        presupuesto_mes=plan_mensual,
        proforma_mtd=proforma_mtd,
        venta_mtd_real=mtod,
        mes_anterior=mes_anterior_mtd,
        cumplimiento_pct=cumplimiento,
        mom_pct=mom_pct,
        cierre_estimado=cierre_estimado,
        gap=gap,
        estado=_estado(cumplimiento),
        insights=insights or [],
        acciones=acciones or [],
    )


_ESTADO_LABEL = {
    "on_track": "On Track",
    "en_riesgo": "En Riesgo",
    "por_debajo": "Por Debajo",
}


def build_report_context(
    rows: list[ClienteRow],
    mes_label: str,
    fecha_corte: date,
    kam_name: str = "Fernanda",
    proxima_revision: str = "",
) -> dict:
    if not rows:
        return {
            "mes_label": mes_label,
            "fecha_corte_str": fecha_corte.strftime("%d.%m.%Y"),
            "kam_name": kam_name,
            "num_cuentas": 0,
            "canales_activos": "",
            "rows": [],
            "total": {},
            "closing": {},
        }

    total_presupuesto = sum(r.presupuesto_mes for r in rows)
    total_proforma = sum(r.proforma_mtd for r in rows)
    total_mtd = sum(r.venta_mtd_real for r in rows)
    total_ma = sum(r.mes_anterior for r in rows)
    total_cierre = sum(r.cierre_estimado for r in rows)

    total_cumplimiento = (total_mtd / total_proforma) if total_proforma else 0.0
    total_mom = ((total_mtd - total_ma) / total_ma) if total_ma else 0.0

    mejor = max(rows, key=lambda r: r.cumplimiento_pct)
    en_riesgo = [r for r in rows if r.cumplimiento_pct < 0.80]

    canales = sorted({c for r in rows for c in r.canal.split(" + ") if c})

    enriched_rows = []
    for r in rows:
        d = asdict(r)
        d["estado_label"] = _ESTADO_LABEL.get(r.estado, r.estado)
        enriched_rows.append(d)

    return {
        "mes_label": mes_label,
        "fecha_corte_str": fecha_corte.strftime("%d.%m.%Y"),
        "kam_name": kam_name,
        "num_cuentas": len(rows),
        "canales_activos": " · ".join(canales) if canales else "",
        "rows": enriched_rows,
        "total": {
            "presupuesto_mes": total_presupuesto,
            "proforma_mtd": total_proforma,
            "venta_mtd_real": total_mtd,
            "cierre_estimado": total_cierre,
            "cumplimiento_pct": total_cumplimiento,
            "mom_pct": total_mom,
            "estado": _estado(total_cumplimiento),
            "estado_label": _ESTADO_LABEL.get(_estado(total_cumplimiento), ""),
        },
        "closing": {
            "cierre_estimado": total_cierre,
            "cumplimiento_pct": total_cumplimiento,
            "mejor_cuenta": {
                "cliente": mejor.cliente,
                "cumplimiento_pct": mejor.cumplimiento_pct,
                "mom_pct": mejor.mom_pct,
                "gap": mejor.gap,
            },
            "en_riesgo": [r.cliente for r in en_riesgo],
            "proxima_revision": proxima_revision,
        },
    }
