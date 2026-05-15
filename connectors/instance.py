"""
Cliente al MCP de Instance (https://backendia.instancelatam.com/sse).

Reemplaza el viejo acceso Postgres directo. La auth es Bearer con el access_token
que la app obtiene via OAuth y guarda en la sesion firmada.

Las tablas reales que expone este MCP son: clients, orders, products_in_orders,
buybox, healthcheck. No existe panel_comercial ni consenso_cliente_mes — el plan
mensual lo introduce el usuario en el formulario.
"""
import json
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, timedelta
from typing import AsyncIterator, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_SSE_URL = "https://backendia.instancelatam.com/sse"


@asynccontextmanager
async def mcp_session(access_token: str) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with sse_client(MCP_SSE_URL, headers=headers) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _escape_sql_str(s: str) -> str:
    return s.replace("'", "''")


def _prev_month_window(fecha_corte: date) -> tuple[date, date]:
    """[prev_month_first, prev_month_end_exclusive] mirroring fecha_corte's day-of-month."""
    if fecha_corte.month == 1:
        prev_year, prev_month = fecha_corte.year - 1, 12
    else:
        prev_year, prev_month = fecha_corte.year, fecha_corte.month - 1
    prev_start = date(prev_year, prev_month, 1)
    last_day = monthrange(prev_year, prev_month)[1]
    end_day = min(fecha_corte.day, last_day)
    return prev_start, date(prev_year, prev_month, end_day) + timedelta(days=1)


def _parse_query_result(result) -> list[dict]:
    if not result or not getattr(result, "content", None):
        return []
    for block in result.content:
        text = getattr(block, "text", None)
        if not text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("rows", "data", "result", "results"):
                v = data.get(key)
                if isinstance(v, list):
                    return v
            return [data]
    return []


async def fetch_ventas_resumen(
    session: ClientSession,
    cliente: str,
    pais: str,
    fecha_corte: date,
) -> Optional[dict]:
    """Devuelve mtod, mes_anterior_mtd y canal para un cliente/pais hasta fecha_corte (inclusive)."""
    cur_start = fecha_corte.replace(day=1)
    cur_end = fecha_corte + timedelta(days=1)
    prev_start, prev_end = _prev_month_window(fecha_corte)

    sql = (
        "SELECT "
        f"COALESCE(SUM(CASE WHEN fecha_creacion >= '{cur_start}' AND fecha_creacion < '{cur_end}' THEN precio_sin_shipping ELSE 0 END), 0) AS mtod, "
        f"COALESCE(SUM(CASE WHEN fecha_creacion >= '{prev_start}' AND fecha_creacion < '{prev_end}' THEN precio_sin_shipping ELSE 0 END), 0) AS mes_anterior_mtd, "
        "GROUP_CONCAT(DISTINCT canal_de_venta SEPARATOR ' + ') AS canal "
        "FROM orders "
        f"WHERE cliente = '{_escape_sql_str(cliente)}' "
        f"AND pais = '{_escape_sql_str(pais)}' "
        f"AND fecha_creacion >= '{prev_start}' "
        f"AND fecha_creacion < '{cur_end}'"
    )

    result = await session.call_tool("query", {"sql": sql})
    rows = _parse_query_result(result)
    if not rows:
        return None
    row = rows[0]
    return {
        "mtod": float(row.get("mtod") or 0),
        "mes_anterior_mtd": float(row.get("mes_anterior_mtd") or 0),
        "canal": row.get("canal") or "",
    }
