-- Inventory sync with SkuVault: 0007
--
-- A venue can name the SkuVault warehouse and location that holds its stock.
-- When it does, the register shows on-hand quantities from that location,
-- each sale removes the sold units there, and a full refund adds them back.
-- Every removal or add-back is a row in pos_inventory_moves so a failed call
-- can be retried and audited.

alter table pos_events
  add column if not exists sv_warehouse text,
  add column if not exists sv_location  text;

comment on column pos_events.sv_warehouse is 'SkuVault warehouse code (e.g. IT1) holding this venue''s stock; null = no inventory sync.';
comment on column pos_events.sv_location  is 'SkuVault location code (e.g. PKP) within sv_warehouse.';

create table if not exists pos_inventory_moves (
  id              bigint generated always as identity primary key,
  transaction_id  uuid not null references pos_transactions (id) on delete cascade,
  line_no         smallint not null,
  sku             text not null,
  quantity        integer not null check (quantity > 0),
  direction       text not null check (direction in ('out', 'in')),   -- out = sold, in = refunded
  warehouse       text not null,
  location        text not null,
  status          text not null default 'pending' check (status in ('pending', 'ok', 'failed')),
  attempts        integer not null default 0,
  last_error      text,
  response        jsonb,
  created_at      timestamptz not null default now(),
  updated_at      timestamptz not null default now(),
  done_at         timestamptz,
  unique (transaction_id, line_no, direction)
);

create index if not exists pos_inventory_moves_status_idx on pos_inventory_moves (status, created_at) where status <> 'ok';

alter table pos_inventory_moves enable row level security;

comment on table pos_inventory_moves is
  'One row per sale line sent to SkuVault (out) or added back on refund (in). Retried while status is pending or failed.';

-- The Markham store sells from location PKP in the internal-transfer warehouse.
update pos_events set sv_warehouse = 'IT1', sv_location = 'PKP'
where code = 'markham-steeles' and sv_location is null;
