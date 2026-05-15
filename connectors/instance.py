"""
Conector a la base de datos de Instance.

IMPORTANTE: el modo exacto de acceso queda pendiente. La skill habla del
"MCP de Instance" — en runtime el web app debe ir directo a la fuente sin
pasar por un LLM. Las dos rutas posibles son:

  1. Conexión SQL directa (Postgres/MySQL) si la BD acepta conexiones
     externas. Setear DB_URL en .env (ej. postgresql://user:pass@host/db).
  2. Endpoint HTTP propio de Instance si lo expone como API.

Las funciones aquí asumen ruta (1). Si es (2), reemplazar el cuerpo de
cada función por una llamada httpx al endpoint correspondiente.
"""
import os
from datetime import date
from typing import Optional


def _connect():
    db_url = os.environ.get("INSTANCE_DB_URL")
    if not db_url:
        raise RuntimeError("INSTANCE_DB_URL no configurada en .env")
    import psycopg
    return psycopg.connect(db_url)


def fetch_panel_comercial(cliente: str, pais: str = "Chile") -> Optional[dict]:
    sql = """
        SELECT mtod, ventas_lm, ventas_mom, ventas_mom_lastyear, last_mtod, fecha_lectura
        FROM panel_comercial
        WHERE cliente = %s AND pais = %s
        ORDER BY fecha_lectura DESC
        LIMIT 1
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (cliente, pais))
        row = cur.fetchone()
    if not row:
        return None
    cols = ["mtod", "ventas_lm", "ventas_mom", "ventas_mom_lastyear", "last_mtod", "fecha_lectura"]
    return dict(zip(cols, row))


def fetch_consenso(cliente: str, mes: str, pais: str = "Chile") -> Optional[dict]:
    sql = """
        SELECT plan_mensual, ppto, estado
        FROM consenso_cliente_mes
        WHERE cliente = %s AND mes = %s AND pais = %s AND estado = 'APROBADO'
        ORDER BY fecha_lectura DESC NULLS LAST
        LIMIT 1
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (cliente, mes, pais))
        row = cur.fetchone()
    if not row:
        return None
    return {"plan_mensual": row[0], "ppto": row[1], "estado": row[2]}


def fetch_daily_sales(cliente: str, mes: str, pais: str = "Chile") -> list[dict]:
    sql = """
        SELECT fecha, cliente, canal, logistica, venta_dia, pedidos, unidades
        FROM daily_sales_by_brand
        WHERE cliente = %s AND TO_CHAR(fecha, 'YYYY-MM') = %s AND pais = %s
        ORDER BY fecha ASC
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (cliente, mes, pais))
        rows = cur.fetchall()
    cols = ["fecha", "cliente", "canal", "logistica", "venta_dia", "pedidos", "unidades"]
    return [dict(zip(cols, r)) for r in rows]


def fetch_aceleracion(cliente: str, dias_recientes: int = 7) -> Optional[dict]:
    sql = """
        SELECT total_venta, total_visitas, total_cantidad, total_pedidos, fecha_lectura
        FROM formula_aceleracion
        WHERE cliente = %s AND fecha_lectura >= CURRENT_DATE - INTERVAL '%s days'
        ORDER BY fecha_lectura DESC
        LIMIT 1
    """
    with _connect() as conn, conn.cursor() as cur:
        cur.execute(sql, (cliente, dias_recientes))
        row = cur.fetchone()
    if not row:
        return None
    cols = ["total_venta", "total_visitas", "total_cantidad", "total_pedidos", "fecha_lectura"]
    return dict(zip(cols, row))
