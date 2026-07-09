-- ============================================================
-- Schema para el bot de facturacion (Factura C / monotributo)
-- ============================================================
-- Diseno: consumidor final es el DEFAULT IMPLICITO.
--   -> Una factura a consumidor final se guarda con cliente_id = NULL.
--   -> La tabla "clientes" guarda solo los receptores identificados que
--      valen la pena recordar; el bot la alimenta solo (aprende el email
--      de cada receptor la primera vez que usas /mail).
--
-- Correr este archivo COMPLETO en el SQL Editor de Supabase. Es idempotente:
-- crea las tablas si no existen y aplica las migraciones al final. Si tu
-- proyecto de Supabase ya tiene tablas con estos nombres, revisa antes.
-- ============================================================

-- ------------------------------------------------------------
-- Tabla: clientes  (receptores recordados; el bot la alimenta solo)
-- ------------------------------------------------------------
create table if not exists clientes (
  id                     bigint generated always as identity primary key,
  nombre                 text not null,

  -- Combo del receptor (se mueven juntos). Codigos confirmados contra el
  -- web service (ver NOTAS-ARCA.md):
  --   doc_tipo: 99 = Consumidor Final, 80 = CUIT, 96 = DNI, 86 = CUIL
  --   condicion_iva_receptor: 1 = Resp. Inscripto, 4 = Exento,
  --                           5 = Consumidor Final, 6 = Monotributo, ...
  doc_tipo               int not null,
  doc_nro                bigint not null default 0,
  condicion_iva_receptor int not null,

  notas                  text,
  created_at             timestamptz not null default now()
);

-- ------------------------------------------------------------
-- Tabla: facturas_emitidas  (log / red de seguridad)
-- ------------------------------------------------------------
-- El registro local de todo lo emitido: alimenta /resumen, /csv, /tope,
-- /pdf, /nc y los receptores recientes. El unique de abajo evita registrar
-- dos veces el mismo comprobante.
create table if not exists facturas_emitidas (
  id              bigint generated always as identity primary key,

  cliente_id      bigint references clientes(id),  -- NULL = consumidor final

  -- Snapshot del receptor TAL COMO FUE A ARCA. La factura es un documento
  -- inmutable: no puede depender de una fila de "clientes" editable.
  -- Defaults = consumidor final anonimo (doc_tipo 99, doc_nro 0, cond 5),
  -- que es el caso de la gran mayoria de las facturas.
  doc_tipo               int not null default 99,
  doc_nro                bigint not null default 0,
  condicion_iva_receptor int not null default 5,

  -- Datos del comprobante
  pto_vta         int not null,
  cbte_tipo       int not null,          -- 11 = Factura C
  cbte_nro        int not null,
  concepto        int not null,          -- 1 Productos / 2 Servicios / 3 ambos

  -- Importe (monotributo: sin discriminar IVA)
  imp_total       numeric(14,2) not null,

  -- Periodo de servicio (solo para concepto 2/3; NULL en concepto 1)
  fch_serv_desde  date,
  fch_serv_hasta  date,

  -- Resultado de ARCA
  cae             text not null,
  cae_vto         date not null,
  fecha_cbte      date not null,

  pdf_url         text,                  -- cache: los links del SDK vencen (~1 dia);
                                         -- si murio, se regenera desde esta fila
  created_at      timestamptz not null default now(),

  -- Evita duplicar el mismo numero en el mismo punto de venta/tipo
  unique (pto_vta, cbte_tipo, cbte_nro)
);

create index if not exists idx_facturas_created_at
  on facturas_emitidas (created_at desc);

-- ------------------------------------------------------------
-- RLS
-- ------------------------------------------------------------
alter table clientes            enable row level security;
alter table facturas_emitidas   enable row level security;

-- SIN policies a proposito: RLS activado sin policies = nadie entra por la
-- API publica (deny-all). El bot usa la key secret (server-side), que
-- saltea RLS. Si algun dia lees estas tablas desde un frontend, agrega
-- las policies que correspondan.

-- ------------------------------------------------------------
-- Migracion: snapshot del receptor (idempotente)
-- ------------------------------------------------------------
-- Si las tablas YA existen (el create if not exists de arriba no las toca),
-- este bloque agrega las columnas nuevas. Correr todo el archivo es seguro.
alter table facturas_emitidas
  add column if not exists doc_tipo               int not null default 99,
  add column if not exists doc_nro                bigint not null default 0,
  add column if not exists condicion_iva_receptor int not null default 5;

-- Notas de credito (cbte_tipo 13): numero de la factura asociada que anulan.
-- NULL en facturas comunes.
alter table facturas_emitidas
  add column if not exists asociado_cbte_nro int;

-- Email del cliente, para /mail. El bot lo aprende solo: la primera vez que
-- mandas /mail con email explicito a un receptor identificado, lo guarda aca
-- y la proxima vez /mail <nro> alcanza.
alter table clientes
  add column if not exists email text;
