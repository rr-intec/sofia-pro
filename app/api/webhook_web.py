"""Endpoints del canal Web Chat.

- `GET /chat`            sirve la UI mínima (chat.html).
- `POST /webhook/web`    recibe un mensaje del usuario y devuelve la respuesta de Sofía.
- `GET /chat/history/{session_id}` devuelve los últimos N mensajes de la sesión.

NOTA: para MVP usamos request/response simple. SSE (streaming token-a-token) se
puede agregar más adelante; con Haiku 4.5 + caching, la latencia de un turno típico
(~3-5s) es suficiente sin streaming.
"""

from __future__ import annotations

import logging
import uuid
from pathlib import Path

from fastapi import APIRouter, Cookie, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from app.config import get_settings
from app.core.sofia_engine import procesar_turno_sofia
from app.core.repository import get_repository
from app.core.state import Canal

log = logging.getLogger(__name__)

router = APIRouter(tags=["web-chat"])

WEB_TEMPLATES = Path(__file__).resolve().parent.parent.parent / "web" / "templates"


class WebChatRequest(BaseModel):
    content: str = Field(min_length=1, max_length=4000)


class WebChatResponse(BaseModel):
    session_id: str
    response: str
    turn_number: int
    fase_journey: str
    intent: str | None = None
    tokens_input: int
    tokens_output: int
    tokens_cached: int
    cost_usd: float
    latency_ms: int


@router.get("/chat", response_class=HTMLResponse)
async def chat_page(
    sofia_web_session: str | None = Cookie(default=None),
) -> HTMLResponse:
    """Sirve la UI del chat. Asegura cookie de sesión.

    Bug fix crítico: el `response.set_cookie(...)` sobre un parámetro
    `Response` NO se transfiere a un `HTMLResponse(...)` retornado distinto —
    el header `Set-Cookie` nunca llegaba al cliente. Cada POST llegaba sin
    cookie y el endpoint generaba un UUID nuevo por request, dejando cada
    turno como t=0 sin contexto. Fix: setear la cookie sobre el `HTMLResponse`
    que efectivamente se retorna.
    """
    settings = get_settings()
    is_new_session = not sofia_web_session
    if is_new_session:
        sofia_web_session = str(uuid.uuid4())

    html_path = WEB_TEMPLATES / "chat.html"
    if not html_path.exists():
        raise HTTPException(status_code=500, detail="chat.html no encontrado")

    html = html_path.read_text(encoding="utf-8")
    html = html.replace("{{TITLE}}", settings.web_chat_title)
    html = html.replace("{{SESSION_ID}}", f"web:{sofia_web_session}")

    html_response = HTMLResponse(content=html)
    if is_new_session:
        # secure=True solo en prod (HTTPS). httponly y SameSite=Lax protegen
        # contra CSRF/XSS sin romper el flujo same-origin del JS frontend.
        html_response.set_cookie(
            key=settings.web_session_cookie,
            value=sofia_web_session,
            httponly=True,
            secure=settings.is_production,
            samesite="lax",
            max_age=60 * 60 * 24 * 30,  # 30 días
        )
    return html_response


@router.post("/webhook/web", response_model=WebChatResponse)
async def webhook_web(
    body: WebChatRequest,
    request: Request,
    sofia_web_session: str | None = Cookie(default=None),
) -> WebChatResponse:
    """Procesa un mensaje del usuario y devuelve la respuesta de Sofía."""
    if not sofia_web_session:
        # Cliente envió sin cookie — generar fallback de UUID
        sofia_web_session = str(uuid.uuid4())
    session_id = f"web:{sofia_web_session}"

    result = await procesar_turno_sofia(
        mensaje=body.content,
        session_id=session_id,
        canal=Canal.WEB,
        tester=False,
    )

    return WebChatResponse(
        session_id=result.session_id,
        response=result.response,
        turn_number=result.turn_number,
        # Sofía Pro es model-driven: no hay máquina de fases/intent del code-driven.
        # Mantenemos el shape del response con valores fijos para no romper la UI.
        fase_journey="agente",
        intent=None,
        tokens_input=result.tokens_input,
        tokens_output=result.tokens_output,
        tokens_cached=result.tokens_cached,
        cost_usd=float(result.cost_usd),
        latency_ms=result.latency_ms,
    )


@router.get("/chat/history/{session_id:path}")
async def chat_history(session_id: str, limit: int = 50) -> dict:
    """Devuelve el historial reciente para hidratar la UI al recargar."""
    repo = get_repository()
    rows = await repo.list_recent_messages(session_id, limit=limit)
    return {"session_id": session_id, "messages": rows}


