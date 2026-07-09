# Fase 0 — Setup fiscal en ARCA para facturar por Web Service

> **Objetivo:** dejar tu CUIT habilitado para emitir Factura C (monotributo) vía web service (WSFE), con certificado propio y **sin darle tu clave fiscal a ningún tercero**.

---

## ⚠️ Leé esto antes de arrancar

- **El portal cambia seguido.** Los nombres de servicios y menús de abajo **pueden estar distintos** en tu pantalla. Si un nombre no coincide exacto, buscá el equivalente. Verificá siempre contra lo que ves.
- Marco con **✅** lo confirmado en doc oficial de ARCA, y con **⚠️** lo que doy desde conocimiento general / recuerdo y **tenés que verificar**.
- Es un trámite de **una sola vez**. Guardá todo (key, CSR, cert) en un lugar seguro (carpeta distinta del repo)
- **Orden:** cert → asociar al web service → punto de venta. Si lo hacés en otro orden te podés trabar.
- Si no querés hacer toda esta fase, el SDK que vamos a usar luego te provee de automatizaciones para sacar estos certificados pero te solicita tu **Clave Fiscal**. Como me parece demasiado brindar ese dato a un tercero, lo hacemos a mano y nos mantenemos seguros. 

---

## Paso 1 — Clave fiscal con nivel suficiente ⚠️

- Para usar el "Administrador de Certificados Digitales" necesitás clave fiscal con **nivel 3**
- Si tenés nivel 2, tenés que subir de nivel. El método (app Mi ARCA / biometría / dependencia) **puede haber cambiado**; confirmá el vigente.

## Paso 2 — Generar tu private key y tu CSR con OpenSSL ✅

En tu máquina o contenedor (OpenSSL ya viene en Linux/Mac).

**¿Windows?** No trae OpenSSL, pero los comandos son exactamente los mismos;
solo necesitás una terminal que lo tenga:
1. **Git Bash** (si tenés Git instalado, ya lo tenés — la opción más común).
2. **WSL** (Ubuntu en Windows).
3. `winget install ShiningLight.OpenSSL.Light` en PowerShell.

```bash
# 1) Private key de 2048 bits — ESTO NO SE COMPARTE NUNCA
openssl genrsa -out privada.key 2048

# 2) CSR (pedido de certificado)
openssl req -new -key privada.key \
  -subj "/C=AR/O=TU_NOMBRE/CN=ALIAS/serialNumber=CUIT TUCUIT" \
  -out pedido.csr
```

- ✅ Este es el flujo oficial de ARCA para generar la key y el CSR.
- `O=` → ⚠️ para monotributista/persona física suele ir tu **nombre completo tal como figura en ARCA**. Verificá el requisito exacto (a veces rebota si no coincide).
- `CN=` → un alias para reconocer el cert (ej. `proyecto-facturacion`).
- `TUCUIT` → tu CUIT **sin guiones**.
- **Hacé backup de `privada.key`.** Sin esa key, el `.crt` no sirve para nada.
- ⚠️ Si tu OpenSSL es nuevo y algún paso posterior se queja del formato de la key, puede que necesites regenerarla con `-traditional`. Solo si aparece el problema; no siempre hace falta.

## Paso 3 — Crear el certificado en "Administrador de Certificados Digitales" ✅ / ⚠️

- ✅ El certificado de **producción** se tramita en la app web "Administrador de Certificados Digitales" del portal, con clave fiscal.
- Adentro: **Agregar alias** → poné el mismo nombre del `CN` → **Examinar** y subí tu `pedido.csr` → **Agregar Alias**.
- Descargá el `.crt` que te genera. Ese es tu certificado. Guardalo junto a `privada.key`.
- ⚠️ El detalle de botones lo doy de lo que me acuerdo cuando lo hice; el flujo general es ese, pero confirmá en pantalla.

## Paso 4 — Asociar el certificado al web service de Facturación Electrónica ✅ (que hay que hacerlo) / ⚠️ (la UI)

- ✅ ARCA aclara que, una vez obtenido el certificado, **hay que asociarlo al web service de negocio** que vas a usar. Sin esto, el cert existe pero no factura.
- El web service de factura electrónica se referencia como **WSFE** (o "Facturación Electrónica").
- Se hace en "Administrador de Relaciones con Clave Fiscal": **Nueva Relación** → Servicio → ARCA → WebServices →buscás Facturación Electrónica → y como representante ponés tu certificado (alias/DN).

## Paso 5 — Crear un punto de venta tipo Web Service ⚠️ 

- Necesitás un **punto de venta específico para web services**, distinto del de "Comprobantes en Línea"/"Facturador". Si los mezclás, te da error de numeración de comprobante.
- Se hace en "Administración de Puntos de Venta y Domicilios" (o el nombre actual) → Alta → tipo/sistema = **Web Service**.
- Agregar → Completás los datos (fijate que el número no sea el mismo que otros puntos de venta) y listo. El sistema es **Factura electrónica - Mnotributo - Web Services**

## Paso 6 — (Solo si usás Afip SDK) Sacar tu access_token ⚠️

- En app.afipsdk.com creás cuenta y sacás el `access_token`. El plan Free (1 CUIT, 1k requests) te sobra.
- Para producción, en el SDK pasás `cert`, `key` y `production=true`.
- **NO uses la automatización de certificado del SDK** — te va a pedir tu clave fiscal (ver decisión de en el README).

## Paso 7 — Probar en homologación ANTES de producción ✅

- Para testing, ARCA da certificados de homologación por la app **WSASS** (con clave fiscal). O usás el CUIT de testing del SDK (`20409378472`) sin cert.
- Emití una Factura C de prueba, confirmá que sale el **CAE**, y recién ahí pasás a producción con tu cert real.

---

## Al final tenés que tener

- [ ] `privada.key` (guardada, nunca compartida)
- [ ] `pedido.csr`
- [ ] `certificado.crt` (bajado de ARCA)
- [ ] Certificado asociado al web service WSFE
- [ ] Punto de venta tipo Web Service creado
- [ ] `access_token` de Afip SDK
- [ ] Prueba OK en homologación (CAE obtenido)

---

## Dónde tengo menos certeza — verificá sí o sí

1. El **nivel de clave fiscal** exacto (creo nivel 3, no confirmado).
2. Los **nombres actuales de los servicios** en el portal ARCA siempre pueden cambiar.

Ninguno de estos pasos es "plata perdida" si algo cambió: es la secuencia lógica del trámite, pero ARCA mueve la UI seguido, así que tratá cada nombre de menú como orientativo, no como verdad absoluta.
