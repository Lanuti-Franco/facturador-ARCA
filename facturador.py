"""
Core del facturador — ARCA (via Afip SDK) + Supabase (log) + PDF con QR.

Este modulo NO sabe nada de Telegram: es la logica de negocio completa.
La capa de conversacion vive en bot_telegram.py (y cualquier otra UI futura
—WhatsApp, CLI, web— se enchufa aca sin tocar nada de esto).

Modo test vs produccion: se controla SOLO desde el .env (PRODUCTION=true).
El codigo no contiene CUIT, punto de venta ni certificados propios: el repo
es publicable sin exponer datos personales.
"""

import os
import re
import csv
import io
import json
import base64
import logging
import smtplib
import urllib.request
from email.message import EmailMessage
from datetime import datetime, date, timedelta
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

import segno

from dotenv import load_dotenv
load_dotenv()  # ⚠️ debe ir ANTES de leer os.environ

from afip import Afip
from supabase import create_client

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CONFIG (todo desde variables de entorno)
# ---------------------------------------------------------------------------
AFIP_ACCESS_TOKEN = os.environ.get("AFIP_ACCESS_TOKEN")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_SECRET_KEY = os.environ.get("SUPABASE_SECRET_KEY")

PRODUCTION = os.environ.get("PRODUCTION", "").strip().lower() in ("1", "true", "si", "sí")

if PRODUCTION:
    try:
        CUIT = int(os.environ["AFIP_CUIT"])
        PUNTO_DE_VENTA = int(os.environ["AFIP_PUNTO_VENTA"])
        with open(os.environ["AFIP_CERT_PATH"]) as f:
            AFIP_CERT = f.read()
        with open(os.environ["AFIP_KEY_PATH"]) as f:
            AFIP_KEY = f.read()
    except (KeyError, OSError, ValueError) as e:
        raise SystemExit(
            f"PRODUCTION=true pero la configuración está incompleta ({e}).\n"
            "El .env necesita: AFIP_CUIT, AFIP_PUNTO_VENTA, AFIP_CERT_PATH "
            "y AFIP_KEY_PATH (rutas legibles al .crt y la .key)."
        )
else:
    CUIT = 20409378472         # CUIT de testing COMPARTIDO del SDK (no es de nadie)
    PUNTO_DE_VENTA = 1         # PV del entorno de homologacion
    AFIP_CERT = AFIP_KEY = None

FACTURA_C = 11
NOTA_CREDITO_C = 13
# Que vendes: 1 = Productos, 2 = Servicios (default), 3 = ambos.
# Se define por instancia en el .env. OJO: no es la letra del comprobante
# (eso sigue siendo Factura C) — es el campo Concepto del WSFE, y cambia
# las reglas: con productos (1) no hay periodo de servicio y la ventana
# retroactiva baja de 10 a 5 dias. Verificado en homologacion (jul-2026).
CONCEPTO = int(os.environ.get("CONCEPTO", "2"))
if CONCEPTO not in (1, 2, 3):
    raise SystemExit(f"CONCEPTO={CONCEPTO} inválido: usá 1 (productos), 2 (servicios) o 3 (ambos).")
CONCEPTO_DESC = {1: "Productos", 2: "Servicios", 3: "Productos y Servicios"}[CONCEPTO]
# Las fechas de servicio (periodo) existen solo para conceptos 2 y 3.
USA_PERIODO = CONCEPTO != 1

# Titulo y codigo impresos en el PDF segun tipo de comprobante
TIPOS_CBTE = {
    FACTURA_C: ("FACTURA", "COD. 011"),
    NOTA_CREDITO_C: ("NOTA DE CRÉDITO", "COD. 013"),
}

# Tope anual de TU categoria de monotributo (en pesos, sin puntos), para la
# alerta de recategorizacion. Se actualiza cada semestre: no se hardcodea.
# Vacio = alerta desactivada.
MONOTRIBUTO_TOPE = (
    float(os.environ["MONOTRIBUTO_TOPE"]) if os.environ.get("MONOTRIBUTO_TOPE") else None
)

# Umbral de identificacion del consumidor final (RG 5700/2025: $10.000.000).
# Igual o arriba de este monto NO se puede facturar a CF anonimo: hay que
# identificar al receptor. Se actualiza por resolucion -> vive en el .env.
# Vacio = alerta desactivada.
UMBRAL_CF = float(os.environ["UMBRAL_CF"]) if os.environ.get("UMBRAL_CF") else None

# Datos del emisor para el PDF (opcionales; si faltan, el PDF sale con "-").
# El nombre debe coincidir con el que figura en ARCA.
EMISOR_NOMBRE = os.environ.get("EMISOR_NOMBRE", "-")
EMISOR_DOMICILIO = os.environ.get("EMISOR_DOMICILIO", "-")
EMISOR_INICIO_ACTIVIDADES = os.environ.get("EMISOR_INICIO_ACTIVIDADES", "-")
FACTURA_DESCRIPCION = os.environ.get("FACTURA_DESCRIPCION", "Servicios")

