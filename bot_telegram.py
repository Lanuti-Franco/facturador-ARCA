"""
Bot de Telegram del facturador — python-telegram-bot v22 (async).

Esta es SOLO la capa de conversacion. La logica de negocio (ARCA, Supabase,
PDF) vive en facturador.py y no sabe nada de Telegram.

Comandos:
    /facturar                      -> mini menu: Consumidor Final / Con CUIT o DNI
    /facturar 15000                -> atajo: preview directo (consumidor final)
    /facturar 15000 20-12345678-6  -> atajo: pide condicion IVA y preview
    /facturar 15000 26/06          -> atajo con fecha retroactiva
    /facturar 15000 01/06-30/06    -> atajo con periodo facturado
    /facturar 15000 "diseño web junio" -> con detalle propio en el PDF
    (doc, fecha, periodo y "detalle" se combinan en cualquier orden)
    + alerta si un monto a CF anonimo alcanza el umbral de identificacion
      (UMBRAL_CF en el .env, RG 5700/2025)
    /lote 15000 20000 12500 [fecha] [periodo]
        -> varias facturas de un saque, todas a consumidor final sin datos
    /nc 5 [monto]   -> Nota de Credito C asociada a la Factura 5 (total o parcial)
    /resumen [6 | 06/2026 | 2026 | 01/06-30/06]
        -> lista de comprobantes del periodo con total (NC restan)
    /mail 5 [email] -> manda el comprobante por email al cliente (recuerda
                       el email por receptor en la tabla `clientes`)
    /csv [periodo] -> export CSV para el contador (NC en negativo)
    /tope          -> facturado 12 meses vs tope de la categoria (MONOTRIBUTO_TOPE)
    /pdf 5 | /pdf nc 2 | /ultima | /id
    + resumen automatico el 1° de cada mes a las 9:00 (job-queue de PTB)
    + alerta de tope en cada emision al superar el 80% de la categoria

Variables de entorno propias de esta capa (el resto las lee facturador.py):
    TELEGRAM_TOKEN  -> token de BotFather
    MI_CHAT_ID      -> tu chat_id numerico (allowlist, obligatorio)
"""

import os
import logging
from datetime import date, time as dtime, timedelta
from decimal import InvalidOperation

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

import facturador as fac
from facturador import fmt_ars, fmt_doc, hoy_ar, descripcion_receptor

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN")
MI_CHAT_ID = int(os.environ["MI_CHAT_ID"]) if os.environ.get("MI_CHAT_ID") else None

# Estados de la conversacion
(ELIGIENDO_RECEPTOR, PIDIENDO_CUIT, ELIGIENDO_COND_IVA,
 PIDIENDO_MONTO, CONFIRMANDO, PIDIENDO_FECHA, PIDIENDO_PERIODO,
 CONFIRMANDO_LOTE, CONFIRMANDO_NC) = range(9)

LOTE_MAX = 10                  # tope de facturas por /lote, contra el fat-finger

MENSAJE_USO_PERIODO = (
    "No entendí el período. Ejemplos:\n"
    "sin nada → mes actual\n6 → junio\n"
    "06/2026 · 2026 · 01/06-30/06"
)


# ---------------------------------------------------------------------------
# Allowlist: solo tu chat_id puede usar el bot
# ---------------------------------------------------------------------------
def solo_autorizado(func):
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if MI_CHAT_ID is not None and update.effective_chat.id != MI_CHAT_ID:
            logger.warning("Chat no autorizado: %s", update.effective_chat.id)
            return ConversationHandler.END
        return await func(update, context)
    return wrapper


# ---------------------------------------------------------------------------
# Helpers de UI
# ---------------------------------------------------------------------------
def set_receptor_cf(ud: dict) -> None:
    ud["doc_tipo"] = fac.DOC_TIPO_CF
    ud["doc_nro"] = fac.DOC_NRO_CF
    ud["cond_iva"] = fac.COND_IVA_CF


def botones_cond_iva() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(desc, callback_data=f"iva_{codigo}")]
        for codigo, desc in fac.CONDICIONES_IVA.items()
    ])


async def mostrar_preview(mensaje, ud: dict) -> int:
    """Preview con Confirmar/Cancelar. `mensaje` es update.message o query.message."""
    fecha = ud.setdefault("fecha", hoy_ar())
    etiqueta_fecha = fecha.strftime("%d/%m/%Y") + (" (hoy)" if fecha == hoy_ar() else "")
    if ud.get("serv_desde"):
        etiqueta_periodo = (f"{ud['serv_desde'].strftime('%d/%m/%Y')} al "
                            f"{ud['serv_hasta'].strftime('%d/%m/%Y')}")
    else:
        etiqueta_periodo = "= fecha de emisión"
    botones = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Confirmar", callback_data="confirmar"),
            InlineKeyboardButton("❌ Cancelar", callback_data="cancelar"),
        ],
        [
            InlineKeyboardButton("📅 Fecha", callback_data="fecha"),
            InlineKeyboardButton("📆 Período", callback_data="periodo"),
        ],
    ])
    linea_detalle = f"Detalle: {ud['descripcion']}\n" if ud.get("descripcion") else ""
    umbral = fac.aviso_umbral(ud["monto"], ud["doc_tipo"])
    linea_umbral = f"{umbral}\n\n" if umbral else ""
    await mensaje.reply_text(
        f"Vas a emitir:\n\n"
        f"Factura C — Servicios\n"
        f"Receptor: {descripcion_receptor(ud)}\n"
        f"Total: ${fmt_ars(ud['monto'])}\n"
        f"{linea_detalle}"
        f"Fecha: {etiqueta_fecha}\n"
        f"Período: {etiqueta_periodo}\n\n"
        f"{linea_umbral}"
        f"{'⚠️ MODO TEST (no es real)' if not fac.PRODUCTION else '🔴 PRODUCCIÓN (real)'}\n\n"
        f"¿Confirmás?",
        reply_markup=botones,
    )
    return CONFIRMANDO