# ============================================================
# Página pública de disponibilidad (SOLO LECTURA)
# El papá VE los horarios libres; agenda por WhatsApp (no hay auto-registro,
# para no sacarlo del chat y que no se enfríe). Reunión Fabiola 2026-08-07.
# ============================================================

_DIAS_SEMANA = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_MESES = [
    "enero", "febrero", "marzo", "abril", "mayo", "junio",
    "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre",
]


def _fecha_larga_es(dt) -> str:
    return f"{_DIAS_SEMANA[dt.weekday()]} {dt.day} de {_MESES[dt.month - 1]}"


def _hora_ampm(dt) -> str:
    h, m = dt.hour, dt.minute
    ampm = "a.m." if h < 12 else "p.m."
    return f"{h % 12 or 12}:{m:02d} {ampm}"


@router.get("/agenda", response_class=HTMLResponse)
async def agenda_disponibilidad() -> HTMLResponse:
    """Disponibilidad pública de citas de informes (solo lectura). Misma lógica de
    slots que usa Sofía (fuente única de verdad); se agenda por WhatsApp."""
    from app.tools.availability_checker import evaluar_dia, proximos_dias_habiles

    bloques: list[str] = []
    try:
        dias = await proximos_dias_habiles(cantidad=10)
        for d in dias:
            res = await evaluar_dia(d)
            if not res.available or not res.alternativas:
                continue
            chips = "".join(f'<span class="slot">{_hora_ampm(h)}</span>' for h in res.alternativas)
            bloques.append(
                f'<div class="dia"><div class="fecha">{_fecha_larga_es(d)}</div>'
                f'<div class="slots">{chips}</div></div>'
            )
    except Exception as exc:  # noqa: BLE001
        log.warning("agenda: cálculo de disponibilidad falló", extra={"error": str(exc)})

    cuerpo = (
        "\n".join(bloques)
        if bloques
        else '<p class="vacio">Por ahora no hay horarios publicados. Escríbenos por '
        "WhatsApp y con gusto te agendamos 😊</p>"
    )

    html = (
        '<!doctype html><html lang="es"><head>'
        '<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">'
        "<title>Citas de informes · Maple Collège</title><style>"
        ":root{--maple:#ec2b3b;--ink:#1c1c1e;--muted:#6b7280;--line:#e7e7ea;--bg:#f7f7f8;}"
        "*{box-sizing:border-box;}body{margin:0;font-family:-apple-system,BlinkMacSystemFont,"
        "'Segoe UI',Roboto,sans-serif;background:var(--bg);color:var(--ink);}"
        ".wrap{max-width:640px;margin:0 auto;padding:0 16px 48px;}"
        "header{background:var(--maple);color:#fff;padding:28px 16px;text-align:center;}"
        "header h1{margin:0;font-size:20px;}header p{margin:6px 0 0;opacity:.92;font-size:14px;}"
        ".card{background:#fff;border:1px solid var(--line);border-radius:14px;padding:16px;margin-top:16px;}"
        ".intro{font-size:14px;color:var(--muted);margin:0;}"
        ".dia{padding:14px 0;border-bottom:1px solid var(--line);}.dia:last-child{border-bottom:0;}"
        ".fecha{font-weight:600;text-transform:capitalize;margin-bottom:8px;}"
        ".slots{display:flex;flex-wrap:wrap;gap:8px;}"
        ".slot{background:#fff;border:1px solid var(--maple);color:var(--maple);border-radius:999px;"
        "padding:6px 12px;font-size:14px;font-variant-numeric:tabular-nums;}"
        ".vacio{color:var(--muted);font-size:14px;margin:0;}"
        ".cta{text-align:center;margin-top:20px;font-size:14px;color:var(--muted);}"
        ".foot{text-align:center;color:var(--muted);font-size:12px;margin-top:24px;}"
        "</style></head><body>"
        "<header><h1>Maple Collège · Citas de informes</h1>"
        "<p>Horarios disponibles para conocer el colegio</p></header>"
        '<div class="wrap"><div class="card"><p class="intro">Estos son los horarios '
        "disponibles para tu cita de informes. Para apartar el tuyo, <strong>respóndenos por "
        "WhatsApp</strong> con el día y la hora que más te acomode — nosotros lo confirmamos "
        '😊</p></div><div class="card">' + cuerpo + "</div>"
        '<p class="cta">¿Ninguno te acomoda? Escríbenos por WhatsApp y buscamos una opción para ti.</p>'
        '<p class="foot">Maple Collège · Best Education Active and Relevant</p></div></body></html>'
    )
    return HTMLResponse(content=html)