# Logo opcional para el encabezado del PDF (PNG o JPG; vacío = sin logo).
# Se incrusta como data URI en el HTML, igual que el QR. Se lee en cada
# PDF: cambiar el archivo no requiere reiniciar el bot.
LOGO_PATH = os.environ.get("LOGO_PATH")

# Email saliente (para mandarle la factura al cliente). Con Gmail:
# SMTP_USER = tu casilla, SMTP_PASS = una "contraseña de aplicación"
# (myaccount.google.com/apppasswords — NO tu contraseña normal).
# Sin estas variables, /mail avisa que falta configurar y no rompe nada.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "465"))
SMTP_USER = os.environ.get("SMTP_USER")
SMTP_PASS = os.environ.get("SMTP_PASS")

# Receptor default: consumidor final anonimo (sin doc). Es el caso del ~70-95%
# de las facturas. cliente_id NULL en la tabla significa exactamente esto.
DOC_TIPO_CF = 99               # 99 = Consumidor Final (sin identificar)
DOC_NRO_CF = 0
COND_IVA_CF = 5                # 5 = Consumidor Final

DOC_TIPO_CUIT = 80             # 80 = CUIT
DOC_TIPO_DNI = 96              # 96 = DNI (consumidor final identificado)

# Condiciones IVA del receptor ofrecidas cuando se factura identificado.
# ✅ Codigos CONFIRMADOS contra FEParamGetCondicionIvaReceptor (6-jul-2026),
#    todos validos para comprobantes clase C. El 5 (Consumidor Final) cubre
#    el caso "consumidor final identificado" (ej: monto arriba del umbral).
CONDICIONES_IVA = {
    1: "Resp. Inscripto",
    6: "Monotributo",
    4: "IVA Exento",
    15: "IVA No Alcanzado",
    5: "Consumidor Final",
}

# WSFE acepta CbteFch hacia atras: 10 dias para servicios (concepto 2/3),
# 5 para productos (concepto 1).
DIAS_ATRAS_MAX = 10 if USA_PERIODO else 5

# Las fechas de los comprobantes SIEMPRE en hora argentina, aunque corra en
# un host en UTC (si no, de 21:00 a 00:00 la factura sale con fecha del dia
# siguiente).
TZ_AR = ZoneInfo("America/Argentina/Buenos_Aires")

# Cliente de Supabase (se crea una vez, al importar)
supabase = None
if SUPABASE_URL and SUPABASE_SECRET_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_SECRET_KEY)


# ---------------------------------------------------------------------------
# Parseo y formato
# ---------------------------------------------------------------------------
def parsear_monto(texto: str) -> float:
    """Acepta formato argentino Y estadounidense, con o sin $:
    15000 / 15.000 / 15.000,50 / 15000,50 / 15,000 / 15,000.00 / 15000.5

    Regla anti-ambiguedad: si aparecen ambos separadores, el que esta mas a
    la DERECHA es el decimal. Si hay uno solo agrupando de a 3, son miles.
    Devuelve float redondeado a 2 decimales. Lanza InvalidOperation si no
    es un monto valido o es <= 0.
    """
    t = texto.strip().replace(" ", "").lstrip("$")
    if "," in t and "." in t:
        if t.rfind(",") > t.rfind("."):
            t = t.replace(".", "").replace(",", ".")   # 450.000,00 (AR)
        else:
            t = t.replace(",", "")                     # 450,000.00 (US)
    elif re.fullmatch(r"\d{1,3}([.,]\d{3})+", t):
        t = re.sub(r"[.,]", "", t)                     # 15.000 / 450,000 -> miles
    else:
        t = t.replace(",", ".")                        # 15000,50 -> decimal
    monto = float(round(Decimal(t), 2))
    if monto <= 0:
        raise InvalidOperation
    return monto


def hoy_ar() -> date:
    return datetime.now(TZ_AR).date()


def parsear_fecha(texto: str) -> date | None:
    """Acepta dd/mm, dd/mm/aaaa, dd-mm, dd-mm-aaaa u 'hoy'. None si no parsea."""
    t = texto.strip().lower().replace("-", "/")
    if t == "hoy":
        return hoy_ar()
    partes = t.split("/")
    try:
        if len(partes) == 2:
            return date(hoy_ar().year, int(partes[1]), int(partes[0]))
        if len(partes) == 3:
            anio = int(partes[2])
            if anio < 100:
                anio += 2000
            return date(anio, int(partes[1]), int(partes[0]))
    except ValueError:
        pass
    return None


def validar_fecha(fecha: date) -> str | None:
    """None si la fecha es emitible; si no, el mensaje de error para el chat."""
    hoy = hoy_ar()
    if fecha > hoy:
        return "La fecha no puede ser futura. Probá de nuevo."
    if fecha < hoy - timedelta(days=DIAS_ATRAS_MAX):
        return (
            f"Máximo {DIAS_ATRAS_MAX} días para atrás "
            f"(desde el {(hoy - timedelta(days=DIAS_ATRAS_MAX)).strftime('%d/%m/%Y')}). Probá de nuevo."
        )
    return None