# ---------------------------------------------------------------------------
# Handlers del flujo /facturar
# ---------------------------------------------------------------------------
@solo_autorizado
async def facturar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Entrada. Sin args: mini menu. Con args: atajos de un solo mensaje."""
    context.user_data.clear()
    descripcion, args = fac.extraer_descripcion(context.args or [])
    if descripcion:
        context.user_data["descripcion"] = descripcion
    args = fac.normalizar_args(args)

    # Atajo: /facturar <monto> [cuit/dni] [fecha] [periodo]
    if args:
        try:
            context.user_data["monto"] = fac.parsear_monto(args[0])
        except (InvalidOperation, ArithmeticError, ValueError):
            await update.message.reply_text(
                "No entendí el monto. Ejemplos:\n"
                "/facturar 15000\n/facturar 15.000,50\n/facturar 15000 20-12345678-6"
            )
            return ConversationHandler.END

        # Los args extra pueden ser CUIT/DNI, fecha y/o periodo, en cualquier orden.
        for extra in args[1:]:
            doc = fac.parsear_doc(extra)
            if doc is not None:
                context.user_data["doc_tipo"], context.user_data["doc_nro"] = doc
                continue
            periodo = fac.parsear_periodo(extra)
            if periodo is not None:
                context.user_data["serv_desde"], context.user_data["serv_hasta"] = periodo
                continue
            fecha = fac.parsear_fecha(extra)
            if fecha is not None:
                error = fac.validar_fecha(fecha)
                if error:
                    await update.message.reply_text(error)
                    return ConversationHandler.END
                context.user_data["fecha"] = fecha
                continue
            await update.message.reply_text(
                f"No entendí «{extra}»: no es un CUIT (11 dígitos), un DNI (7-8), "
                f"una fecha (26/06) ni un período (01/06-30/06).\n"
                f"Ej: /facturar 15000 20-12345678-6 26/06 01/06-30/06"
            )
            return ConversationHandler.END

        if context.user_data.get("doc_tipo") is not None:
            await update.message.reply_text(
                f"{fmt_doc(context.user_data['doc_tipo'], context.user_data['doc_nro'])}. "
                f"¿Condición de IVA del receptor?",
                reply_markup=botones_cond_iva(),
            )
            return ELIGIENDO_COND_IVA

        set_receptor_cf(context.user_data)
        return await mostrar_preview(update.message, context.user_data)

    # Sin args: mini menu
    botones = InlineKeyboardMarkup([
        [InlineKeyboardButton("👤 Consumidor final", callback_data="r_cf")],
        [InlineKeyboardButton("🧾 Con CUIT o DNI", callback_data="r_cuit")],
        [InlineKeyboardButton("❌ Cancelar", callback_data="r_cancelar")],
    ])
    await update.message.reply_text(
        "Nueva Factura C. ¿A quién?\n\n"
        "Atajos:\n"
        "/facturar 15000 → consumidor final\n"
        "/facturar 15000 20-12345678-6 → con CUIT/DNI\n"
        "/facturar 15000 26/06 → con fecha retroactiva\n"
        "/facturar 15000 01/06-30/06 → con período facturado",
        reply_markup=botones,
    )
    return ELIGIENDO_RECEPTOR


async def elegir_receptor(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "r_cancelar":
        await query.edit_message_text("Cancelado. No se emitió nada.")
        return ConversationHandler.END

    if query.data == "r_cuit":
        botones = [
            [InlineKeyboardButton(
                f"{fmt_doc(r['doc_tipo'], r['doc_nro'])} — "
                f"{fac.CONDICIONES_IVA.get(r['cond_iva'], r['cond_iva'])}",
                callback_data=f"rec_{r['doc_tipo']}_{r['doc_nro']}_{r['cond_iva']}",
            )]
            for r in fac.receptores_recientes()
        ]
        texto = "Mandame el CUIT (11 dígitos) o DNI (7-8) del receptor"
        texto += ", o elegí uno reciente:" if botones else ", con o sin guiones."
        await query.edit_message_text(
            texto,
            reply_markup=InlineKeyboardMarkup(botones) if botones else None,
        )
        return PIDIENDO_CUIT

    # Consumidor final
    set_receptor_cf(context.user_data)
    await query.edit_message_text(
        "Factura C a consumidor final.\n¿Por qué monto total? (ej: 15000 o 15.000,50)"
    )
    return PIDIENDO_MONTO


async def elegir_reciente(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Receptor elegido de los botones de recientes: doc + condicion en un toque."""
    query = update.callback_query
    await query.answer()

    _, doc_tipo, doc_nro, cond_iva = query.data.split("_")
    ud = context.user_data
    ud["doc_tipo"], ud["doc_nro"], ud["cond_iva"] = int(doc_tipo), int(doc_nro), int(cond_iva)

    await query.edit_message_text(f"Receptor: {descripcion_receptor(ud)}")
    if ud.get("monto") is not None:
        return await mostrar_preview(query.message, ud)
    await query.message.reply_text("¿Por qué monto total? (ej: 15000 o 15.000,50)")
    return PIDIENDO_MONTO


async def recibir_cuit(update: Update, context: ContextTypes.DEFAULT_TYPE):
    doc = fac.parsear_doc(update.message.text)
    if doc is None:
        await update.message.reply_text(
            "No es un CUIT válido (11 dígitos, verifico el dígito final) "
            "ni un DNI (7-8 dígitos). Probá de nuevo."
        )
        return PIDIENDO_CUIT

    context.user_data["doc_tipo"], context.user_data["doc_nro"] = doc
    await update.message.reply_text(
        f"{fmt_doc(*doc)}. ¿Condición de IVA del receptor?",
        reply_markup=botones_cond_iva(),
    )
    return ELIGIENDO_COND_IVA


async def elegir_cond_iva(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    context.user_data["cond_iva"] = int(query.data.removeprefix("iva_"))
    await query.edit_message_text(
        f"Receptor: {descripcion_receptor(context.user_data)}"
    )

    # Si el monto ya vino en el atajo, directo al preview.
    if context.user_data.get("monto") is not None:
        return await mostrar_preview(query.message, context.user_data)

    await query.message.reply_text("¿Por qué monto total? (ej: 15000 o 15.000,50)")
    return PIDIENDO_MONTO


async def recibir_monto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        context.user_data["monto"] = fac.parsear_monto(update.message.text)
    except (InvalidOperation, ArithmeticError, ValueError):
        await update.message.reply_text("No entendí el monto. Ej: 15000 o 15.000,50")
        return PIDIENDO_MONTO

    return await mostrar_preview(update.message, context.user_data)


async def pedir_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        f"¿Fecha del comprobante? (dd/mm o dd/mm/aaaa)\n\n"
        f"Podés ir hasta {fac.DIAS_ATRAS_MAX} días para atrás (servicios). "
        f"Ojo: no puede ser anterior a tu último comprobante emitido — "
        f"ARCA exige numeración cronológica."
    )
    return PIDIENDO_FECHA


