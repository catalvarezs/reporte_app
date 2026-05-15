"""
Cliente al MCP de Instance (https://backendia.instancelatam.com/sse).

Tablas BI consultadas (requieren rol con acceso a tablas BI en el MCP):
  - panel_comercial:        mtod, ventas_lm, ventas_mom, ventas_mom_lastyear, last_mtod, fecha_lectura
  - consenso_cliente_mes:   plan_mensual, ppto, estado (filtrado por mes en curso + APROBADO)

Auth: Bearer con el access_token que la app obtiene via OAuth y guarda en la sesion firmada.
"""
import json
import logging
from contextlib import asynccontextmanager
from datetime import date
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


def _escape_sql_str(s: str) -> str:
    return s.replace("'", "''")


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
    """Llama al tool `query` del MCP. Devuelve (filas_parseadas, respuesta_cruda)."""
    _logger.info("SQL >>> %s", sql)
    try:
        result = await session.call_tool("query", {"sql": sql})
    except Exception as e:
        _logger.exception("call_tool exception: %s", e)
        raise MCPQueryError(f"call_tool('query') fallo: {e}", sql=sql) from e
    raw = _dump_content(result)
    is_err = bool(getattr(result, "isError", False))
    _logger.info("isError=%s RAW <<< %s", is_err, raw)
    if is_err:
        raise MCPQueryError("MCP devolvio isError=True", sql=sql, raw=raw)
    rows = _parse_query_result(result)
    _logger.info("PARSED %d row(s): %s", len(rows), rows[:3] if rows else [])
    return rows, raw


async def fetch_ventas_resumen(
    session: ClientSession,
    cliente: str,
    pais: str,
    fecha_corte: date,
) -> dict:
    """
    Devuelve para un cliente/pais:
      - mtod (panel_comercial.mtod)
      - mes_anterior_mtd (panel_comercial.ventas_lm)
      - plan_mensual (consenso_cliente_mes.plan_mensual con estado=APROBADO del mes en curso)
      - ventas_mom, ventas_mom_lastyear (panel_comercial, informativos)
      - canal (vacio por ahora — pendiente decidir fuente)
    Mas keys con prefijo "_" con SQLs y respuestas crudas para debug.
    """
    mes_iso = fecha_corte.strftime("%Y-%m")
    c = _escape_sql_str(cliente)
    p = _escape_sql_str(pais)

    sql_panel = (
        "SELECT mtod, ventas_lm, ventas_mom, ventas_mom_lastyear, last_mtod, fecha_lectura "
        "FROM panel_comercial "
        f"WHERE cliente = '{c}' AND pais = '{p}' "
        "ORDER BY fecha_lectura DESC LIMIT 1"
    )
    sql_consenso = (
        "SELECT plan_mensual, ppto, estado "
        "FROM consenso_cliente_mes "
        f"WHERE cliente = '{c}' AND pais = '{p}' AND mes = '{mes_iso}' AND estado = 'APROBADO' "
        "ORDER BY fecha_lectura DESC LIMIT 1"
    )

    panel_rows, panel_raw = await _run_query(session, sql_panel)
    panel = panel_rows[0] if panel_rows else {}

    consenso_rows, consenso_raw = await _run_query(session, sql_consenso)
    consenso = consenso_rows[0] if consenso_rows else {}

    return {
        "mtod": float(panel.get("mtod") or 0),
        "mes_anterior_mtd": float(panel.get("ventas_lm") or 0),
        "plan_mensual": float(consenso.get("plan_mensual") or 0),
        "ventas_mom": panel.get("ventas_mom"),
        "ventas_mom_lastyear": panel.get("ventas_mom_lastyear"),
        "canal": "",
        "_panel_sql": sql_panel,
        "_panel_raw": panel_raw,
        "_consenso_sql": sql_consenso,
        "_consenso_raw": consenso_raw,
        "_empty": not panel_rows and not consenso_rows,
    }
