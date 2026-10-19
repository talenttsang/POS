-- Sales tax per venue: 0010
--
-- A venue can carry a tax rate and label (the Markham store: HST 13%). Tax is
-- added on top of the shelf price at checkout: grand_total = subtotal -
-- discount_total + tax_total. Venues with no rate keep selling tax-free, so
-- the trade-show register is unchanged until a rate is set on an event.

alter table pos_events
  add column if not exists tax_rate  numeric(7,5) not null default 0 check (tax_rate >= 0 and tax_rate < 1),
  add column if not exists tax_label text;

comment on column pos_events.tax_rate  is 'Sales tax added at checkout as a fraction, e.g. 0.13000 for HST 13%. 0 = no tax.';
comment on column pos_events.tax_label is 'Printed on receipts and invoices, e.g. HST 13%.';

alter table pos_transactions
  add column if not exists tax_rate  numeric(7,5) not null default 0 check (tax_rate >= 0 and tax_rate < 1),
  add column if not exists tax_label text,
  add column if not exists tax_total numeric(12,2) not null default 0 check (tax_total >= 0);

alter table pos_transactions drop constraint if exists pos_transactions_totals_add_up;
alter table pos_transactions add constraint pos_transactions_totals_add_up
  check (grand_total = round(subtotal - discount_total + tax_total, 2));

comment on column pos_transactions.tax_total is 'Tax charged on top of (subtotal - discount_total), at tax_rate, as the venue was set when the sale was rung.';

drop view if exists pos_daily_cash_summary;
create or replace view pos_daily_cash_summary as
select
  location_id,
  currency,
  (sold_at at time zone 'America/Toronto')::date                     as sale_day,
  count(*)                                                           as sales,
  count(*) filter (where is_inspection)                              as inspection_deposits,
  sum(amount_paid)                                                   as amount_paid,
  sum(tax_total)                                                     as tax_total,
  sum(cash_received)                                                 as cash_received,
  sum(change_given)                                                  as change_given,
  sum(refund_amount) filter (where refund_method = 'cash')           as cash_refunded,
  sum(amount_paid)
    - coalesce(sum(refund_amount) filter (where refund_method = 'cash'), 0) as net_cash_in_drawer
from pos_transactions
where status <> 'void'
  and payment_method = 'cash'
group by location_id, currency, (sold_at at time zone 'America/Toronto')::date;

comment on view pos_daily_cash_summary is
  'Per till, per Toronto calendar day, per currency. Use for end-of-day cash counts.';

-- The Markham store charges Ontario HST.
update pos_events set tax_rate = 0.13, tax_label = 'HST 13%' where code = 'markham-steeles';
