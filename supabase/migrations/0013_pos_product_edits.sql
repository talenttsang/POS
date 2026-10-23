-- Product edits from the register: 0013
--
-- Admins can edit a product from the register. The barcode and the SkuVault
-- description are written to SkuVault, the company's catalogue for every
-- channel; name, price and category stay register-only. Each save is logged
-- here with what it changed, by whom, so a barcode change can be traced.
create table if not exists pos_product_edits (
  id          bigint generated always as identity primary key,
  product_id  uuid references pos_products (id) on delete set null,
  sku         text not null,
  changes     jsonb not null,             -- {"field": {"from": ..., "to": ...}, ...}
  skuvault    jsonb,                      -- what was sent to SkuVault and its reply, if anything
  edited_by   text not null,
  edited_at   timestamptz not null default now()
);
create index if not exists pos_product_edits_sku_idx on pos_product_edits (sku, edited_at desc);
alter table pos_product_edits enable row level security;
comment on table pos_product_edits is 'One row per product saved from the register; barcode and description changes also went to SkuVault.';