def normalizar_args(args: list[str]) -> list[str]:
    """Re-tokeniza los args pegando los periodos escritos con espacios.

    Telegram separa por espacios, asi que "01/06 - 30/06" llega en 3 pedazos.
    Aca "fecha [-|a|al|hasta] fecha" se colapsa a "fecha-fecha" (un token).
    """
    texto = " ".join(args)
    texto = re.sub(
        r"(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\s*(?:-|al|a|hasta)\s*(\d{1,2}/\d{1,2}(?:/\d{2,4})?)",
        r"\1-\2",
        texto,
    )
    return texto.split()


def extraer_descripcion(args: list[str]) -> tuple[str | None, list[str]]:
    """Saca el texto entre comillas de los args: ("detalle", args restantes).

    Telegram parte por espacios, asi que '"diseño web junio"' llega en
    pedazos; aca se rearma. Acepta comillas rectas y tipograficas.
    """
    texto = " ".join(args)
    m = re.search(r'["“”\'‘’]([^"“”\'‘’]+)["“”\'‘’]', texto)
    if not m:
        return None, args
    resto = (texto[:m.start()] + " " + texto[m.end():]).split()
    return m.group(1).strip(), resto


def parsear_periodo(texto: str) -> tuple[date, date] | None:
    """Dos fechas en un texto: '01/06-30/06', '01/06 al 30/06', '1/6 30/6'..."""
    fechas = re.findall(r"\d{1,2}/\d{1,2}(?:/\d{2,4})?", texto)
    if len(fechas) != 2:
        return None
    desde, hasta = parsear_fecha(fechas[0]), parsear_fecha(fechas[1])
    if desde is None or hasta is None or desde > hasta:
        return None
    return desde, hasta


def parsear_cuit(texto: str) -> int | None:
    """Valida un CUIT (con o sin guiones) incluido el digito verificador."""
    t = re.sub(r"[-.\s]", "", texto.strip())
    if not re.fullmatch(r"\d{11}", t):
        return None
    digitos = [int(c) for c in t]
    pesos = [5, 4, 3, 2, 7, 6, 5, 4, 3, 2]
    resto = sum(d * p for d, p in zip(digitos, pesos)) % 11
    verificador = 0 if resto == 0 else 11 - resto
    if verificador == 10 or verificador != digitos[10]:
        return None
    return int(t)


def parsear_doc(texto: str) -> tuple[int, int] | None:
    """Detecta CUIT (11 digitos, valida verificador) o DNI (7-8 digitos).

    Devuelve (doc_tipo, numero) o None si no es ni una cosa ni la otra.
    """
    t = re.sub(r"[-.\s]", "", texto.strip())
    if re.fullmatch(r"\d{11}", t):
        cuit = parsear_cuit(t)
        return (DOC_TIPO_CUIT, cuit) if cuit is not None else None
    if re.fullmatch(r"\d{7,8}", t):
        return (DOC_TIPO_DNI, int(t))
    return None


def fmt_cuit(cuit: int) -> str:
    s = str(cuit)
    return f"{s[:2]}-{s[2:10]}-{s[10]}"


def fmt_ars(monto: float) -> str:
    # 15000.5 -> "15.000,50" (formato argentino)
    return f"{monto:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def fmt_doc(doc_tipo: int, nro: int) -> str:
    if doc_tipo == DOC_TIPO_CUIT:
        return f"CUIT {fmt_cuit(nro)}"
    return "DNI " + f"{nro:,}".replace(",", ".")


def descripcion_receptor(ud: dict) -> str:
    if ud.get("doc_tipo") in (DOC_TIPO_CUIT, DOC_TIPO_DNI):
        cond = CONDICIONES_IVA.get(ud.get("cond_iva"), f"cond. {ud.get('cond_iva')}")
        return f"{fmt_doc(ud['doc_tipo'], ud['doc_nro'])} — {cond}"
    return "Consumidor Final"


# ---------------------------------------------------------------------------
# Emision en ARCA
# ---------------------------------------------------------------------------
def get_afip() -> Afip:
    opciones = {"CUIT": CUIT, "production": PRODUCTION}
    if AFIP_ACCESS_TOKEN:
        opciones["access_token"] = AFIP_ACCESS_TOKEN
    if AFIP_CERT and AFIP_KEY:
        opciones["cert"] = AFIP_CERT
        opciones["key"] = AFIP_KEY
    return Afip(opciones)


