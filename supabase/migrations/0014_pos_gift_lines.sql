-- Gift lines: 0014
--
-- A cashier can mark a line as a gift. It is sold at $0 (line_discount equals
-- the full line, so line_total is 0) and still takes the units out of stock;
-- is_gift says why the line is free, for receipts and reporting.
alter table pos_transaction_items
  add column if not exists is_gift boolean not null default false;
comment on column pos_transaction_items.is_gift is 'Given away at $0: line_discount is the whole line. Stock still moves.';
