import asyncio
import base64
import hashlib
import os
import re
import secrets
import sys
import time
import traceback
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from calculations import (
    build_report_context,
    calcular_cliente,
    ClienteRow,
    Insight,
    Accion,
)


def _fmt_deadline(raw: str) -> str:
    """yyyy-mm-dd -> dd.mm.yyyy; pasa cualquier otro string tal cual."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return datetime.strptime(raw, "%Y-%m-%d").strftime("%d.%m.%Y")
    except ValueError:
        return raw


# Cache de reportes generados, por id corto. TTL para que no crezca para siempre.
_REPORT_CACHE: dict[str, dict] = {}
_REPORT_TTL_SEC = 30 * 60
_REPORT_MAX_ENTRIES = 30


def _cache_report(context: dict) -> str:
    now = time.time()
    expired = [k for k, v in _REPORT_CACHE.items() if now - v["ts"] > _REPORT_TTL_SEC]
    for k in expired:
        _REPORT_CACHE.pop(k, None)
    if len(_REPORT_CACHE) >= _REPORT_MAX_ENTRIES:
        oldest = min(_REPORT_CACHE.items(), key=lambda kv: kv[1]["ts"])[0]
        _REPORT_CACHE.pop(oldest, None)
    rid = secrets.token_urlsafe(8)
    _REPORT_CACHE[rid] = {"ts": now, "context": context}
    return rid


def _slug_filename(text: str) -> str:
    """Para usar en Content-Disposition: 'Update Comercial - Mayo 2026' -> 'update-comercial-mayo-2026'."""
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-zA-Z0-9]+", "-", s).strip("-").lower()
    return s or "reporte"
import charts
from connectors.instance import (
    MCPQueryError,
    fetch_evolucion_mensual,
    fetch_ventas_por_cliente,
    list_clientes_in_scope,
    mcp_session,
)

load_dotenv()

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

# El recurso /sse tiene su PROPIO authorization server (issuer .../sse), distinto
# del AS de la base. /sse solo acepta tokens emitidos por estos endpoints; usar los
# de la base (.../authorize, .../token) da 401. Descubierto via
# /.well-known/oauth-authorization-server/sse.
OAUTH_AUTHORIZE_URL = "https://backendia.instancelatam.com/sse/authorize"
OAUTH_TOKEN_URL = "https://backendia.instancelatam.com/sse/token"
# RFC 8707 Resource Indicator: el MCP exige que el access_token quede "bound" a
# este recurso; sin esto /sse responde 401. Debe coincidir con el campo
# "resource" de /.well-known/oauth-protected-resource/sse.
OAUTH_RESOURCE = "https://backendia.instancelatam.com/sse"
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET")

if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET no configurada en .env")
if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("CLIENT_ID y CLIENT_SECRET deben estar en .env")

app = FastAPI(title="Update Comercial")
# En produccion (HTTPS) conviene COOKIE_SECURE=true para marcar la cookie Secure;
# en local sobre http queda en false para que la sesion funcione igual.
_COOKIE_SECURE = os.getenv("COOKIE_SECURE", "").lower() in ("1", "true", "yes")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="reporte_session",
    same_site="lax",
    https_only=_COOKIE_SECURE,
    max_age=60 * 60 * 8,
)

_MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def _mes_label(mes_iso: str) -> str:
    y, m = mes_iso.split("-")
    return f"{_MESES_ES[int(m)]} {y}"


_MESES_ABBR = {
    1: "Ene", 2: "Feb", 3: "Mar", 4: "Abr", 5: "May", 6: "Jun",
    7: "Jul", 8: "Ago", 9: "Sep", 10: "Oct", 11: "Nov", 12: "Dic",
}


def _add_months(d: date, n: int) -> date:
    """Suma n meses (puede ser negativo) al primer dia del mes de d."""
    total = (d.year * 12 + (d.month - 1)) + n
    return date(total // 12, total % 12 + 1, 1)


def _month_seq(desde: date, hasta: date) -> list[date]:
    """Lista de primeros-de-mes desde 'desde' hasta el mes de 'hasta', inclusive."""
    out: list[date] = []
    cur = desde.replace(day=1)
    end = hasta.replace(day=1)
    while cur <= end:
        out.append(cur)
        cur = _add_months(cur, 1)
    return out


def _mes_corto(d: date) -> str:
    """date -> 'May 26' (etiqueta corta para el eje X de los graficos)."""
    return f"{_MESES_ABBR[d.month]} {d.year % 100:02d}"


def _pkce_pair() -> tuple[str, str]:
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    digest = hashlib.sha256(verifier.encode()).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
    return verifier, challenge


def _is_authenticated(request: Request) -> bool:
    token = request.session.get("oauth")
    if not token or not token.get("access_token"):
        return False
    expires_at = token.get("expires_at")
    if not expires_at:
        return True
    return datetime.fromisoformat(expires_at) > datetime.now(timezone.utc)


def _find_in_group(exc: BaseException, cls: type) -> BaseException | None:
    """Busca una excepcion de tipo cls dentro de un (posible) ExceptionGroup.

    sse_client / ClientSession envuelven los errores en anyio TaskGroups, asi que
    ni un 401 (httpx.HTTPStatusError) ni un MCPQueryError llegan directos: vienen
    anidados en uno o mas ExceptionGroup. Recorremos sub-excepciones y __cause__.
    """
    if isinstance(exc, cls):
        return exc
    for sub in getattr(exc, "exceptions", None) or ():
        found = _find_in_group(sub, cls)
        if found is not None:
            return found
    cause = exc.__cause__
    if cause is not None:
        return _find_in_group(cause, cls)
    return None


def _first_http_status_error(exc: BaseException) -> httpx.HTTPStatusError | None:
    return _find_in_group(exc, httpx.HTTPStatusError)  # type: ignore[return-value]


def _callback_url(request: Request) -> str:
    return str(request.url_for("callback"))


def _cliente_slug(cliente: str) -> str:
    """Slug seguro para usar como sufijo de name= en inputs (sin perder reversibilidad)."""
    return re.sub(r"[^a-zA-Z0-9]+", "_", cliente).strip("_")


def _parse_monto(raw: str) -> float:
    cleaned = (raw or "").strip().replace(".", "").replace(" ", "").replace(",", "")
    if not cleaned:
        return 0.0
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


@app.get("/", response_class=HTMLResponse)
async def home(request: Request):
    authenticated = _is_authenticated(request)
    clientes_disponibles: list[dict] = []
    error_clientes: str | None = None
    if authenticated:
        token = request.session.get("oauth", {}).get("access_token") or ""
        try:
            async with mcp_session(token) as mcp:
                clientes_disponibles = await list_clientes_in_scope(mcp)
        except MCPQueryError as e:
            error_clientes = str(e)
        except Exception as e:
            # Los errores del MCP llegan envueltos en ExceptionGroup (anyio); un
            # 401/403 (token vencido) -> reautenticar; el resto se muestra al usuario.
            http_err = _first_http_status_error(e)
            if http_err is not None and http_err.response.status_code in (401, 403):
                request.session.clear()
                return RedirectResponse("/login", status_code=302)
            if http_err is not None:
                error_clientes = f"MCP rechazo la conexion: HTTP {http_err.response.status_code} en {http_err.request.url}"
            else:
                error_clientes = str(e)

    today = date.today()
    for c in clientes_disponibles:
        c["slug"] = _cliente_slug(c["cliente"])

    return templates.TemplateResponse(
        "form.html",
        {
            "request": request,
            "authenticated": authenticated,
            "clientes_disponibles": clientes_disponibles,
            "error_clientes": error_clientes,
            "default_mes": today.strftime("%Y-%m"),
            "default_fecha_corte": today.strftime("%Y-%m-%d"),
            # Por defecto, los graficos arrancan 5 meses atras (6 meses en total).
            "default_grafico_desde": _add_months(today, -5).strftime("%Y-%m"),
        },
    )


@app.get("/login")
def login(request: Request):
    verifier, challenge = _pkce_pair()
    state = secrets.token_urlsafe(32)
    request.session["pkce_verifier"] = verifier
    request.session["oauth_state"] = state
    params = {
        "response_type": "code",
        "client_id": CLIENT_ID,
        "redirect_uri": _callback_url(request),
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "resource": OAUTH_RESOURCE,
    }
    return RedirectResponse(f"{OAUTH_AUTHORIZE_URL}?{urlencode(params)}", status_code=302)


@app.get("/callback", name="callback")
def callback(
    request: Request,
    code: str | None = None,
    state: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
):
    if error:
        detail = error_description or error
        raise HTTPException(status_code=400, detail=f"OAuth error: {detail}")
    if not code or not state:
        raise HTTPException(status_code=400, detail="Faltan parametros code/state")

    expected_state = request.session.pop("oauth_state", None)
    verifier = request.session.pop("pkce_verifier", None)
    if not expected_state or not secrets.compare_digest(state, expected_state):
        raise HTTPException(status_code=400, detail="State invalido")
    if not verifier:
        raise HTTPException(status_code=400, detail="Falta PKCE verifier en la sesion")

    data = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": _callback_url(request),
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "code_verifier": verifier,
        "resource": OAUTH_RESOURCE,
    }
    resp = httpx.post(OAUTH_TOKEN_URL, data=data, timeout=30.0)
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"Token exchange fallo ({resp.status_code}): {resp.text}",
        )

    payload = resp.json()
    expires_in = int(payload.get("expires_in", 3600))
    expires_at = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()
    request.session["oauth"] = {
        "access_token": payload.get("access_token"),
        "refresh_token": payload.get("refresh_token"),
        "token_type": payload.get("token_type", "Bearer"),
        "expires_at": expires_at,
    }
    return RedirectResponse("/", status_code=302)


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=302)


@app.post("/generar", response_class=HTMLResponse)
async def generar(request: Request):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    form = await request.form()
    clientes_sel = [c for c in form.getlist("clientes_sel") if c.strip()]
    mes = form.get("mes", "")
    fecha_corte_raw = form.get("fecha_corte", "")
    kam_name = (form.get("kam_name") or "").strip() or "Catalina"

    if not clientes_sel or not mes or not fecha_corte_raw:
        return RedirectResponse("/", status_code=302)

    fecha_corte_d = datetime.strptime(fecha_corte_raw, "%Y-%m-%d").date()

    # Mes de inicio de los graficos de evolucion. Default: 5 meses atras.
    try:
        grafico_desde_d = datetime.strptime(form.get("grafico_desde", ""), "%Y-%m").date()
    except ValueError:
        grafico_desde_d = _add_months(fecha_corte_d, -5)
    grafico_desde_d = grafico_desde_d.replace(day=1)
    if grafico_desde_d > fecha_corte_d.replace(day=1):
        grafico_desde_d = fecha_corte_d.replace(day=1)

    planes: dict[str, float] = {}
    grupos_raw: dict[str, str] = {}
    cliente_desde: dict[str, date] = {}
    tope_mes = fecha_corte_d.replace(day=1)
    for cliente in clientes_sel:
        slug = _cliente_slug(cliente)
        planes[cliente] = _parse_monto(form.get(f"plan_{slug}", ""))
        grupos_raw[cliente] = (form.get(f"grupo_{slug}", "") or "").strip()
        # Override de rango de grafico por cliente; si esta vacio usa el global.
        try:
            od = datetime.strptime(form.get(f"gdesde_{slug}", ""), "%Y-%m").date().replace(day=1)
        except ValueError:
            od = grafico_desde_d
        cliente_desde[cliente] = min(od, tope_mes)
    # La query trae el rango mas amplio pedido; cada grupo recorta el suyo.
    global_min_desde = min(cliente_desde.values()) if cliente_desde else grafico_desde_d

    acciones_por_cliente: dict[str, list[Accion]] = {c: [] for c in clientes_sel}
    a_cli = form.getlist("accion_cliente")
    a_act = form.getlist("accion_actividad")
    a_det = form.getlist("accion_detalle")
    a_dl = form.getlist("accion_deadline")
    a_est = form.getlist("accion_estado")
    for i in range(len(a_cli)):
        cli = (a_cli[i] or "").strip()
        actividad = (a_act[i] if i < len(a_act) else "").strip()
        if not cli or not actividad or cli not in acciones_por_cliente:
            continue
        acciones_por_cliente[cli].append(Accion(
            actividad=actividad,
            detalle=(a_det[i] if i < len(a_det) else "").strip(),
            deadline=_fmt_deadline(a_dl[i] if i < len(a_dl) else ""),
            estado=(a_est[i] if i < len(a_est) else "En proceso").strip() or "En proceso",
        ))

    access_token = request.session["oauth"]["access_token"]
    rows: list[ClienteRow] = []
    debug_info: list[dict] = []
    sql_str = ""
    raw_str = ""

    evol_rows: list[dict] = []
    try:
        async with mcp_session(access_token) as mcp:
            por_cliente, sql_str, raw_str = await fetch_ventas_por_cliente(mcp, fecha_corte_d)
            evol_rows, _, _ = await fetch_evolucion_mensual(mcp, global_min_desde, fecha_corte_d)
    except Exception as e:
        # MCPQueryError y httpx.HTTPStatusError llegan envueltos en ExceptionGroup
        # (anyio), asi que desenvolvemos para mostrar el error real y no un generico.
        http_err = _first_http_status_error(e)
        if http_err is not None and http_err.response.status_code in (401, 403):
            request.session.clear()
            return RedirectResponse("/login", status_code=302)
        mcp_err = _find_in_group(e, MCPQueryError)
        if mcp_err is not None:
            return templates.TemplateResponse("error.html", {
                "request": request,
                "title": "MCP devolvio error",
                "error": str(mcp_err),
                "trace": None,
                "debug_info": [{"sql": mcp_err.sql, "raw": mcp_err.raw, "error": str(mcp_err)}],
            })
        if http_err is not None:
            return templates.TemplateResponse("error.html", {
                "request": request,
                "title": f"HTTP {http_err.response.status_code} contra el MCP",
                "error": str(http_err),
                "trace": traceback.format_exc(),
                "debug_info": [],
            })
        return templates.TemplateResponse("error.html", {
            "request": request,
            "title": "No se pudo conectar al MCP",
            "error": str(e),
            "trace": traceback.format_exc(),
            "debug_info": [],
        })

    def _norm(s: str) -> str:
        return (s or "").strip().casefold()

    def _match_resumen(nombre: str) -> tuple[dict, bool]:
        n = _norm(nombre)
        mk = next(((c, p) for (c, p) in por_cliente.keys() if _norm(c) == n), None)
        return (por_cliente.get(mk, {}) if mk else {}), (mk is not None)

    # Indice de evolucion mensual por cliente normalizado y secuencia de meses
    # (continua, incluyendo meses sin ventas) para el eje X de los graficos.
    evol_idx: dict[str, list[tuple[str, str, float]]] = {}
    for er in evol_rows:
        c = er.get("cliente")
        if c:
            evol_idx.setdefault(_norm(c), []).append(
                (er.get("ym") or "", er.get("canal") or "Sin canal", float(er.get("total") or 0))
            )
    def _evolucion_grupo(miembros: list[str], desde: date) -> dict:
        # Eje X continuo (incluye meses sin ventas) desde el mes elegido al corte.
        seq = _month_seq(desde, fecha_corte_d)
        yms = [d.strftime("%Y-%m") for d in seq]
        labels = [_mes_corto(d) for d in seq]
        mes_total = {ym: 0.0 for ym in yms}
        canal_mes: dict[str, dict[str, float]] = {}
        for miembro in miembros:
            for (ym, canal, total) in evol_idx.get(_norm(miembro), []):
                if ym not in mes_total:
                    continue
                mes_total[ym] += total
                d = canal_mes.setdefault(canal, {})
                d[ym] = d.get(ym, 0.0) + total
        # Top 5 canales por venta total; el resto se agrupa en "Otros".
        rank = sorted(canal_mes.items(), key=lambda kv: sum(kv[1].values()), reverse=True)
        series = {name: [vals.get(ym, 0.0) for ym in yms] for name, vals in rank[:5]}
        if rank[5:]:
            series["Otros"] = [sum(vals.get(ym, 0.0) for _, vals in rank[5:]) for ym in yms]
        total_serie = [mes_total[ym] for ym in yms]
        return {
            "labels": labels,
            "total": total_serie,
            "series": series,
            "has_data": sum(total_serie) > 0,
        }

    # Agrupar los clientes seleccionados por el campo "Grupo". Clave = grupo
    # normalizado; si esta vacio, el cliente va solo (clave propia). El label
    # visible es el texto del grupo tal cual (o el nombre del cliente si va solo).
    grupos: dict[tuple, dict] = {}
    orden: list[tuple] = []
    for cliente in clientes_sel:
        g = grupos_raw.get(cliente, "")
        key = ("g", _norm(g)) if g else ("c", _norm(cliente))
        if key not in grupos:
            grupos[key] = {"label": g or cliente, "miembros": [], "plan": 0.0, "acciones": []}
            orden.append(key)
        grupos[key]["miembros"].append(cliente)
        grupos[key]["plan"] += planes.get(cliente, 0.0)
        grupos[key]["acciones"].extend(acciones_por_cliente.get(cliente, []))

    for key in orden:
        grp = grupos[key]
        g_mtod = g_ma = 0.0
        canales: list[str] = []
        no_encontrados: list[str] = []
        for miembro in grp["miembros"]:
            resumen, encontrado = _match_resumen(miembro)
            if not encontrado:
                no_encontrados.append(miembro)
            g_mtod += float(resumen.get("mtod") or 0)
            g_ma += float(resumen.get("mes_anterior_mtd") or 0)
            for c in (resumen.get("canal") or "").split(" + "):
                c = c.strip()
                if c and c not in canales:
                    canales.append(c)
        rows.append(calcular_cliente(
            cliente=grp["label"],
            fecha_corte=fecha_corte_d,
            plan_mensual=grp["plan"],
            mtod=g_mtod,
            mes_anterior_mtd=g_ma,
            canal=" + ".join(canales),
            acciones=grp["acciones"],
            evolucion=_evolucion_grupo(
                grp["miembros"],
                min((cliente_desde[m] for m in grp["miembros"]), default=grafico_desde_d),
            ),
        ))
        debug_info.append({
            "cliente": grp["label"],
            "miembros": grp["miembros"],
            "encontrado": not no_encontrados,
            "no_encontrados": no_encontrados,
            "mtod": g_mtod,
            "mes_anterior_mtd": g_ma,
            "canal": " + ".join(canales),
            "plan_mensual": grp["plan"],
        })

    force_debug = os.getenv("DEBUG_MCP", "").strip() in ("1", "true", "yes")
    if force_debug:
        return templates.TemplateResponse("error.html", {
            "request": request,
            "title": "DEBUG_MCP=1 (forzado)",
            "error": None,
            "trace": None,
            "debug_info": [{"sql": sql_str, "raw": raw_str}, *debug_info],
        })

    context = build_report_context(
        rows=rows,
        mes_label=_mes_label(mes),
        fecha_corte=fecha_corte_d,
        kam_name=kam_name,
    )
    # Generamos los SVG de evolucion y los inyectamos en cada fila del contexto.
    # Van como HTML pre-renderizado (no se serializan al cache como SVG gigante
    # si no hay datos) para que la plantilla solo tenga que hacer `| safe`.
    for d in context["rows"]:
        ev = d.get("evolucion") or {}
        if ev.get("has_data"):
            # Con un solo canal el grafico por canal es redundante con el total:
            # mostramos solo el total (mas alto para llenar la lamina).
            multi = len(ev.get("series", {})) > 1
            d["chart_total_svg"] = charts.bar_chart_svg(ev["labels"], ev["total"], height=200 if multi else 300)
            d["chart_canal_svg"] = charts.line_chart_svg(ev["labels"], ev["series"]) if multi else ""
        else:
            d["chart_total_svg"] = ""
            d["chart_canal_svg"] = ""
    report_id = _cache_report(context)
    return templates.TemplateResponse(
        "reporte.html",
        {"request": request, "report_id": report_id, "print_mode": False, **context},
    )


def _render_report_html(request: Request, context: dict, print_mode: bool) -> str:
    return templates.get_template("reporte.html").render(
        request=request,
        report_id=None,
        print_mode=print_mode,
        **context,
    )


@app.get("/reporte/{report_id}.html")
def download_report_html(request: Request, report_id: str):
    entry = _REPORT_CACHE.get(report_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Reporte no encontrado o expiro (TTL 30 min). Regenera desde el formulario.")
    context = entry["context"]
    html = _render_report_html(request, context, print_mode=True)
    fname = f"{_slug_filename('Update Comercial ' + context.get('mes_label', ''))}.html"
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


async def _generate_pdf_async(html: str) -> bytes:
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            return await page.pdf(
                format="A4",
                landscape=True,
                print_background=True,
                margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
            )
        finally:
            await browser.close()


def _render_pdf(html: str) -> bytes:
    """Genera el PDF en un loop dedicado, pensado para correr en un thread aparte.

    En Windows, uvicorn con reload (o workers>1) fuerza WindowsSelectorEventLoopPolicy
    a nivel de proceso (ver uvicorn/loops/asyncio.py), y ese loop no puede lanzar
    subprocesos, por lo que Playwright falla al abrir su driver/Chromium. Acá creamos
    un ProactorEventLoop dedicado (que sí soporta subprocesos) sin tocar la policy
    global del proceso, así no interferimos con el loop principal de uvicorn.
    """
    loop = asyncio.ProactorEventLoop() if sys.platform == "win32" else asyncio.new_event_loop()
    try:
        return loop.run_until_complete(_generate_pdf_async(html))
    finally:
        loop.close()


@app.get("/reporte/{report_id}.pdf")
async def download_report_pdf(request: Request, report_id: str):
    entry = _REPORT_CACHE.get(report_id)
    if not entry:
        raise HTTPException(status_code=404, detail="Reporte no encontrado o expiro (TTL 30 min). Regenera desde el formulario.")
    context = entry["context"]
    html = _render_report_html(request, context, print_mode=True)

    pdf_bytes = await asyncio.to_thread(_render_pdf, html)

    fname = f"{_slug_filename('Update Comercial ' + context.get('mes_label', ''))}.pdf"
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{fname}"'},
    )


@app.get("/demo", response_class=HTMLResponse)
def demo(request: Request):
    """Reproduce el caso Febrero 2026 del HTML de referencia con datos hardcoded."""
    fc = date(2026, 2, 23)
    rows = [
        calcular_cliente("Ballerina", fc, 38_156_746, 14_133_710, 11_873_468, canal="Mercado Libre"),
        calcular_cliente("Concha y Toro", fc, 12_260_479, 2_563_853, 3_388_614, canal="Mercado Libre + Walmart"),
        calcular_cliente("Mercado Carozzi", fc, 60_137_312, 27_040_200, 22_477_599, canal="Mercado Libre + Walmart"),
        calcular_cliente("Carozzi FS", fc, 3_513_621, 723_361, 524_533, canal="Mercado Libre"),
        calcular_cliente("Instance Mall", fc, 3_609_596, 708_664, 1_215_624, canal="Mercado Libre"),
        calcular_cliente("PepsiCo Joy", fc, 464_078, 122_408, 157_386, canal="Mercado Libre"),
    ]
    context = build_report_context(
        rows=rows,
        mes_label="Febrero 2026",
        fecha_corte=fc,
        proxima_revision="11.03.2026",
    )
    report_id = _cache_report(context)
    return templates.TemplateResponse(
        "reporte.html",
        {"request": request, "report_id": report_id, "print_mode": False, **context},
    )


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
    )
