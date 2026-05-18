"""
Cliente al MCP de Instance (https://backendia.instancelatam.com/sse).

Lee ventas reales desde `products_in_orders` agregando por cliente:
  - mtod              = SUM(qty * precio) del mes en curso, [primero_del_mes, fecha_corte)
  - mes_anterior_mtd  = SUM(qty * precio) del mes anterior espejando el dia
  - canal             = canales activos en la ventana [mes anterior .. fecha_corte)

Nota: `products_in_orders` es line-item-level y es la fuente que matchea el
dashboard de Looker. `orders.precio_sin_shipping` esta inflado para algunos
canales (ej. Vtex), asi que NO se usa para el monto.

Pais queda en blanco aqui: el LEFT JOIN a `orders` para traerlo agotaba el
timeout del MCP, y el llamador hace match por cliente (no por pais).

El plan_mensual NO viene del MCP — se ingresa en el formulario.

Las queries envuelven cada tabla en un subquery para que el rewrite server-side
(MYSQL_ORDERS_SCOPE_*) quede contenido adentro y no rompa WHERE/GROUP BY.
"""
import json
import logging
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, timedelta
from pathlib import Path
from typing import AsyncIterator, Optional

from mcp import ClientSession
from mcp.client.sse import sse_client

MCP_SSE_URL = "https://backendia.instancelatam.com/sse"

_LOG_PATH = Path(__file__).resolve().parents[1] / "debug.log"
_logger = logging.getLogger("instance_mcp")
if not _logger.handlers:
    _logger.setLevel(logging.DEBUG)
    _handler = logging.FileHandler(_LOG_PATH, mode="a", encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s | %(message)s"))
    _logger.addHandler(_handler)
    _logger.propagate = False


class MCPQueryError(Exception):
    def __init__(self, message: str, sql: Optional[str] = None, raw: Optional[str] = None):
        super().__init__(message)
        self.sql = sql
        self.raw = raw


@asynccontextmanager
async def mcp_session(access_token: str) -> AsyncIterator[ClientSession]:
    headers = {"Authorization": f"Bearer {access_token}"}
    async with sse_client(MCP_SSE_URL, headers=headers) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            yield session


def _dump_content(result) -> str:
    if not result or not getattr(result, "content", None):
        return ""
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n---\n".join(parts)


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


async def _run_query(session: ClientSession, sql: str) -> tuple[list[dict], str]:
    _logger.info("SQL >>> %s", sql)
    try:
        result = await session.call_tool("query", {"sql": sql})
    except Exception as e:
        _logger.exception("call_tool exception: %s", e)
        raise MCPQueryError(f"call_tool('query') fallo: {e}", sql=sql) from e
    raw = _dump_content(result)
    is_err = bool(getattr(result, "isError", False))
    _logger.info("isError=%s RAW <<< %s", is_err, raw[:500])
    if is_err:
        raise MCPQueryError("MCP devolvio isError=True", sql=sql, raw=raw)
    rows = _parse_query_result(result)
    _logger.info("PARSED %d row(s)", len(rows))
    return rows, raw


def _prev_month_window(fecha_corte: date) -> tuple[date, date]:
    """[prev_month_first, prev_month_end_exclusive] espejando fecha_corte (inclusivo).

    Para fecha_corte=2026-05-18, devuelve (2026-04-01, 2026-04-19) — incluye
    todo el dia 18 de abril, equivalente a "datos hasta el final de ese dia".
    """
    if fecha_corte.month == 1:
        prev_year, prev_month = fecha_corte.year - 1, 12
    else:
        prev_year, prev_month = fecha_corte.year, fecha_corte.month - 1
    prev_start = date(prev_year, prev_month, 1)
    last_day = monthrange(prev_year, prev_month)[1]
    end_day = min(fecha_corte.day, last_day)
    return prev_start, date(prev_year, prev_month, end_day) + timedelta(days=1)


async def list_clientes_in_scope(session: ClientSession) -> list[dict]:
    """Devuelve [{cliente, pais}, ...] de todos los clientes accesibles en la sesion."""
    sql = (
        "SELECT cliente, pais FROM "
        "(SELECT cliente, pais FROM orders) o "
        "GROUP BY cliente, pais"
    )
    rows, _ = await _run_query(session, sql)
    return [{"cliente": r.get("cliente"), "pais": r.get("pais")} for r in rows if r.get("cliente")]


# Estados que NO se cuentan como venta. El dashboard mantiene 'partially_refunded'
# (la parte cobrada cuenta), 'paid', 'delivered', 'shipped', 'pending', 'invoiced',
# 'ready_to_ship', 'ready-for-handling'. No filtramos por estado_pago porque
# products_in_orders no expone esa columna y el dashboard tampoco lo necesita.
_ESTADOS_OC_EXCLUIDOS = ("cancelled", "canceled", "returned", "refunded")


def _sql_filtro_estados() -> str:
    estados_oc = ",".join(f"'{e}'" for e in _ESTADOS_OC_EXCLUIDOS)
    return f"estado_oc NOT IN ({estados_oc})"


async def fetch_ventas_por_cliente(
    session: ClientSession,
    fecha_corte: date,
) -> tuple[dict[tuple[str, str], dict], str, str]:
    """
    Devuelve dict {(cliente, pais): {mtod, mes_anterior_mtd, canal}} para TODOS los clientes
    de la sesion. El llamador filtra por los que le interesan.
    Tambien devuelve (sql, raw) para debug.
    """
    # fecha_corte es la fecha del snapshot ("hoy"); incluimos TODO el dia
    # de fecha_corte (datos hasta el final de hoy), por eso +1 dia exclusivo.
    cur_start = fecha_corte.replace(day=1)
    cur_end = fecha_corte + timedelta(days=1)
    prev_start, prev_end = _prev_month_window(fecha_corte)
    flt = _sql_filtro_estados()

    # No hacemos JOIN con orders para traer pais: el LEFT JOIN sobre el
    # rewrite del scope explotaba el tiempo limite del MCP. El llamador hace
    # match por cliente (no por pais), asi que pais queda vacio aqui.
    sql = (
        "SELECT cliente, "
        f"ROUND(SUM(CASE WHEN fecha_creacion >= '{cur_start}' AND fecha_creacion < '{cur_end}' AND {flt} THEN qty * precio ELSE 0 END)) AS mtod, "
        f"ROUND(SUM(CASE WHEN fecha_creacion >= '{prev_start}' AND fecha_creacion < '{prev_end}' AND {flt} THEN qty * precio ELSE 0 END)) AS mes_anterior_mtd, "
        f"GROUP_CONCAT(DISTINCT CASE WHEN fecha_creacion >= '{prev_start}' AND fecha_creacion < '{cur_end}' AND {flt} THEN canal_de_venta END SEPARATOR ' + ') AS canal "
        "FROM (SELECT cliente, fecha_creacion, qty, precio, canal_de_venta, estado_oc FROM products_in_orders) o "
        "GROUP BY cliente"
    )
    rows, raw = await _run_query(session, sql)
    out: dict[tuple[str, str], dict] = {}
    for r in rows:
        key = (r.get("cliente") or "", "")
        out[key] = {
            "mtod": float(r.get("mtod") or 0),
            "mes_anterior_mtd": float(r.get("mes_anterior_mtd") or 0),
            "canal": (r.get("canal") or "").strip(" +") or "",
        }
    return out, sql, raw