def emitir_factura_c(importe_total: float, doc_tipo: int, doc_nro: int,
                     cond_iva: int, fecha: date,
                     serv_desde: date | None = None,
                     serv_hasta: date | None = None,
                     cbte_tipo: int = FACTURA_C,
                     asociado_nro: int | None = None) -> dict:
    """Emite Factura C o (con cbte_tipo=13 y asociado_nro) Nota de Credito C."""
    afip = get_afip()

    ultimo = afip.ElectronicBilling.getLastVoucher(PUNTO_DE_VENTA, cbte_tipo)
    numero = ultimo + 1
    fecha_int = int(fecha.strftime("%Y%m%d"))
    # Periodo facturado (solo servicios): si no se indica, default = emision.
    # Para productos (concepto 1) estas fechas NO deben mandarse a ARCA.
    if USA_PERIODO:
        desde_int = int((serv_desde or fecha).strftime("%Y%m%d"))
        hasta_int = int((serv_hasta or fecha).strftime("%Y%m%d"))
    else:
        desde_int = hasta_int = None

    data = {
        "CantReg": 1,
        "PtoVta": PUNTO_DE_VENTA,
        "CbteTipo": cbte_tipo,
        "Concepto": CONCEPTO,
        "DocTipo": doc_tipo,
        "DocNro": doc_nro,
        "CbteDesde": numero,
        "CbteHasta": numero,
        "CbteFch": fecha_int,
        "ImpTotal": importe_total,
        "ImpTotConc": 0,
        "ImpNeto": importe_total,      # Factura C: neto = total
        "ImpOpEx": 0,
        "ImpIVA": 0,
        "ImpTrib": 0,
        "MonId": "PES",
        "MonCotiz": 1,
        "CondicionIVAReceptorId": cond_iva,
    }

    if USA_PERIODO:
        data["FchServDesde"] = desde_int
        data["FchServHasta"] = hasta_int
        data["FchVtoPago"] = fecha_int

    if asociado_nro is not None:
        # NC/ND deben referenciar el comprobante original (RG 4540/2019)
        data["CbtesAsoc"] = [{
            "Tipo": FACTURA_C,
            "PtoVta": PUNTO_DE_VENTA,
            "Nro": asociado_nro,
            "Cuit": str(CUIT),
        }]

    res = afip.ElectronicBilling.createVoucher(data)
    res["numero"] = numero
    res["fecha_int"] = fecha_int
    res["serv_desde_int"] = desde_int
    res["serv_hasta_int"] = hasta_int
    res["cbte_tipo"] = cbte_tipo
    res["asociado_nro"] = asociado_nro
    return res


# ---------------------------------------------------------------------------
# Guardado en Supabase
# ---------------------------------------------------------------------------
def _fecha_int_a_iso(fecha_int: int) -> str:
    # 20260705 -> "2026-07-05"
    s = str(fecha_int)
    return f"{s[0:4]}-{s[4:6]}-{s[6:8]}"


def guardar_factura(res: dict, importe_total: float, doc_tipo: int, doc_nro: int,
                    cond_iva: int, descripcion: str | None = None) -> None:
    """Guarda el comprobante emitido. Lanza excepcion si falla (para avisar)."""
    if supabase is None:
        raise RuntimeError("Supabase no configurado (falta URL o key en el .env)")

    fila = {
        "cliente_id": None,
        # Snapshot del receptor tal como fue a ARCA: la factura es inmutable,
        # no puede depender de una fila de "clientes" que alguien edite despues.
        "doc_tipo": doc_tipo,
        "doc_nro": doc_nro,
        "condicion_iva_receptor": cond_iva,
        "pto_vta": PUNTO_DE_VENTA,
        "cbte_tipo": res.get("cbte_tipo", FACTURA_C),
        "cbte_nro": res["numero"],
        "concepto": CONCEPTO,
        "imp_total": importe_total,
        # NULL en concepto 1 (productos): no hay periodo de servicio
        "fch_serv_desde": _fecha_int_a_iso(res["serv_desde_int"]) if res.get("serv_desde_int") else None,
        "fch_serv_hasta": _fecha_int_a_iso(res["serv_hasta_int"]) if res.get("serv_hasta_int") else None,
        "cae": res["CAE"],
        "cae_vto": res["CAEFchVto"],        # el SDK ya lo devuelve como yyyy-mm-dd
        "fecha_cbte": _fecha_int_a_iso(res["fecha_int"]),
    }
    # Columnas de migraciones posteriores: solo se mandan si tienen valor,
    # asi el guardado basico sigue funcionando aunque falte alguna migracion.
    if res.get("asociado_nro") is not None:
        fila["asociado_cbte_nro"] = res["asociado_nro"]
    if descripcion:
        fila["descripcion"] = descripcion

    # .execute() lanza error si el unique rebota (duplicado) -> lo dejamos propagar
    supabase.table("facturas_emitidas").insert(fila).execute()


def guardar_pdf_url(res: dict, url: str) -> None:
    """Completa pdf_url en la fila ya insertada. Best-effort."""
    if supabase is None:
        return
    (supabase.table("facturas_emitidas")
        .update({"pdf_url": url})
        .eq("pto_vta", PUNTO_DE_VENTA)
        .eq("cbte_tipo", res.get("cbte_tipo", FACTURA_C))
        .eq("cbte_nro", res["numero"])
        .execute())


