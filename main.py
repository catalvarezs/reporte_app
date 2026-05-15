import base64
import hashlib
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode

import httpx
from dotenv import load_dotenv
from fastapi import FastAPI, Form, HTTPException, Request
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

load_dotenv()

BASE_DIR = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))

OAUTH_AUTHORIZE_URL = "https://backendia.instancelatam.com/authorize"
OAUTH_TOKEN_URL = "https://backendia.instancelatam.com/token"
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
SESSION_SECRET = os.getenv("SESSION_SECRET")

if not SESSION_SECRET:
    raise RuntimeError("SESSION_SECRET no configurada en .env")
if not CLIENT_ID or not CLIENT_SECRET:
    raise RuntimeError("CLIENT_ID y CLIENT_SECRET deben estar en .env")

app = FastAPI(title="Update Comercial")
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    session_cookie="reporte_session",
    same_site="lax",
    https_only=False,
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


def _callback_url(request: Request) -> str:
    return str(request.url_for("callback"))


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse(
        "form.html",
        {"request": request, "authenticated": _is_authenticated(request)},
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
def generar(
    request: Request,
    clientes: str = Form(...),
    pais: str = Form("Chile"),
    mes: str = Form(...),
    fecha_corte: str = Form(...),
):
    if not _is_authenticated(request):
        return RedirectResponse("/login", status_code=302)

    fecha_corte_d = datetime.strptime(fecha_corte, "%Y-%m-%d").date()
    lista_clientes = [c.strip() for c in clientes.split(",") if c.strip()]

    rows: list[ClienteRow] = [
        _build_row(cliente, fecha_corte_d, pais) for cliente in lista_clientes
    ]

    context = build_report_context(
        rows=rows,
        mes_label=_mes_label(mes),
        fecha_corte=fecha_corte_d,
    )
    return templates.TemplateResponse("reporte.html", {"request": request, **context})


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
    return templates.TemplateResponse("reporte.html", {"request": request, **context})


@app.get("/reporte.pdf")
def reporte_pdf():
    return Response(
        content="PDF aun no implementado.",
        media_type="text/plain",
        status_code=501,
    )


def _empty_row(cliente: str, fecha_corte: date) -> ClienteRow:
    return calcular_cliente(
        cliente=cliente, fecha_corte=fecha_corte,
        plan_mensual=0, mtod=0, mes_anterior_mtd=0,
    )


def _build_row(cliente: str, fecha_corte: date, pais: str) -> ClienteRow:
    if not os.getenv("INSTANCE_DB_URL"):
        return _empty_row(cliente, fecha_corte)

    try:
        from connectors.instance import fetch_panel_comercial, fetch_consenso
        mes_iso = fecha_corte.strftime("%Y-%m")
        panel = fetch_panel_comercial(cliente, pais=pais) or {}
        consenso = fetch_consenso(cliente, mes_iso, pais=pais) or {}
        return calcular_cliente(
            cliente=cliente,
            fecha_corte=fecha_corte,
            plan_mensual=float(consenso.get("plan_mensual") or 0),
            mtod=float(panel.get("mtod") or 0),
            mes_anterior_mtd=float(panel.get("ventas_lm") or 0),
        )
    except Exception as e:
        print(f"[warn] connector fallo para {cliente}: {e}")
        return _empty_row(cliente, fecha_corte)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
    )
