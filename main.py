import os
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

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

app = FastAPI(title="Update Comercial")

_MESES_ES = {
    1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril",
    5: "Mayo", 6: "Junio", 7: "Julio", 8: "Agosto",
    9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre",
}


def _mes_label(mes_iso: str) -> str:
    y, m = mes_iso.split("-")
    return f"{_MESES_ES[int(m)]} {y}"


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("form.html", {"request": request})


@app.post("/generar", response_class=HTMLResponse)
def generar(
    request: Request,
    clientes: str = Form(...),
    pais: str = Form("Chile"),
    mes: str = Form(...),
    fecha_corte: str = Form(...),
):
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