def receptores_recientes(limite: int = 4) -> list[dict]:
    """Receptores identificados ya usados, desde el log (mas frecuentes primero).

    No hay ABM de clientes: "recordar" un receptor es simplemente haberle
    facturado alguna vez. Se filtra por el PV actual para no mezclar los
    docs de prueba del modo test con los reales.
    """
    if supabase is None:
        return []
    try:
        res = (
            supabase.table("facturas_emitidas")
            .select("doc_tipo, doc_nro, condicion_iva_receptor")
            .eq("pto_vta", PUNTO_DE_VENTA)
            .neq("doc_tipo", DOC_TIPO_CF)
            .order("created_at", desc=True)
            .limit(50)
            .execute()
        )
    except Exception as e:
        logger.error("No pude leer receptores recientes: %s", e)
        return []

    vistos: dict[tuple, int] = {}
    for fila in res.data:
        clave = (fila["doc_tipo"], fila["doc_nro"], fila["condicion_iva_receptor"])
        vistos[clave] = vistos.get(clave, 0) + 1
    # Mas usados primero; a igual uso, gana el mas reciente (orden de insercion).
    ordenados = sorted(vistos.items(), key=lambda kv: -kv[1])
    return [
        {"doc_tipo": k[0], "doc_nro": k[1], "cond_iva": k[2], "veces": v}
        for k, v in ordenados[:limite]
    ]


# ---------------------------------------------------------------------------
# Email al cliente
# ---------------------------------------------------------------------------
def es_email(texto: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", texto.strip()) is not None


def descargar_pdf(url: str) -> bytes:
    with urllib.request.urlopen(url) as r:
        return r.read()


def enviar_factura_email(destinatario: str, res: dict, pdf: bytes) -> None:
    """Manda el comprobante adjunto por SMTP. Lanza excepcion si falla."""
    if not SMTP_USER or not SMTP_PASS:
        raise RuntimeError(
            "Email no configurado: falta SMTP_USER / SMTP_PASS en el .env "
            "(con Gmail usá una contraseña de aplicación)."
        )

    tipo = "Nota de Crédito C" if res.get("cbte_tipo") == NOTA_CREDITO_C else "Factura C"
    numero_completo = f"{PUNTO_DE_VENTA:05d}-{res['numero']:08d}"
    # En ARCA el nombre figura EN MAYUSCULAS; para un mail queda gritado.
    remitente = EMISOR_NOMBRE.title() if EMISOR_NOMBRE != "-" else SMTP_USER

    msg = EmailMessage()
    msg["Subject"] = f"{tipo} {numero_completo} — {remitente}"
    msg["From"] = f"{remitente} <{SMTP_USER}>"
    msg["To"] = destinatario
    msg.set_content(
        f"Hola,\n\n"
        f"Te adjunto la {tipo} N° {numero_completo}.\n\n"
        f"Saludos,\n{remitente}"
    )
    msg.add_attachment(pdf, maintype="application", subtype="pdf",
                       filename=f"{nombre_pdf(res)}.pdf")

    with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT) as smtp:
        smtp.login(SMTP_USER, SMTP_PASS)
        smtp.send_message(msg)


def email_de_cliente(doc_tipo: int, doc_nro: int) -> str | None:
    """Busca en `clientes` el email guardado para ese documento."""
    if supabase is None or doc_tipo == DOC_TIPO_CF:
        return None
    try:
        res = (supabase.table("clientes").select("email")
               .eq("doc_tipo", doc_tipo).eq("doc_nro", doc_nro)
               .not_.is_("email", "null").limit(1).execute())
        return res.data[0]["email"] if res.data else None
    except Exception as e:
        logger.error("No pude buscar el email del cliente: %s", e)
        return None


def recordar_email_cliente(doc_tipo: int, doc_nro: int, cond_iva: int, email: str) -> bool:
    """Guarda/actualiza el email en `clientes` para reusarlo.

    Devuelve False si no se pudo (p.ej. falta la migracion de la columna
    email): el que llama NO debe decir "recordado" si esto fallo.
    """
    if supabase is None or doc_tipo == DOC_TIPO_CF:
        return False
    try:
        existente = (supabase.table("clientes").select("id")
                     .eq("doc_tipo", doc_tipo).eq("doc_nro", doc_nro)
                     .limit(1).execute())
        if existente.data:
            (supabase.table("clientes").update({"email": email})
             .eq("id", existente.data[0]["id"]).execute())
        else:
            supabase.table("clientes").insert({
                "nombre": fmt_doc(doc_tipo, doc_nro),   # placeholder editable
                "doc_tipo": doc_tipo,
                "doc_nro": doc_nro,
                "condicion_iva_receptor": cond_iva,
                "email": email,
            }).execute()
        return True
    except Exception as e:
        logger.error("No pude recordar el email del cliente: %s", e)
        return False


