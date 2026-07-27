"""Seguimientos automáticos ("3 toques") para reconectar leads que se enfriaron sin
agendar. Pautas y mensajes aprobados por Gaby (2026-07-16).

Secuencia:
  Toque 1 — Sofía, recordatorio suave.
  Toque 2 — Sofía, valor + cierre amable.
  Toque 3 — Lili, NOTA DE VOZ personal. La automatización NUNCA lo envía: solo marca
            el lead para que Lili haga el toque humano.

Dos cadencias, misma secuencia:
  - `normal`        (lead recién enfriado): 48h → +48h → +5 días.
  - `recuperacion`  (backlog olvidado hace días): comprimida → ya → +24h → +24h.

Reglas duras:
  - SOLO a chats donde Sofía es la dueña (bot_activo=True). Jamás a un chat de Lily.
  - Se detiene si el prospecto responde, si agenda, o si Lily toma el chat.
  - Goteo: tope de envíos por tick para no estallar (el scheduler corre cada 5 min).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import asyncpg

from app.config import get_settings

log = logging.getLogger(__name__)

# --- Mensajes EXACTOS aprobados por Gaby (no cambiar sin su visto bueno) ---
TOQUE1 = (
    "Hola{n} 👋 Soy Sofía, de Maple Collège. Me quedé pensando en lo que platicamos y "
    "la verdad me encantaría que te animaras a conocernos 😊 ¿Te gustaría que agendemos "
    "una visita para que conozcas la escuela y resuelvas cualquier duda en persona? Con "
    "gusto te aparto un espacio."
)
TOQUE2 = (
    "Hola{n} 🍁 Sé que elegir la escuela de un hijo no es cualquier decisión — por eso "
    "lo mejor es que vengas, la conozcas y sientas el ambiente. La visita no te "
    "compromete a nada, es solo para que tengas toda la claridad. ¿Buscamos un día que "
    "te acomode?"
)
# Variante del Toque 1 para chats que atendía LILY (Sofía no "platicó" con ellos, así que
# no puede decir "lo que platicamos"). Más neutral, sin dar por hecho una charla previa.
TOQUE1_LILY = (
    "Hola{n} 👋 Soy Sofía, del equipo de admisiones de Maple Collège. Vi que nos "
    "escribiste hace unos días y me encantaría ayudarte a que conozcas la escuela 😊 "
    "¿Te gustaría que agendemos una visita para que la conozcas y resuelvas cualquier "
    "duda en persona? Con gusto te aparto un espacio."
)
# Guía para Lili (se guarda como nota en el lead; ella manda su nota de voz).
TOQUE3_GUIA_LILI = (
    "Hola{n}, soy Lili de Maple 💛 Con mucho gusto te acompaño en el proceso. Si tienes "
    "cualquier duda o quieres que te aparte un espacio para tu visita, aquí estoy para "
    "ayudarte."
)

# Horas entre toques por cadencia. Toque 1: horas de silencio necesarias para arrancar.
CADENCIAS = {
    "normal": {1: 48, 2: 48, 3: 120},
    "recuperacion": {1: 0, 2: 24, 3: 24},
}
# Si al momento del primer toque el lead lleva MÁS de esto frío, va por vía rápida.
UMBRAL_RECUPERACION_H = 96
# Silencio mínimo antes del primer toque en un chat de SOFÍA (bot activo).
FRIO_MINIMO_H = 48
# Silencio mínimo MUCHO más estricto para un chat que atendía LILY (bot apagado): no
# basta con que esté callado, tiene que estar CLARAMENTE abandonado. Una pausa natural
# en un proceso que Lily lleva (esperando docs, que el papá decida) NO es un lead frío.
FRIO_MINIMO_LILY_H = 120  # 5 días

STAGES_SEGUIBLES = ("contacto_inicial", "pendiente_agendar")


@dataclass
class Accion:
    session_id: str
    nombre: str
    toque: int
    cadencia: str
    horas_frio: float
    texto: str  # mensaje a enviar (toque 1/2) o guía para Lili (toque 3)
    es_lili: bool  # True = toque 3, NO se envía, solo se marca
    de_lily: bool = False  # el chat lo atendía Lily (bot apagado) → mensaje neutral


def _primer_nombre(parent_name: str | None) -> str:
    """' Ana' para 'Ana López'; '' si es genérico/basura/ausente (saludo sin nombre).
    Limpia emojis y símbolos ('Adriana☕' → ' Adriana'); descarta tokens sin ≥2 letras."""
    import re

    n = (parent_name or "").strip()
    if not n or n.lower() in ("prospecto", "prospecto/a", "sin nombre"):
        return ""
    token = n.split(" ")[0]
    limpio = re.sub(r"[^A-Za-zÁÉÍÓÚÑáéíóúñ]", "", token)
    return " " + limpio if len(limpio) >= 2 else ""


def _texto_toque(toque: int, parent_name: str | None, de_lily: bool = False) -> str:
    n = _primer_nombre(parent_name)
    if toque == 1 and de_lily:
        return TOQUE1_LILY.format(n=n)
    plantilla = {1: TOQUE1, 2: TOQUE2, 3: TOQUE3_GUIA_LILI}[toque]
    return plantilla.format(n=n)


async def planear(conn: asyncpg.Connection, ahora: datetime) -> list[Accion]:
    """Devuelve las acciones DEBIDAS ahora (sin enviar), ordenadas por urgencia
    (lead más frío primero). Aplica todas las reglas de calificación y parada."""
    rows = await conn.fetch(
        """
        with lastmsg as (
          select distinct on (session_id) session_id, role, created_at
          from sofia_messages order by session_id, created_at desc)
        select l.parent_name, l.stage, l.conversation_session_id sid,
               c.bot_activo, lm.role ultimo_rol, lm.created_at ultima_actividad
        from leads l
        join sofia_conversations c on c.session_id = l.conversation_session_id
        left join lastmsg lm on lm.session_id = l.conversation_session_id
        where l.stage::text = any($1::text[])
          and l.conversation_session_id is not null
          and not exists (
            select 1 from appointments a where a.lead_id = l.id)
        """,
        list(STAGES_SEGUIBLES),
    )

    # Estado de toques ya enviados por sesión.
    enviados = await conn.fetch("select session_id, toque, cadencia, enviado_at from lead_seguimientos")
    por_sesion: dict[str, dict[int, dict]] = {}
    for e in enviados:
        por_sesion.setdefault(e["session_id"], {})[e["toque"]] = e

    acciones: list[Accion] = []
    for r in rows:
        sid = r["sid"]
        ult = r["ultima_actividad"]
        if ult is None:
            continue
        if ult.tzinfo is None:
            ult = ult.replace(tzinfo=timezone.utc)
        horas_frio = (ahora - ult).total_seconds() / 3600.0

        hechos = por_sesion.get(sid, {})
        # Si el prospecto respondió DESPUÉS del último toque → re-enganchó, se detiene.
        if hechos:
            ultimo_toque = max(hechos)
            t_env = hechos[ultimo_toque]["enviado_at"]
            if t_env.tzinfo is None:
                t_env = t_env.replace(tzinfo=timezone.utc)
            if r["ultimo_rol"] == "user" and ult > t_env:
                continue  # volvió a escribir → Sofía lo atiende, no seguimiento

        siguiente = (max(hechos) + 1) if hechos else 1
        if siguiente > 3:
            continue  # ya completó la secuencia

        # Caso "prospecto esperando respuesta" antes de cualquier toque → no es
        # seguimiento (Sofía debió contestar); lo dejamos fuera.
        if siguiente == 1 and r["ultimo_rol"] == "user":
            continue

        # Cadencia: se fija en el toque 1 y se conserva.
        if siguiente == 1:
            cad = "recuperacion" if horas_frio > UMBRAL_RECUPERACION_H else "normal"
        else:
            cad = hechos[1]["cadencia"] if 1 in hechos else "normal"

        # ¿Está DEBIDO el siguiente toque? El silencio mínimo es MUCHO mayor si el chat
        # lo atendía Lily (bot apagado): así solo entramos cuando está claramente
        # abandonado, no en una pausa de un proceso que ella lleva.
        if siguiente == 1:
            frio_min = FRIO_MINIMO_H if r["bot_activo"] else FRIO_MINIMO_LILY_H
            if horas_frio < frio_min:
                continue
        else:
            prev = hechos[siguiente - 1]
            t_env = prev["enviado_at"]
            if t_env.tzinfo is None:
                t_env = t_env.replace(tzinfo=timezone.utc)
            horas_desde_prev = (ahora - t_env).total_seconds() / 3600.0
            if horas_desde_prev < CADENCIAS[cad][siguiente]:
                continue

        de_lily = not r["bot_activo"]
        acciones.append(
            Accion(
                session_id=sid,
                nombre=(r["parent_name"] or "").strip(),
                toque=siguiente,
                cadencia=cad,
                horas_frio=horas_frio,
                texto=_texto_toque(siguiente, r["parent_name"], de_lily=de_lily),
                es_lili=(siguiente == 3),
                de_lily=de_lily,
            )
        )

    acciones.sort(key=lambda a: a.horas_frio, reverse=True)
    return acciones


async def _enviar_wa(evo, session_id: str, texto: str) -> bool:
    """Envía por WhatsApp resolviendo @lid → número real (remoteJidAlt) si hace falta."""
    jid = session_id.split(":", 1)[1] if ":" in session_id else session_id
    if jid.endswith("@lid"):
        numero = None
        for m in await evo.find_messages_by_jid(jid):
            alt = (m.get("key") or {}).get("remoteJidAlt") or ""
            if alt.endswith("@s.whatsapp.net"):
                numero = "".join(c for c in alt.split("@")[0] if c.isdigit())
                break
        if not numero:
            return False
        r = await evo.http.post(f"/message/sendText/{evo.instance}", json={"number": numero, "text": texto})
        return r.status_code < 300
    return bool(await evo.send_text(session_id, texto))


async def _marcar_lili(conn: asyncpg.Connection, sid: str, guia: str) -> None:
    """Toque 3: NO se envía. Se anota en el lead para que Lili haga su nota de voz."""
    nota = f"[SEGUIMIENTO] Requiere toque personal de Lili (nota de voz). Guía sugerida: {guia}"
    await conn.execute(
        "update leads set notes = coalesce(notes || E'\\n', '') || $2 "
        "where conversation_session_id = $1 and (notes is null or notes not like '%toque personal de Lili%')",
        sid,
        nota,
    )


async def ejecutar(enviar: bool, cap: int) -> list[dict]:
    """Corre una pasada de seguimientos. `enviar=False` = dry-run (no manda nada).
    `cap` limita cuántos toques 1/2 se envían en esta pasada (goteo). El toque 3 solo
    marca al lead, no cuenta contra el cap. Devuelve el reporte de lo hecho/planeado."""
    from app.adapters.evolution_client import get_evolution

    from zoneinfo import ZoneInfo

    settings = get_settings()
    # GUARDA DE HORARIO: solo se envía dentro de la ventana local de México. Fuera de
    # ella no se manda NADA (ni se marca a Lili) — se reintenta en el siguiente tick.
    hora_mx = datetime.now(ZoneInfo("America/Mexico_City")).hour
    if enviar and not (settings.seguimientos_hora_inicio <= hora_mx < settings.seguimientos_hora_fin):
        log.info(
            "seguimientos: fuera de horario, no se envía",
            extra={"hora_mx": hora_mx, "ventana": [settings.seguimientos_hora_inicio, settings.seguimientos_hora_fin]},
        )
        return [{"accion": "FUERA_DE_HORARIO", "hora_mx": hora_mx}]

    conn = await asyncpg.connect(settings.supabase_db_url)
    reporte: list[dict] = []
    try:
        ahora = datetime.now(timezone.utc)
        # Tope diario: cuántos toques (1/2) ya salieron HOY (fecha de México).
        if enviar and settings.seguimientos_cap_dia > 0:
            hoy = await conn.fetchval(
                "select count(*) from lead_seguimientos where toque in (1,2) "
                "and (enviado_at at time zone 'America/Mexico_City')::date "
                "= (now() at time zone 'America/Mexico_City')::date"
            )
            restante_dia = max(0, settings.seguimientos_cap_dia - int(hoy or 0))
            cap = min(cap, restante_dia)
            if cap == 0:
                log.info("seguimientos: tope diario alcanzado", extra={"hoy": hoy})
                return [{"accion": "TOPE_DIARIO_ALCANZADO", "hoy": int(hoy or 0)}]
        acciones = await planear(conn, ahora)
        evo = get_evolution()
        enviados = 0
        for a in acciones:
            item = {
                "sid": a.session_id,
                "nombre": a.nombre or "(sin nombre)",
                "toque": a.toque,
                "cadencia": a.cadencia,
                "dias_frio": round(a.horas_frio / 24, 1),
                "accion": "MARCAR_LILI" if a.es_lili else "ENVIAR",
            }
            if a.es_lili:
                if enviar:
                    await _marcar_lili(conn, a.session_id, a.texto)
                    await conn.execute(
                        "insert into lead_seguimientos(session_id,toque,cadencia) values($1,3,$2) "
                        "on conflict do nothing",
                        a.session_id, a.cadencia,
                    )
                reporte.append(item)
                continue
            if enviados >= cap:
                item["accion"] = "APLAZADO_POR_TOPE"
                reporte.append(item)
                continue
            if enviar:
                ok = await _enviar_wa(evo, a.session_id, a.texto)
                if not ok:
                    item["accion"] = "FALLO_ENVIO"
                    reporte.append(item)
                    continue
                await conn.execute(
                    "insert into lead_seguimientos(session_id,toque,cadencia) values($1,$2,$3) "
                    "on conflict do nothing",
                    a.session_id, a.toque, a.cadencia,
                )
                # Si el chat estaba en manos de Lily (bot apagado) y ya está FRÍO, Sofía
                # retoma la re-conexión para quitarle carga a Lily. Si Lily lo quiere de
                # vuelta, su próximo mensaje lo reclama solo (auto-handoff). No aplica al
                # Toque 3 (ese es de Lili).
                await conn.execute(
                    "update sofia_conversations set bot_activo=true, atendido_por='bot' "
                    "where session_id=$1 and bot_activo=false",
                    a.session_id,
                )
            enviados += 1
            reporte.append(item)
    finally:
        await conn.close()
    return reporte
