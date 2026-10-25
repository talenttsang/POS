-- Drop SkuVault: 0015
--
-- The register no longer talks to SkuVault. Product photos were read from the
-- SkuVault-synced public.products table; they are now a link on each product,
-- set by the product upload (image_url column) or the register's edit sheet.
-- The inventory-sync queue and the venue's SkuVault warehouse and bin go.

alter table pos_products add column if not exists image_url text;
comment on column pos_products.image_url is 'Link to the product photo shown on the register; null shows a placeholder.';

drop table if exists pos_inventory_moves;
alter table pos_events drop column if exists sv_warehouse, drop column if exists sv_location;
alter table pos_product_edits drop column if exists skuvault;
comment on table pos_product_edits is 'One row per product saved from the register: what changed, by whom.';