# ---------------------------------------------------------------------------
# Consultas / reportes
# ---------------------------------------------------------------------------
def rango_periodo(args: list[str]) -> tuple[date, date] | None:
    """Interpreta el periodo de /resumen y /csv.

    Sin args: mes actual. '6' -> junio de este anio. '06/2026' -> ese mes.
    '2026' -> anio entero. '01/06-30/06' -> rango. None si no se entiende.
    """
    hoy = hoy_ar()

    def fin_de_mes(anio: int, mes: int) -> date:
        return (date(anio, mes + 1, 1) if mes < 12 else date(anio + 1, 1, 1)) - timedelta(days=1)

    if not args:
        return hoy.replace(day=1), hoy
    a = args[0]
    periodo = parsear_periodo(a)
    if periodo:
        return periodo
    if re.fullmatch(r"\d{4}", a):
        return date(int(a), 1, 1), date(int(a), 12, 31)
    if re.fullmatch(r"\d{1,2}", a) and 1 <= int(a) <= 12:
        return date(hoy.year, int(a), 1), fin_de_mes(hoy.year, int(a))
    if re.fullmatch(r"\d{1,2}/\d{4}", a):
        mes, anio = map(int, a.split("/"))
        if 1 <= mes <= 12:
            return date(anio, mes, 1), fin_de_mes(anio, mes)
    return None


def filas_periodo(desde: date, hasta: date) -> list[dict]:
    """Comprobantes del PV actual en el rango, ordenados. Lanza si falla."""
    if supabase is None:
        raise RuntimeError("Supabase no configurado")
    res = (
        supabase.table("facturas_emitidas").select("*")
        .eq("pto_vta", PUNTO_DE_VENTA)
        .gte("fecha_cbte", desde.isoformat())
        .lte("fecha_cbte", hasta.isoformat())
        .order("fecha_cbte").order("cbte_nro")
        .execute()
    )
    return res.data


def texto_resumen(desde: date, hasta: date) -> str:
    filas = filas_periodo(desde, hasta)
    encabezado = f"📊 {desde.strftime('%d/%m/%Y')} – {hasta.strftime('%d/%m/%Y')}"
    if not filas:
        return f"{encabezado}\n\nSin comprobantes en el período."

    lineas, total, n_fc, n_nc = [], 0.0, 0, 0
    for f in filas:
        monto = float(f["imp_total"])
        fecha_f = date.fromisoformat(f["fecha_cbte"]).strftime("%d/%m")
        receptor = (fmt_doc(f["doc_tipo"], f["doc_nro"])
                    if f["doc_tipo"] != DOC_TIPO_CF else "CF")
        if f["cbte_tipo"] == NOTA_CREDITO_C:
            n_nc += 1
            total -= monto
            asoc = f" (x F.{f['asociado_cbte_nro']})" if f.get("asociado_cbte_nro") else ""
            lineas.append(f"NC {f['cbte_nro']} · {fecha_f} · −${fmt_ars(monto)}{asoc}")
        else:
            n_fc += 1
            total += monto
            lineas.append(f"N° {f['cbte_nro']} · {fecha_f} · ${fmt_ars(monto)} · {receptor}")

    if len(lineas) > 50:
        lineas = lineas[:50] + [f"... y {len(filas) - 50} más"]
    detalle_nc = f", {n_nc} NC" if n_nc else ""
    return (
        f"{encabezado}\n\n" + "\n".join(lineas) +
        f"\n\n{n_fc} facturas{detalle_nc}\n"
        f"Total del período: ${fmt_ars(total)}"
    )


def csv_periodo(desde: date, hasta: date) -> bytes | None:
    """CSV para el contador. None si no hay comprobantes en el rango.

    Separador ';' y coma decimal: lo que espera Excel configurado en espaniol.
    utf-8-sig (BOM) para que abra las tildes bien de una. NC en negativo.
    """
    filas = filas_periodo(desde, hasta)
    if not filas:
        return None

    buffer = io.StringIO()
    w = csv.writer(buffer, delimiter=";")
    w.writerow(["tipo", "punto_venta", "numero", "fecha", "doc_tipo", "doc_nro",
                "condicion_iva_receptor", "periodo_desde", "periodo_hasta",
                "importe", "cae", "vto_cae", "factura_asociada"])
    for f in filas:
        es_nc = f["cbte_tipo"] == NOTA_CREDITO_C
        importe = float(f["imp_total"]) * (-1 if es_nc else 1)
        w.writerow([
            "NC C" if es_nc else "Factura C",
            f["pto_vta"], f["cbte_nro"], f["fecha_cbte"],
            f["doc_tipo"], f["doc_nro"], f["condicion_iva_receptor"],
            f.get("fch_serv_desde") or "", f.get("fch_serv_hasta") or "",
            f"{importe:.2f}".replace(".", ","),
            f["cae"], f["cae_vto"], f.get("asociado_cbte_nro") or "",
        ])
    return buffer.getvalue().encode("utf-8-sig")


