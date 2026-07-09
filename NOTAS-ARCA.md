# Notas de campo: el web service de ARCA (WSFE)

Todo lo de acá abajo está **verificado contra el web service real** (no
copiado de documentación) durante el desarrollo del bot, jul-2026. Son las
trampas que me costaron debugging y que ninguna doc oficial te cuenta bien.

## 1. Las fechas van como entero, y son obligatorias

Formato `aaaammdd` como int (ej: `20260706`). Mandar `CbteFch=None` esperando
que ARCA ponga "hoy" **no funciona**: da error `10016`. Fecha explícita siempre.

Bonus: si tu server corre en UTC (cualquier hosting), entre las 21:00 y las
00:00 de Argentina "hoy" es mañana. Usá `ZoneInfo("America/Argentina/Buenos_Aires")`.

## 2. La numeración es cronológica POR PUNTO DE VENTA

La fecha de un comprobante no puede ser anterior a la del último emitido
**en ese punto de venta y tipo** (error `10016` también). Consecuencias:

- Fecha retroactiva funciona (hasta 10 días para servicios, 5 para productos)
  **solo si** no emitiste nada más nuevo en ese PV.
- Regla práctica: emití siempre de la fecha más vieja a la más nueva.
- "Pero Comprobantes en Línea me deja retroceder" — porque usa **otro** punto
  de venta, con su propia secuencia. No es magia, es otro contador.

## 3. Códigos de receptor (confirmados contra el WS, no contra la doc)

`FEParamGetCondicionIvaReceptor`, válidos para comprobantes clase C:

| Id | Condición |
|----|-----------|
| 1  | IVA Responsable Inscripto |
| 4  | IVA Sujeto Exento |
| 5  | Consumidor Final |
| 6  | Responsable Monotributo |
| 7  | Sujeto No Categorizado |
| 8  | Proveedor del Exterior |
| 9  | Cliente del Exterior |
| 10 | IVA Liberado – Ley 19.640 |
| 13 | Monotributista Social |
| 15 | IVA No Alcanzado |
| 16 | Monotributo Trabajador Independiente Promovido |

Tipos de documento (`getDocumentTypes`): **80** = CUIT, **86** = CUIL,
**96** = DNI, **99** = Consumidor Final sin identificar (con `DocNro 0`).

Consultalos vos mismo: `afip.ElectronicBilling.executeRequest("FEParamGetCondicionIvaReceptor")`.

## 4. La "Condición de Venta" (Contado/Transferencia/etc.) NO existe en el WSFE

Ese campo del formulario web de Comprobantes en Línea **no viaja** en
`FECAESolicitar`. Es puramente informativo del PDF impreso. Si armás tu
propio PDF, imprimí el valor que quieras; ARCA no lo conoce ni lo valida.

## 5. Notas de Crédito: comprobante asociado obligatorio y numeración propia

- Una NC C (tipo 13) debe referenciar la factura original vía `CbtesAsoc`
  (RG 4540/2019): `[{"Tipo": 11, "PtoVta": X, "Nro": N, "Cuit": "<tu cuit>"}]`.
- Cada tipo de comprobante tiene **su propia secuencia**: tu NC N° 1 puede
  convivir con tu Factura N° 500 en el mismo PV.
- Las facturas no se "borran" ni editan: un error se corrige emitiendo una
  NC (total o parcial).

## 6. Manejo de PDFs 

-Los links de createPDF vencen en ~1 día — tratalos como cache, no como storage
-Regenerar los PDFs (si no los enviás en el momento) consume un PDF extra de la cuota del SDK (100/mes gratis al menos a Julio 2026). Si algún día quisieras eliminarlo de raíz, la alternativa sería guardar el PDF binario en Supabase Storage (1GB gratis) en vez del link.

---

*Generadas durante el desarrollo de este bot, con emisiones reales contra
homologación y producción. Si algo cambió (ARCA cambia), abrí un issue.*
