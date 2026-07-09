"""
Smoke test de afip.py (Afip SDK) — MODO DEV / HOMOLOGACION.

Estado: VALIDADO corriendo. Emite Factura C (Servicios, Concepto 2) contra
homologacion usando el CUIT de testing del SDK y devuelve CAE.

⚠️ NO valida tu cert, tu punto de venta ni tu CUIT reales. Solo la plomeria.
   (Para eso: homologacion con tu cert via WSASS, o produccion controlada.)

Sirve como banco de pruebas de AFIP independiente del bot de Telegram,
util para debuggear la emision sin la capa de Telegram encima.

Requisitos:
    pip install afip.py
Env:
    AFIP_ACCESS_TOKEN  (el CUIT de testing requiere token; confirmado corriendo)
"""

import os
from datetime import datetime
from afip import Afip

CUIT_TESTING = 20409378472
ACCESS_TOKEN = os.environ.get("AFIP_ACCESS_TOKEN")  # requerido

opciones = {"CUIT": CUIT_TESTING, "production": False}
if ACCESS_TOKEN:
    opciones["access_token"] = ACCESS_TOKEN

afip = Afip(opciones)

PUNTO_DE_VENTA = 1
FACTURA_C = 11


def etapa_1_conectividad():
    print("\n[1] Conectividad (FEDummy)...")
    print("    OK:", afip.ElectronicBilling.getServerStatus())


def etapa_2_verificar_codigos():
    print("\n[2] Verificando codigo 11 contra ARCA...")
    for t in afip.ElectronicBilling.getVoucherTypes():
        if str(t.get("Id")) == "11":
            print("    Codigo 11 =>", t.get("Desc"))


def etapa_3_emitir():
    print("\n[3] Emitiendo Factura C (Servicios)...")
    ultimo = afip.ElectronicBilling.getLastVoucher(PUNTO_DE_VENTA, FACTURA_C)
    numero = ultimo + 1
    hoy = int(datetime.now().strftime("%Y%m%d"))

    data = {
        "CantReg": 1,
        "PtoVta": PUNTO_DE_VENTA,
        "CbteTipo": FACTURA_C,
        "Concepto": 2,                 # Servicios
        "DocTipo": 99,                 # Consumidor Final
        "DocNro": 0,
        "CbteDesde": numero,
        "CbteHasta": numero,
        "CbteFch": hoy,
        # Obligatorios para Concepto 2/3. En uso real = periodo facturado.
        "FchServDesde": hoy,
        "FchServHasta": hoy,
        "FchVtoPago": hoy,
        # Factura C: neto = total, sin IVA (confirmado en doc de Factura C).
        "ImpTotal": 1000,
        "ImpTotConc": 0,
        "ImpNeto": 1000,
        "ImpOpEx": 0,
        "ImpIVA": 0,
        "ImpTrib": 0,
        "MonId": "PES",
        "MonCotiz": 1,
        "CondicionIVAReceptorId": 5,   # Consumidor Final
    }

    res = afip.ElectronicBilling.createVoucher(data)
    print("    ✅ Numero:", numero)
    print("    ✅ CAE:", res["CAE"])
    print("    ✅ Vto CAE:", res["CAEFchVto"])


if __name__ == "__main__":
    etapa_1_conectividad()
    etapa_2_verificar_codigos()
    etapa_3_emitir()
    print("\nListo.")