def total_facturado_12m() -> float:
    """Facturado neto (facturas - NC) de los ultimos 12 meses, como mira ARCA."""
    filas = filas_periodo(hoy_ar() - timedelta(days=365), hoy_ar())
    return sum(
        float(f["imp_total"]) * (-1 if f["cbte_tipo"] == NOTA_CREDITO_C else 1)
        for f in filas
    )


def aviso_umbral(monto: float, doc_tipo: int) -> str | None:
    """Advertencia si el monto exige identificar al receptor (RG 5700/2025)."""
    if UMBRAL_CF is None or doc_tipo != DOC_TIPO_CF or monto < UMBRAL_CF:
        return None
    return (
        f"🛑 OJO: ${fmt_ars(monto)} está en o sobre el umbral de "
        f"${fmt_ars(UMBRAL_CF)} — a consumidor final ANÓNIMO no se puede: "
        f"tenés que identificar al receptor (CUIT/DNI)."
    )


def aviso_tope() -> str | None:
    """Advertencia si el facturado 12m se acerca al tope de la categoria."""
    if MONOTRIBUTO_TOPE is None:
        return None
    try:
        total = total_facturado_12m()
    except Exception as e:
        logger.error("No pude calcular el facturado 12m: %s", e)
        return None
    pct = total / MONOTRIBUTO_TOPE * 100
    if pct >= 100:
        return (f"🚨 TOPE SUPERADO: llevás ${fmt_ars(total)} en 12 meses "
                f"({pct:.0f}% de tu categoría). Hablá con tu contador YA.")
    if pct >= 80:
        return (f"⚠️ Ojo al tope: llevás ${fmt_ars(total)} en 12 meses "
                f"({pct:.0f}% de tu categoría de monotributo).")
    return None


# ---------------------------------------------------------------------------
# PDF (con QR obligatorio de ARCA, RG 4892/2020)
# ---------------------------------------------------------------------------
def url_qr_arca(res: dict, ud: dict) -> str:
    """URL oficial del QR: https://www.afip.gob.ar/fe/qr/?p=<base64(json)>."""
    datos = {
        "ver": 1,
        "fecha": _fecha_int_a_iso(res["fecha_int"]),
        "cuit": CUIT,
        "ptoVta": PUNTO_DE_VENTA,
        "tipoCmp": res.get("cbte_tipo", FACTURA_C),
        "nroCmp": res["numero"],
        "importe": round(ud["monto"], 2),
        "moneda": "PES",
        "ctz": 1,
        "tipoDocRec": ud["doc_tipo"],
        "nroDocRec": ud["doc_nro"],
        "tipoCodAut": "E",              # E = CAE
        "codAut": int(res["CAE"]),
    }
    payload = base64.b64encode(json.dumps(datos).encode()).decode()
    return f"https://www.afip.gob.ar/fe/qr/?p={payload}"