async def recibir_fecha(update: Update, context: ContextTypes.DEFAULT_TYPE):
    fecha = fac.parsear_fecha(update.message.text)
    if fecha is None:
        await update.message.reply_text("No entendí la fecha. Ej: 26/06 o 26/06/2026")
        return PIDIENDO_FECHA

    error = fac.validar_fecha(fecha)
    if error:
        await update.message.reply_text(error)
        return PIDIENDO_FECHA

    context.user_data["fecha"] = fecha
    return await mostrar_preview(update.message, context.user_data)


async def pedir_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    await query.edit_message_text(
        "¿Período facturado? Mandame las dos fechas.\n"
        "Ej: 01/06 al 30/06 — o «emision» para volver al default."
    )
    return PIDIENDO_PERIODO


async def recibir_periodo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = update.message.text.strip().lower()
    if texto in ("emision", "emisión", "default"):
        context.user_data.pop("serv_desde", None)
        context.user_data.pop("serv_hasta", None)
        return await mostrar_preview(update.message, context.user_data)

    periodo = fac.parsear_periodo(texto)
    if periodo is None:
        await update.message.reply_text(
            "No entendí el período. Mandame dos fechas: ej 01/06 al 30/06"
        )
        return PIDIENDO_PERIODO

    context.user_data["serv_desde"], context.user_data["serv_hasta"] = periodo
    return await mostrar_preview(update.message, context.user_data)


