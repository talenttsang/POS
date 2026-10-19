-- Line discounts: 0011
--
-- A cart line can carry its own discount (a dollar amount or a percentage,
-- recorded as the dollar amount taken off that line). line_total becomes the
-- line's net: unit_price x quantity - line_discount. The whole-sale discount
-- (discount_total) still applies on top, and subtotal is the sum of net lines.

alter table pos_transaction_items
  add column if not exists line_discount numeric(12,2) not null default 0 check (line_discount >= 0);

alter table pos_transaction_items drop column if exists line_total;
alter table pos_transaction_items
  add column line_total numeric(12,2) generated always as (round(unit_price * quantity - line_discount, 2)) stored;

comment on column pos_transaction_items.line_discount is 'Dollar discount taken off this line before the whole-sale discount.';
comment on column pos_transaction_items.line_total is 'Net for the line: unit_price x quantity - line_discount.';