def _html_factura(res: dict, ud: dict) -> str:
    def _fmt(fecha_int: int) -> str:
        return datetime.strptime(str(fecha_int), "%Y%m%d").strftime("%d/%m/%Y")

    fecha = _fmt(res["fecha_int"])
    if res.get("serv_desde_int"):
        bloque_periodo = (
            f'<div class="bloque"><div class="fila">'
            f'<b>Período Facturado Desde:</b> {_fmt(res["serv_desde_int"])}'
            f'&nbsp;&nbsp;<b>Hasta:</b> {_fmt(res["serv_hasta_int"])}'
            f'&nbsp;&nbsp;<b>Fecha de Vto. para el pago:</b> {fecha}</div></div>'
        )
    else:
        bloque_periodo = ""   # productos (concepto 1): sin periodo de servicio
    cae_vto = datetime.strptime(res["CAEFchVto"], "%Y-%m-%d").strftime("%d/%m/%Y")
    qr_png = segno.make(url_qr_arca(res, ud), error="m").png_data_uri(scale=4)

    titulo, codigo = TIPOS_CBTE.get(res.get("cbte_tipo", FACTURA_C), ("FACTURA", "COD. 011"))
    linea_asociado = ""
    if res.get("asociado_nro"):
        linea_asociado = (
            f'<div class="bloque"><div class="fila"><b>Comprobante Asociado:</b> '
            f'Factura C {PUNTO_DE_VENTA:05d}-{res["asociado_nro"]:08d}</div></div>'
        )

    if ud.get("doc_tipo") in (DOC_TIPO_CUIT, DOC_TIPO_DNI):
        receptor_doc = fmt_doc(ud["doc_tipo"], ud["doc_nro"])
        receptor_cond = CONDICIONES_IVA.get(ud.get("cond_iva"), "-")
    else:
        receptor_doc = "-"
        receptor_cond = "Consumidor Final"

    total = fmt_ars(ud["monto"])
    descripcion = ud.get("descripcion") or FACTURA_DESCRIPCION

    bloque_logo = ""
    if LOGO_PATH:
        try:
            with open(LOGO_PATH, "rb") as f:
                logo_b64 = base64.b64encode(f.read()).decode()
            mime = "png" if LOGO_PATH.lower().endswith(".png") else "jpeg"
            bloque_logo = f'<img class="logo" src="data:image/{mime};base64,{logo_b64}" alt="">'
        except OSError:
            logger.warning("No pude leer el logo (%s): el PDF sale sin logo.", LOGO_PATH)

    return f"""
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{ font-family: Helvetica, Arial, sans-serif; font-size: 11px; color: #111; }}
  .marco {{ border: 1px solid #111; }}
  .original {{ text-align: center; font-weight: bold; font-size: 13px;
               padding: 4px; border-bottom: 1px solid #111; }}
  .cabecera {{ display: flex; position: relative; border-bottom: 1px solid #111; }}
  .col {{ width: 50%; padding: 14px; }}
  .col-izq {{ border-right: 1px solid #111; }}
  .col-der {{ padding-left: 44px; }}
  .letra {{ position: absolute; left: 50%; top: 0; transform: translateX(-50%);
            border: 1px solid #111; border-top: 0; background: #fff;
            width: 44px; text-align: center; padding: 4px 0 2px; }}
  .letra .c {{ font-size: 26px; font-weight: bold; line-height: 1; }}
  .letra .cod {{ font-size: 8px; }}
  h1 {{ font-size: 16px; margin-bottom: 8px; }}
  .logo {{ display: block; max-height: 56px; max-width: 200px; margin-bottom: 8px; }}
  .fila {{ margin: 3px 0; }}
  .bloque {{ padding: 10px 14px; border-bottom: 1px solid #111; }}
  table {{ width: 100%; border-collapse: collapse; }}
  th {{ background: #eee; text-align: left; padding: 5px; font-size: 10px; }}
  td {{ padding: 5px; }}
  .der {{ text-align: right; }}
  .total {{ font-size: 14px; font-weight: bold; }}
  .pie {{ display: flex; padding: 12px 14px; align-items: center; gap: 18px; }}
  .cae {{ margin-left: auto; text-align: right; }}
</style>
<div class="marco">
  <div class="original">ORIGINAL</div>
  <div class="cabecera">
    <div class="letra"><div class="c">C</div><div class="cod">{codigo}</div></div>
    <div class="col col-izq">
      {bloque_logo}
      <h1>{EMISOR_NOMBRE}</h1>
      <div class="fila"><b>Razón Social:</b> {EMISOR_NOMBRE}</div>
      <div class="fila"><b>Domicilio Comercial:</b> {EMISOR_DOMICILIO}</div>
      <div class="fila"><b>Condición frente al IVA:</b> Responsable Monotributo</div>
    </div>
    <div class="col col-der">
      <h1>{titulo}</h1>
      <div class="fila"><b>Punto de Venta:</b> {PUNTO_DE_VENTA:05d}
        &nbsp;&nbsp;<b>Comp. Nro:</b> {res['numero']:08d}</div>
      <div class="fila"><b>Fecha de Emisión:</b> {fecha}</div>
      <div class="fila"><b>CUIT:</b> {CUIT}</div>
      <div class="fila"><b>Inicio de Actividades:</b> {EMISOR_INICIO_ACTIVIDADES}</div>
    </div>
  </div>
  {bloque_periodo}
  <div class="bloque">
    <div class="fila"><b>CUIT / DNI:</b> {receptor_doc}
      &nbsp;&nbsp;<b>Condición frente al IVA:</b> {receptor_cond}</div>
  </div>
  {linea_asociado}
  <div class="bloque">
    <table>
      <tr><th>Descripción</th><th class="der">Subtotal</th></tr>
      <tr><td>{descripcion}</td><td class="der">$ {total}</td></tr>
    </table>
  </div>
  <div class="bloque der">
    <span class="total">Importe Total: $ {total}</span>
  </div>
  <div class="pie">
    <img src="{qr_png}" width="110" height="110" alt="QR ARCA">
    <div style="font-size:9px">Comprobante Autorizado por ARCA</div>
    <div class="cae">
      <div class="fila"><b>CAE N°:</b> {res['CAE']}</div>
      <div class="fila"><b>Fecha de Vto. de CAE:</b> {cae_vto}</div>
    </div>
  </div>
</div>
"""


def nombre_pdf(res: dict) -> str:
    prefijo = "NC-C" if res.get("cbte_tipo") == NOTA_CREDITO_C else "Factura-C"
    return f"{prefijo}-{PUNTO_DE_VENTA:05d}-{res['numero']:08d}"


def generar_pdf(res: dict, ud: dict) -> str:
    """Genera el PDF via el SDK y devuelve la URL de descarga."""
    respuesta = get_afip().ElectronicBilling.createPDF({
        "html": _html_factura(res, ud),
        "file_name": nombre_pdf(res),
        "options": {"width": 8, "marginLeft": 0.4, "marginRight": 0.4,
                    "marginTop": 0.4, "marginBottom": 0.4},
    })
    return respuesta["file"]