async def confirmar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancelar":
        await query.edit_message_text("Cancelado. No se emitió nada.")
        return ConversationHandler.END

    ud = context.user_data
    if ud.get("monto") is None or ud.get("doc_tipo") is None:
        # Pasa si el bot se reinicio entre el preview y el boton.
        await query.edit_message_text("Se perdieron los datos (¿se reinició el bot?). Arrancá de nuevo con /facturar.")
        return ConversationHandler.END

    await query.edit_message_text("Emitiendo... ⏳")

    # 1) Emitir en AFIP
    try:
        res = fac.emitir_factura_c(ud["monto"], ud["doc_tipo"], ud["doc_nro"],
                                   ud["cond_iva"], ud.get("fecha") or hoy_ar(),
                                   ud.get("serv_desde"), ud.get("serv_hasta"))
    except Exception as e:
        mensaje_error = f"❌ Error al emitir en AFIP:\n{e}"
        if "10016" in str(e):
            mensaje_error += (
                "\n\n💡 Este error suele ser por la fecha: no puede ser anterior "
                "a la de tu último comprobante emitido (numeración cronológica). "
                "Probá con una fecha más reciente o con hoy."
            )
        await query.message.reply_text(mensaje_error)
        return ConversationHandler.END

    # 2) Guardar en Supabase. Si esto falla, la factura YA existe en AFIP:
    #    te avisamos explicito para que no quede desprolijo en silencio.
    try:
        fac.guardar_factura(res, ud["monto"], ud["doc_tipo"], ud["doc_nro"],
                            ud["cond_iva"], ud.get("descripcion"))
        guardado_ok = True
    except Exception as e:
        guardado_ok = False
        logger.error("Factura emitida pero fallo el guardado: %s", e)

    mensaje = (
        f"✅ Factura emitida\n\n"
        f"Receptor: {descripcion_receptor(ud)}\n"
        f"Total: ${fmt_ars(ud['monto'])}\n"
        f"Fecha: {(ud.get('fecha') or hoy_ar()).strftime('%d/%m/%Y')}\n"
        f"Número: {res['numero']}\n"
        f"CAE: {res['CAE']}\n"
        f"Vto CAE: {res['CAEFchVto']}"
    )
    if not guardado_ok:
        mensaje += (
            "\n\n⚠️ OJO: la factura se emitió en AFIP pero NO se pudo guardar "
            "en Supabase. Anotala manualmente. (Revisá el log del bot.)"
        )

    alerta = fac.aviso_tope()
    if alerta:
        mensaje += f"\n\n{alerta}"

    await query.message.reply_text(mensaje)

    # 3) PDF al chat. Si falla, la factura sigue siendo valida (el CAE ya esta):
    #    avisamos y listo.
    try:
        pdf_url = fac.generar_pdf(res, ud)
        await query.message.reply_document(
            document=pdf_url, filename=f"{fac.nombre_pdf(res)}.pdf",
            reply_markup=boton_mail(res),
        )
        try:
            fac.guardar_pdf_url(res, pdf_url)
        except Exception as e:
            logger.error("No se pudo guardar pdf_url en Supabase: %s", e)
    except Exception as e:
        logger.error("Fallo la generacion/envio del PDF: %s", e)
        await query.message.reply_text(
            "⚠️ La factura está emitida OK, pero falló la generación del PDF. "
            "Podés bajarlo de Mis Comprobantes en ARCA."
        )

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /lote — varias facturas a consumidor final
# ---------------------------------------------------------------------------
@solo_autorizado
async def lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lote 15000 20000 12500 [26/06] [01/06-30/06]

    Varias facturas de un saque, TODAS a consumidor final sin datos.
    Los numeros son montos (aca no hay DNI: por eso es un comando aparte);
    una fecha y/o un periodo opcionales aplican a todas.
    """
    context.user_data.clear()
    montos = []

    descripcion, args = fac.extraer_descripcion(context.args or [])
    if descripcion:
        context.user_data["descripcion"] = descripcion

    for arg in fac.normalizar_args(args):
        periodo = fac.parsear_periodo(arg)
        if periodo is not None:
            context.user_data["serv_desde"], context.user_data["serv_hasta"] = periodo
            continue
        fecha = fac.parsear_fecha(arg)
        if fecha is not None:
            error = fac.validar_fecha(fecha)
            if error:
                await update.message.reply_text(error)
                return ConversationHandler.END
            context.user_data["fecha"] = fecha
            continue
        try:
            montos.append(fac.parsear_monto(arg))
        except (InvalidOperation, ArithmeticError, ValueError):
            await update.message.reply_text(
                f"No entendí «{arg}». En /lote van montos y, opcional, "
                f"fecha o período.\nEj: /lote 15000 20000 12500 01/06-30/06"
            )
            return ConversationHandler.END

    if len(montos) < 2:
        await update.message.reply_text(
            "El lote necesita al menos 2 montos (para una sola usá /facturar).\n"
            "Ej: /lote 15000 20000 12500 01/06-30/06"
        )
        return ConversationHandler.END
    if len(montos) > LOTE_MAX:
        await update.message.reply_text(f"Máximo {LOTE_MAX} facturas por lote.")
        return ConversationHandler.END

    context.user_data["montos"] = montos
    fecha = context.user_data.setdefault("fecha", hoy_ar())
    etiqueta_fecha = fecha.strftime("%d/%m/%Y") + (" (hoy)" if fecha == hoy_ar() else "")
    if context.user_data.get("serv_desde"):
        etiqueta_periodo = (f"{context.user_data['serv_desde'].strftime('%d/%m/%Y')} al "
                            f"{context.user_data['serv_hasta'].strftime('%d/%m/%Y')}")
    else:
        etiqueta_periodo = "= fecha de emisión"

    lineas = "\n".join(f"{i}. ${fmt_ars(m)}" for i, m in enumerate(montos, 1))
    botones = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar todas", callback_data="confirmar"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar"),
    ]])
    linea_detalle = (f"Detalle: {context.user_data['descripcion']}\n"
                     if context.user_data.get("descripcion") else "")
    umbrales = [fac.aviso_umbral(m, fac.DOC_TIPO_CF) for m in montos]
    linea_umbral = next((f"{u}\n\n" for u in umbrales if u), "")
    await update.message.reply_text(
        f"Vas a emitir {len(montos)} Facturas C — Servicios\n"
        f"Receptor: Consumidor Final (todas)\n"
        f"{linea_detalle}"
        f"Fecha: {etiqueta_fecha}\n"
        f"Período: {etiqueta_periodo}\n\n"
        f"{lineas}\n\n"
        f"{linea_umbral}"
        f"Total del lote: ${fmt_ars(sum(montos))}\n\n"
        f"{'⚠️ MODO TEST (no es real)' if not fac.PRODUCTION else '🔴 PRODUCCIÓN (real)'}\n\n"
        f"¿Confirmás?",
        reply_markup=botones,
    )
    return CONFIRMANDO_LOTE


async def confirmar_lote(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancelar":
        await query.edit_message_text("Cancelado. No se emitió nada.")
        return ConversationHandler.END

    ud = context.user_data
    montos = ud.get("montos")
    if not montos:
        await query.edit_message_text("Se perdieron los datos (¿se reinició el bot?). Arrancá de nuevo con /lote.")
        return ConversationHandler.END

    fecha = ud.get("fecha") or hoy_ar()
    total = len(montos)
    await query.edit_message_text(f"Emitiendo {total} facturas... ⏳")

    for i, monto in enumerate(montos, 1):
        # 1) Emitir. Si una falla, cortamos: las anteriores YA son reales.
        try:
            res = fac.emitir_factura_c(monto, fac.DOC_TIPO_CF, fac.DOC_NRO_CF,
                                       fac.COND_IVA_CF, fecha,
                                       ud.get("serv_desde"), ud.get("serv_hasta"))
        except Exception as e:
            restantes = ", ".join(f"${fmt_ars(m)}" for m in montos[i - 1:])
            await query.message.reply_text(
                f"❌ Falló la factura {i}/{total} (${fmt_ars(monto)}):\n{e}\n\n"
                f"Las anteriores SÍ se emitieron. Quedaron sin emitir: {restantes}.\n"
                f"Cuando se resuelva, relanzá el lote solo con esas."
            )
            return ConversationHandler.END

        linea = f"✅ {i}/{total} — ${fmt_ars(monto)} → N° {res['numero']} — CAE {res['CAE']}"

        # 2) Guardar en el log
        try:
            fac.guardar_factura(res, monto, fac.DOC_TIPO_CF, fac.DOC_NRO_CF,
                                fac.COND_IVA_CF, ud.get("descripcion"))
        except Exception as e:
            logger.error("Factura %s emitida pero fallo el guardado: %s", res["numero"], e)
            linea += "\n⚠️ No se guardó en Supabase: anotala manualmente."
        await query.message.reply_text(linea)

        # 3) PDF
        ud_factura = {"monto": monto, "doc_tipo": fac.DOC_TIPO_CF,
                      "doc_nro": fac.DOC_NRO_CF, "cond_iva": fac.COND_IVA_CF,
                      "descripcion": ud.get("descripcion")}
        try:
            pdf_url = fac.generar_pdf(res, ud_factura)
            await query.message.reply_document(
                document=pdf_url, filename=f"{fac.nombre_pdf(res)}.pdf",
            )
            fac.guardar_pdf_url(res, pdf_url)
        except Exception as e:
            logger.error("Fallo el PDF de la factura %s: %s", res["numero"], e)
            await query.message.reply_text(
                f"⚠️ El PDF de la N° {res['numero']} falló (la factura está OK). "
                f"Regeneralo con /pdf {res['numero']}."
            )

    cierre = f"🏁 Lote completo: {total}/{total} emitidas."
    alerta = fac.aviso_tope()
    if alerta:
        cierre += f"\n\n{alerta}"
    await query.message.reply_text(cierre)
    return ConversationHandler.END


# ---------------------------------------------------------------------------
# /nc — Nota de Credito C
# ---------------------------------------------------------------------------
@solo_autorizado
async def nc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/nc <nro_factura> [monto] — Nota de Credito C asociada a una factura.

    Sin monto anula el total; con monto es una NC parcial.
    """
    context.user_data.clear()
    args = context.args or []
    if not args or not args[0].isdigit():
        await update.message.reply_text(
            "Uso: /nc 5 → anula la Factura N° 5 completa\n"
            "     /nc 5 50000 → NC parcial por $50.000"
        )
        return ConversationHandler.END
    if fac.supabase is None:
        await update.message.reply_text("Supabase no configurado: no puedo buscar la factura.")
        return ConversationHandler.END

    numero = int(args[0])
    try:
        consulta = (
            fac.supabase.table("facturas_emitidas").select("*")
            .eq("pto_vta", fac.PUNTO_DE_VENTA).eq("cbte_tipo", fac.FACTURA_C)
            .eq("cbte_nro", numero).execute()
        )
    except Exception as e:
        await update.message.reply_text(f"❌ Error consultando Supabase:\n{e}")
        return ConversationHandler.END
    if not consulta.data:
        await update.message.reply_text(
            f"No encontré la Factura N° {numero} (PV {fac.PUNTO_DE_VENTA}) en el log."
        )
        return ConversationHandler.END

    fila = consulta.data[0]
    monto_original = float(fila["imp_total"])

    if len(args) >= 2:
        try:
            monto = fac.parsear_monto(args[1])
        except (InvalidOperation, ArithmeticError, ValueError):
            await update.message.reply_text(f"No entendí el monto «{args[1]}».")
            return ConversationHandler.END
        if monto > monto_original:
            await update.message.reply_text(
                f"La NC (${fmt_ars(monto)}) no puede superar el total de la "
                f"factura (${fmt_ars(monto_original)})."
            )
            return ConversationHandler.END
    else:
        monto = monto_original

    ud = context.user_data
    ud["nc_asociado"] = numero
    ud["monto"] = monto
    ud["doc_tipo"] = fila["doc_tipo"]
    ud["doc_nro"] = fila["doc_nro"]
    ud["cond_iva"] = fila["condicion_iva_receptor"]
    ud["descripcion"] = fila.get("descripcion")   # la NC hereda el detalle
    if fila.get("fch_serv_desde"):
        ud["serv_desde"] = date.fromisoformat(fila["fch_serv_desde"])
        ud["serv_hasta"] = date.fromisoformat(fila["fch_serv_hasta"])

    parcial = " (PARCIAL)" if monto < monto_original else " (total)"
    botones = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Confirmar", callback_data="confirmar"),
        InlineKeyboardButton("❌ Cancelar", callback_data="cancelar"),
    ]])
    await update.message.reply_text(
        f"Vas a emitir:\n\n"
        f"Nota de Crédito C{parcial}\n"
        f"Anula/ajusta: Factura C N° {numero} (${fmt_ars(monto_original)}, "
        f"{fila['fecha_cbte']})\n"
        f"Receptor: {descripcion_receptor(ud)}\n"
        f"Monto NC: ${fmt_ars(monto)}\n"
        f"Fecha: hoy\n\n"
        f"{'⚠️ MODO TEST (no es real)' if not fac.PRODUCTION else '🔴 PRODUCCIÓN (real)'}\n\n"
        f"¿Confirmás?",
        reply_markup=botones,
    )
    return CONFIRMANDO_NC


