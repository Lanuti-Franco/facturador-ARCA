# 🧾 Facturador ARCA por Telegram

Bot de Telegram para que un **monotributista** emita **Factura C** en ARCA
(ex-AFIP) desde el chat, en dos toques:

```
/facturar 15000 20-12345678-6 06/07/26    →   ✅ CAE + PDF con QR en el chat
```

Hecho para uso personal y regalado tal cual, building in public. Corre en tu
máquina, con **tu** certificado y **tus** llaves: acá nadie custodia tus datos
fiscales.

## Qué hace

- **Factura C** a consumidor final (default), o identificada con **CUIT/DNI**
  (valida dígito verificador) y condición de IVA por botones.
- **Atajos de un solo mensaje**: `/facturar 15000`, `/facturar 15000 20-12345678-6`,
  `/facturar 15000 26/06` (fecha retroactiva, hasta 10 días),
  `/facturar 15000 01/06-30/06` (período facturado real).
- **`/lote 15000 20000 12500 01/06-30/06`** — varias facturas de un saque,
  todas a consumidor final, con preview del total antes de confirmar.
- **`/nc 5 [monto]`** — Nota de Crédito C (total o parcial) asociada
  automáticamente a la factura original, como exige ARCA.
- **PDF con el QR obligatorio** (RG 4892/2020) directo al chat; `/pdf 5` lo
  regenera cuando quieras.
- **La factura por email al cliente, sin salir del chat**: cada PDF emitido
  llega con un botón **📧 Mandar por mail**. Si el bot no conoce el email del
  receptor, se lo tirás ahí nomás y sale; a partir de ahí lo recuerda y la
  próxima es un solo toque. Manda desde tu propia casilla (Gmail +
  contraseña de aplicación, sin servicios pagos ni dominio propio). También
  por comando: `/mail 5 cliente@ejemplo.com` para cualquier comprobante viejo.
- **`/resumen 06/2026`** — lista del período con total (las NC restan).
- **`/csv 06/2026`** — export listo para mandarle al contador.
- **`/tope`** + alerta automática cuando tu facturado de 12 meses se acerca
  al límite de tu categoría de monotributo.
- **Resumen automático** el 1° de cada mes.
- **`/ultima`** — reconcilia tu último comprobante en ARCA contra el log
  local y avisa si hay desfase (la red de seguridad si algo se cayó a mitad
  de una emisión).
- Log de todo en Supabase (gratis), receptores recientes a un botón,
  montos en formato argentino o gringo (`15.000,50` y `15,000.50` valen).

## Qué necesitás

1. **Ser monotributista** con clave fiscal nivel 3.
2. **Certificado digital de ARCA + punto de venta tipo Web Service** — es un
   trámite de una sola vez, sin darle tu clave fiscal a nadie:
   está paso a paso en [`fase0-setup-arca.md`](fase0-setup-arca.md).
3. **Un bot de Telegram** — gratis con [@BotFather](https://t.me/BotFather).
   Le hablás, creás un bot y te guardás el TOKEN en un lugar seguro. 
4. **Cuenta en [Afip SDK](https://afipsdk.com)** (plan free alcanza) — es el
   intermediario con los web services de ARCA. ⚠️ Leé la nota de seguridad.
5. **Proyecto en [Supabase](https://supabase.com)** (plan free alcanza) para
   el log de comprobantes.

## Instalación

```bash
git clone https://github.com/Lanuti-Franco/facturador-arca.git
cd facturador-arca
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt

cp .env.example .env       # completá TODAS las variables (están comentadas)
```

Después:

1. Corré `schema_facturacion.sql` completo en el SQL Editor de Supabase
   (es idempotente: crea las tablas y aplica las migraciones).
2. Arrancá el bot: `.venv/bin/python bot_telegram.py`
3. Mandale `/id` para conocer tu chat_id y ponelo en `MI_CHAT_ID` (sin esto
   el bot no arranca: es la allowlist).
4. Probá con `PRODUCTION=false` (usa el entorno de homologación de ARCA con
   un CUIT de testing compartido — las facturas no son reales).
5. Cuando todo cierre: `PRODUCTION=true` en el `.env` y emití una factura
   real chica para validar tu certificado y punto de venta.

¿Querés que corra siempre? En macOS está `com.facturador.plist`
(launchd, instrucciones adentro). En Linux, un systemd unit equivalente.

## Arquitectura

```
bot_telegram.py    ← la conversación (python-telegram-bot, long polling)
facturador.py      ← el core: ARCA + Supabase + PDF. Cero Telegram.
```

El core no sabe qué UI lo llama: si querés WhatsApp, CLI o web, escribís
otra capa de conversación e importás `facturador`. Sin webhooks ni servidor:
long polling, corre desde una laptop.

## Seguridad — leé esto

- **Nada personal vive en el código.** CUIT, punto de venta, certificado,
  tokens: todo en el `.env`, que está en `.gitignore`. Los certificados
  guardalos **fuera** de la carpeta del repo (ej: `~/.arca-facturador/`).
- **Allowlist obligatoria**: el bot solo le responde a tu chat_id.
- **Decisión de diseño que tenés que conocer**: el Afip SDK firma la
  autenticación WSAA **en sus servidores**, o sea que tu `cert` y tu `key`
  viajan a `app.afipsdk.com`. El riesgo está acotado (ese cert solo sirve
  para facturar, no es tu clave fiscal) y a cambio te ahorrás pelear con
  SOAP/OpenSSL. Si querés cero terceros, la alternativa es PyAfipWs con
  firma local — este proyecto eligió velocidad. **Generá el certificado a
  mano** (como explica la fase 0) y jamás uses flujos que pidan tu clave
  fiscal.

## ¿Sos Responsable Inscripto?

Este bot es **solo Factura C** (monotributo) y va a seguir siéndolo — es un
proyecto personal con scope honesto. Dicho eso: si sos RI y querés adaptarlo,
**el 70% del camino está allanado**. El web service es el mismo, la emisión,
el log, el PDF con QR, las NC y los reportes ya funcionan. Lo que te falta
construir:

- Elegir **Factura A o B** según la condición del receptor (hoy es C fija).
- El **array `Iva`**: descomponer neto + IVA por alícuota (21%, 10.5%...) —
  ARCA valida que la suma cierre al centavo.
- Percepciones/retenciones (`ImpTrib`) si tu actividad las tiene.

El core (`facturador.py`) está separado de la UI justamente para esto.
PRs bienvenidos.

## Ideas anotadas (roadmap sin promesas)

- Systemd unit para correrlo en un VPS Linux.
- Recordatorio de recategorización (enero/julio) usando el facturado que ya trackea.
- Dockerfile para el que prefiera `docker compose up`.

## Disclaimer

Esto no es asesoramiento fiscal. Verificá los comprobantes emitidos, los
umbrales de consumidor final anónimo y tu categoría de monotributo con tu
contador. Usalo bajo tu propia responsabilidad.

## Licencia

MIT — usalo, modificalo, regalalo.