async def confirmar_nc(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "cancelar":
        await query.edit_message_text("Cancelado. No se emitió nada.")
        return ConversationHandler.END

    ud = context.user_data
    if ud.get("monto") is None or ud.get("nc_asociado") is None:
        await query.edit_message_text("Se perdieron los datos (¿se reinició el bot?). Arrancá de nuevo con /nc.")
        return ConversationHandler.END

    await query.edit_message_text("Emitiendo NC... ⏳")
    try:
        res = fac.emitir_factura_c(ud["monto"], ud["doc_tipo"], ud["doc_nro"],
                                   ud["cond_iva"], hoy_ar(),
                                   ud.get("serv_desde"), ud.get("serv_hasta"),
                                   cbte_tipo=fac.NOTA_CREDITO_C,
                                   asociado_nro=ud["nc_asociado"])
    except Exception as e:
        await query.message.reply_text(f"❌ Error al emitir la NC en AFIP:\n{e}")
        return ConversationHandler.END

    try:
        fac.guardar_factura(res, ud["monto"], ud["doc_tipo"], ud["doc_nro"],
                            ud["cond_iva"], ud.get("descripcion"))
        guardado_ok = True
    except Exception as e:
        guardado_ok = False
        logger.error("NC emitida pero fallo el guardado: %s", e)

    mensaje = (
        f"✅ Nota de Crédito C emitida\n\n"
        f"Anula/ajusta: Factura N° {ud['nc_asociado']}\n"
        f"Monto: ${fmt_ars(ud['monto'])}\n"
        f"Número NC: {res['numero']}\n"
        f"CAE: {res['CAE']}\n"
        f"Vto CAE: {res['CAEFchVto']}"
    )
    if not guardado_ok:
        mensaje += "\n\n⚠️ OJO: emitida en AFIP pero NO guardada en Supabase. Anotala."
    await query.message.reply_text(mensaje)

    try:
        pdf_url = fac.generar_pdf(res, ud)
        await query.message.reply_document(document=pdf_url, filename=f"{fac.nombre_pdf(res)}.pdf",
                                           reply_markup=boton_mail(res))
        fac.guardar_pdf_url(res, pdf_url)
    except Exception as e:
        logger.error("Fallo el PDF de la NC: %s", e)
        await query.message.reply_text("⚠️ La NC está emitida OK pero falló el PDF.")

    return ConversationHandler.END


# ---------------------------------------------------------------------------
# Comandos sueltos
# ---------------------------------------------------------------------------
async def cancelar_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Cancelado.")
    return ConversationHandler.END


async def cmd_id(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu chat_id es: {update.effective_chat.id}")


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bienvenida (/start, /ayuda). SIN allowlist a proposito: al usuario no
    autorizado lo guia a configurar su propia instancia (su chat_id)."""
    chat_id = update.effective_chat.id

    if MI_CHAT_ID is not None and chat_id != MI_CHAT_ID:
        await update.message.reply_text(
            "🧾 Hola, soy un facturador de ARCA — pero soy un bot personal: "
            "solo le respondo a mi dueño.\n\n"
            "¿Estás configurando tu propia instancia? Tu chat_id es:\n"
            f"{chat_id}\n\n"
            "Ponelo en MI_CHAT_ID en el .env y reiniciá el bot.\n\n"
            "¿Querés tu propio facturador? El código es libre:\n"
            "github.com/Lanuti-Franco/facturador-arca"
        )
        return

    modo = "⚠️ MODO TEST (las facturas no son reales)" if not fac.PRODUCTION \
        else "🔴 PRODUCCIÓN — las facturas son reales"
    await update.message.reply_text(
        "🧾 Hola, soy tu facturador de ARCA.\n"
        "Emito Factura C (monotributo) desde este chat: CAE y PDF con QR "
        "en segundos.\n\n"
        "Facturar:\n"
        "/facturar 15000 → consumidor final, directo al preview\n"
        "/facturar → paso a paso (CUIT/DNI, fecha, período)\n"
        "/lote 15000 20000 12500 → varias de un saque\n\n"
        "Después de emitir:\n"
        "📧 botón en cada PDF para mandarla por mail al cliente\n"
        "/nc 5 → nota de crédito (anula o ajusta la factura 5)\n"
        "/pdf 5 → regenerar un PDF\n\n"
        "Control:\n"
        "/resumen → el mes en curso, con total\n"
        "/csv 06/2026 → export para tu contador\n"
        "/tope → facturado 12 meses vs tu categoría\n"
        "/ultima → reconciliar contra ARCA\n\n"
        f"{modo}"
    )


def _parse_tipo_y_numero(args: list[str]) -> tuple[int, int, list[str]] | None:
    """Interpreta '[nc] <nro> [resto...]' -> (cbte_tipo, numero, resto)."""
    args = list(args)
    tipo = fac.FACTURA_C
    if args and args[0].lower() == "nc":
        tipo = fac.NOTA_CREDITO_C
        args = args[1:]
    if not args or not args[0].isdigit():
        return None
    return tipo, int(args[0]), args[1:]


def _cargar_comprobante(tipo: int, numero: int) -> tuple[dict, dict, dict] | None:
    """Lee un comprobante del log y arma (fila, res, ud). None si no existe."""
    consulta = (
        fac.supabase.table("facturas_emitidas").select("*")
        .eq("pto_vta", fac.PUNTO_DE_VENTA).eq("cbte_tipo", tipo)
        .eq("cbte_nro", numero).execute()
    )
    if not consulta.data:
        return None
    fila = consulta.data[0]
    res = {
        "numero": fila["cbte_nro"],
        "CAE": fila["cae"],
        "CAEFchVto": fila["cae_vto"],
        "fecha_int": int(fila["fecha_cbte"].replace("-", "")),
        "serv_desde_int": int((fila["fch_serv_desde"] or fila["fecha_cbte"]).replace("-", "")),
        "serv_hasta_int": int((fila["fch_serv_hasta"] or fila["fecha_cbte"]).replace("-", "")),
        "cbte_tipo": fila["cbte_tipo"],
        "asociado_nro": fila.get("asociado_cbte_nro"),
    }
    ud = {
        "monto": float(fila["imp_total"]),
        "doc_tipo": fila["doc_tipo"],
        "doc_nro": fila["doc_nro"],
        "cond_iva": fila["condicion_iva_receptor"],
        "descripcion": fila.get("descripcion"),
    }
    return fila, res, ud


@solo_autorizado
async def cmd_pdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/pdf <numero> (factura) o /pdf nc <numero> — regenera el PDF desde el log."""
    parseado = _parse_tipo_y_numero(context.args or [])
    if parseado is None:
        await update.message.reply_text("Uso: /pdf 123 — o /pdf nc 4 para una nota de crédito")
        return
    if fac.supabase is None:
        await update.message.reply_text("Supabase no configurado: no puedo leer la factura.")
        return
    tipo, numero, _ = parseado

    try:
        cargado = _cargar_comprobante(tipo, numero)
    except Exception as e:
        await update.message.reply_text(f"❌ Error consultando Supabase:\n{e}")
        return
    if cargado is None:
        await update.message.reply_text(
            f"No encontré la factura {numero} (PV {fac.PUNTO_DE_VENTA}) en el log."
        )
        return
    _, res, ud = cargado

    await update.message.reply_text("Generando PDF... ⏳")
    try:
        pdf_url = fac.generar_pdf(res, ud)
        await update.message.reply_document(document=pdf_url, filename=f"{fac.nombre_pdf(res)}.pdf")
        fac.guardar_pdf_url(res, pdf_url)
    except Exception as e:
        await update.message.reply_text(f"❌ Falló la generación del PDF:\n{e}")


def boton_mail(res: dict) -> InlineKeyboardMarkup:
    """Boton para mandar por mail un comprobante recien emitido."""
    return InlineKeyboardMarkup([[InlineKeyboardButton(
        "📧 Mandar por mail",
        callback_data=f"mail_{res['cbte_tipo']}_{res['numero']}",
    )]])


async def _mandar_comprobante(mensaje, context: ContextTypes.DEFAULT_TYPE,
                              tipo: int, numero: int,
                              email_explicito: str | None = None) -> None:
    """Carga un comprobante del log y lo manda por email. Responde en `mensaje`."""
    try:
        cargado = _cargar_comprobante(tipo, numero)
    except Exception as e:
        await mensaje.reply_text(f"❌ Error consultando Supabase:\n{e}")
        return
    if cargado is None:
        await mensaje.reply_text(
            f"No encontré el comprobante {numero} (PV {fac.PUNTO_DE_VENTA}) en el log."
        )
        return
    fila, res, ud = cargado

    destinatario = email_explicito or fac.email_de_cliente(ud["doc_tipo"], ud["doc_nro"])
    if destinatario is None:
        # Queda pendiente: el proximo email suelto que mande el usuario
        # se interpreta como "mandalo a esta direccion".
        context.user_data["mail_pendiente"] = (tipo, numero)
        await mensaje.reply_text(
            "No tengo email para ese receptor. Mandámelo acá nomás y sale."
        )
        return

    await mensaje.reply_text(f"Mandando a {destinatario}... ⏳")

    # PDF: reusa el link guardado si todavia vive; los links del SDK VENCEN
    # (~1 dia), asi que si la descarga falla se regenera desde el log.
    pdf = None
    if fila.get("pdf_url"):
        try:
            pdf = fac.descargar_pdf(fila["pdf_url"])
        except Exception:
            logger.info("pdf_url vencido para %s %s; regenerando", tipo, numero)
    try:
        if pdf is None:
            pdf_url = fac.generar_pdf(res, ud)
            fac.guardar_pdf_url(res, pdf_url)
            pdf = fac.descargar_pdf(pdf_url)
        fac.enviar_factura_email(destinatario, res, pdf)
    except Exception as e:
        await mensaje.reply_text(f"❌ No se pudo mandar:\n{e}")
        return

    confirmacion = f"📧 Enviada a {destinatario}."
    if email_explicito and ud["doc_tipo"] != fac.DOC_TIPO_CF:
        # Solo prometer memoria si el guardado REALMENTE funciono.
        if fac.recordar_email_cliente(ud["doc_tipo"], ud["doc_nro"], ud["cond_iva"], email_explicito):
            confirmacion += (
                f"\n(Recordado para {fmt_doc(ud['doc_tipo'], ud['doc_nro'])}: "
                f"la próxima sale con un toque del botón.)"
            )
        else:
            confirmacion += (
                "\n⚠️ No pude guardar el email para la próxima. ¿Corriste la "
                "última migración de schema_facturacion.sql en Supabase? "
                "(agrega la columna email a clientes)."
            )
    await mensaje.reply_text(confirmacion)


@solo_autorizado
async def cmd_mail(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/mail <nro> [email] — manda el comprobante por email al cliente.

    Sin email: usa el guardado en `clientes` para ese receptor (si existe).
    Con email explicito y receptor identificado, lo recuerda para la proxima.
    """
    parseado = _parse_tipo_y_numero(context.args or [])
    if parseado is None:
        await update.message.reply_text(
            "Uso: /mail 5 cliente@ejemplo.com\n"
            "     /mail 5 → usa el email guardado del receptor\n"
            "     /mail nc 2 ... → para una nota de crédito"
        )
        return
    if fac.supabase is None:
        await update.message.reply_text("Supabase no configurado: no puedo leer la factura.")
        return
    tipo, numero, resto = parseado

    email_explicito = None
    if resto:
        if not fac.es_email(resto[0]):
            await update.message.reply_text(f"«{resto[0]}» no parece un email válido.")
            return
        email_explicito = resto[0].strip()

    await _mandar_comprobante(update.message, context, tipo, numero, email_explicito)


@solo_autorizado
async def boton_mail_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """El boton 📧 del PDF recien emitido: manda al email guardado del receptor."""
    query = update.callback_query
    await query.answer()
    _, tipo, numero = query.data.split("_")
    await _mandar_comprobante(query.message, context, int(tipo), int(numero))


@solo_autorizado
async def recibir_email_suelto(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Un email tirado al chat despues de un "no tengo email": lo completa."""
    pendiente = context.user_data.get("mail_pendiente")
    if pendiente is None or not fac.es_email(update.message.text):
        return
    context.user_data.pop("mail_pendiente", None)
    tipo, numero = pendiente
    await _mandar_comprobante(update.message, context, tipo, numero,
                              update.message.text.strip())


@solo_autorizado
async def cmd_ultima(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Reconciliacion: compara el ultimo comprobante en ARCA vs Supabase.

    Cubre la ventana de riesgo conocida: si el proceso se cae entre "ARCA dio
    el CAE" y "guarde en Supabase", aca aparece el desfase.
    """
    try:
        ultimo_arca = fac.get_afip().ElectronicBilling.getLastVoucher(
            fac.PUNTO_DE_VENTA, fac.FACTURA_C
        )
    except Exception as e:
        await update.message.reply_text(f"❌ No pude consultar ARCA:\n{e}")
        return

    ultimo_db = None
    if fac.supabase is not None:
        try:
            res = (
                fac.supabase.table("facturas_emitidas")
                .select("cbte_nro")
                .eq("pto_vta", fac.PUNTO_DE_VENTA)
                .eq("cbte_tipo", fac.FACTURA_C)
                .order("cbte_nro", desc=True)
                .limit(1)
                .execute()
            )
            ultimo_db = res.data[0]["cbte_nro"] if res.data else 0
        except Exception as e:
            await update.message.reply_text(f"❌ No pude consultar Supabase:\n{e}")
            return

    mensaje = (
        f"Último comprobante (PV {fac.PUNTO_DE_VENTA}, Factura C):\n\n"
        f"ARCA: {ultimo_arca}\n"
        f"Supabase: {ultimo_db if ultimo_db is not None else 'sin configurar'}"
    )
    if ultimo_db is not None:
        if ultimo_arca == ultimo_db:
            mensaje += "\n\n✅ Todo reconciliado."
        else:
            mensaje += (
                f"\n\n⚠️ Desfase de {ultimo_arca - ultimo_db}: hay comprobantes "
                "en ARCA que no están en el log. Revisalos y cargalos a mano."
            )
    if not fac.PRODUCTION:
        mensaje += (
            "\n\n(Modo test: el CUIT de testing es compartido, el desfase "
            "acá es normal y esperable.)"
        )
    await update.message.reply_text(mensaje)


@solo_autorizado
async def cmd_resumen(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/resumen [6 | 06/2026 | 2026 | 01/06-30/06] — emitidas + total del período."""
    rango = fac.rango_periodo(fac.normalizar_args(context.args or []))
    if rango is None:
        await update.message.reply_text(MENSAJE_USO_PERIODO)
        return
    try:
        await update.message.reply_text(fac.texto_resumen(*rango))
    except Exception as e:
        await update.message.reply_text(f"❌ Error consultando Supabase:\n{e}")


@solo_autorizado
async def cmd_csv(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/csv [período] — export para el contador. NC con importe en negativo."""
    rango = fac.rango_periodo(fac.normalizar_args(context.args or []))
    if rango is None:
        await update.message.reply_text(MENSAJE_USO_PERIODO)
        return
    desde, hasta = rango
    try:
        contenido = fac.csv_periodo(desde, hasta)
    except Exception as e:
        await update.message.reply_text(f"❌ Error consultando Supabase:\n{e}")
        return
    if contenido is None:
        await update.message.reply_text(
            f"Sin comprobantes entre {desde.strftime('%d/%m/%Y')} y {hasta.strftime('%d/%m/%Y')}."
        )
        return

    nombre = f"comprobantes_{desde.strftime('%Y%m%d')}_{hasta.strftime('%Y%m%d')}.csv"
    await update.message.reply_document(document=contenido, filename=nombre)


@solo_autorizado
async def cmd_tope(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/tope — facturado de los últimos 12 meses vs tope de la categoría."""
    try:
        total = fac.total_facturado_12m()
    except Exception as e:
        await update.message.reply_text(f"❌ Error consultando Supabase:\n{e}")
        return
    mensaje = f"Facturado últimos 12 meses: ${fmt_ars(total)}"
    if fac.MONOTRIBUTO_TOPE:
        pct = total / fac.MONOTRIBUTO_TOPE * 100
        mensaje += (
            f"\nTope de tu categoría: ${fmt_ars(fac.MONOTRIBUTO_TOPE)}\n"
            f"Usado: {pct:.1f}% — te quedan ${fmt_ars(max(0, fac.MONOTRIBUTO_TOPE - total))}"
        )
    else:
        mensaje += (
            "\n\n(Sin tope configurado: poné MONOTRIBUTO_TOPE en el .env con el "
            "límite anual de tu categoría para activar las alertas.)"
        )
    await update.message.reply_text(mensaje)


async def job_resumen_mensual(context: ContextTypes.DEFAULT_TYPE):
    """El 1° de cada mes: resumen del mes que cerró, directo al chat."""
    hoy = hoy_ar()
    hasta = hoy.replace(day=1) - timedelta(days=1)
    desde = hasta.replace(day=1)
    try:
        texto = fac.texto_resumen(desde, hasta)
    except Exception as e:
        logger.error("Fallo el resumen mensual automatico: %s", e)
        return
    extra = fac.aviso_tope()
    if extra:
        texto += f"\n\n{extra}"
    await context.bot.send_message(MI_CHAT_ID, f"🗓 Cerró el mes:\n\n{texto}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
async def _registrar_menu(app) -> None:
    """El menu de comandos que Telegram muestra al tipear '/'."""
    from telegram import BotCommand
    await app.bot.set_my_commands([
        BotCommand("facturar", "Nueva Factura C (monto opcional de una)"),
        BotCommand("lote", "Varias facturas de un saque"),
        BotCommand("nc", "Nota de crédito sobre una factura"),
        BotCommand("resumen", "Comprobantes del período + total"),
        BotCommand("csv", "Export para el contador"),
        BotCommand("mail", "Mandar un comprobante por email"),
        BotCommand("pdf", "Regenerar el PDF de un comprobante"),
        BotCommand("tope", "Facturado 12 meses vs tu categoría"),
        BotCommand("ultima", "Reconciliar contra ARCA"),
        BotCommand("ayuda", "Qué sé hacer"),
        BotCommand("cancelar", "Cancelar el flujo actual"),
    ])


def main():
    if not TELEGRAM_TOKEN:
        raise SystemExit("Falta TELEGRAM_TOKEN en las variables de entorno.")
    if MI_CHAT_ID is None:
        # Fail-closed: sin allowlist el bot quedaria abierto a cualquiera.
        raise SystemExit(
            "Falta MI_CHAT_ID en las variables de entorno. Sin allowlist, "
            "cualquiera podría facturar a tu nombre. Usá /id para conocer el tuyo."
        )
    if fac.supabase is None:
        logger.warning("Supabase no configurado: las facturas NO se van a guardar.")

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).post_init(_registrar_menu).build()

    conv = ConversationHandler(
        entry_points=[
            CommandHandler("facturar", facturar),
            CommandHandler("lote", lote),
            CommandHandler("nc", nc),
        ],
        states={
            ELIGIENDO_RECEPTOR: [CallbackQueryHandler(elegir_receptor, pattern="^r_")],
            PIDIENDO_CUIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_cuit),
                CallbackQueryHandler(elegir_reciente, pattern="^rec_"),
            ],
            ELIGIENDO_COND_IVA: [CallbackQueryHandler(elegir_cond_iva, pattern="^iva_")],
            PIDIENDO_MONTO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_monto)],
            CONFIRMANDO: [
                CallbackQueryHandler(confirmar, pattern="^(confirmar|cancelar)$"),
                CallbackQueryHandler(pedir_fecha, pattern="^fecha$"),
                CallbackQueryHandler(pedir_periodo, pattern="^periodo$"),
            ],
            PIDIENDO_FECHA: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_fecha)],
            PIDIENDO_PERIODO: [MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_periodo)],
            CONFIRMANDO_LOTE: [CallbackQueryHandler(confirmar_lote, pattern="^(confirmar|cancelar)$")],
            CONFIRMANDO_NC: [CallbackQueryHandler(confirmar_nc, pattern="^(confirmar|cancelar)$")],
        },
        fallbacks=[CommandHandler("cancelar", cancelar_cmd)],
        allow_reentry=True,   # /facturar en medio de un flujo arranca de cero
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler(["start", "ayuda", "help"], cmd_start))
    app.add_handler(CommandHandler("id", cmd_id))
    app.add_handler(CommandHandler("ultima", cmd_ultima))
    app.add_handler(CommandHandler("pdf", cmd_pdf))
    app.add_handler(CommandHandler("mail", cmd_mail))
    app.add_handler(CallbackQueryHandler(boton_mail_handler, pattern="^mail_"))
    # Despues del ConversationHandler a proposito: si hay un flujo activo
    # (monto, CUIT, etc.), la conversacion se queda con el mensaje; si no,
    # un email suelto completa el /mail pendiente.
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, recibir_email_suelto))
    app.add_handler(CommandHandler("resumen", cmd_resumen))
    app.add_handler(CommandHandler("csv", cmd_csv))
    app.add_handler(CommandHandler("tope", cmd_tope))

    # Resumen automatico: el 1° de cada mes a las 9:00 (hora argentina)
    if app.job_queue is not None:
        app.job_queue.run_monthly(
            job_resumen_mensual, when=dtime(9, 0, tzinfo=fac.TZ_AR), day=1
        )
    else:
        logger.warning(
            'Sin job-queue (instala python-telegram-bot[job-queue]): '
            'no habra resumen mensual automatico.'
        )

    if fac.PRODUCTION:
        logger.warning("🔴 MODO PRODUCCIÓN: CUIT %s, PV %s — las facturas son REALES.",
                       fac.CUIT, fac.PUNTO_DE_VENTA)
    else:
        logger.info("⚠️ Modo test (CUIT de testing del SDK, PV 1). PRODUCTION=true en el .env para produccion.")
    logger.info("Bot corriendo (long polling)...")
    app.run_polling()


if __name__ == "__main__":
    main()
