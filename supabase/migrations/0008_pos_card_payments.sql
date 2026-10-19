-- Card payments on a Clover Flex: 0008
--
-- A venue can name the Clover store key and device serial the register
-- charges through (via the company's Clover proxy). A card sale is a
-- pos_transactions row with payment_method = 'card' and no cash detail; the
-- attempt itself, including ones that failed or whose outcome is unknown,
-- is a pos_card_payments row keyed by the register's client record id.

alter table pos_transactions drop constraint if exists pos_transactions_payment_method_check;
alter table pos_transactions add constraint pos_transactions_payment_method_check
  check (payment_method in ('cash', 'card'));

alter table pos_transactions drop constraint if exists pos_transactions_refund_method_check;
alter table pos_transactions add constraint pos_transactions_refund_method_check
  check (refund_method is null or refund_method in ('cash', 'store_credit', 'card'));

alter table pos_events
  add column if not exists clover_store  text,
  add column if not exists clover_device text;

comment on column pos_events.clover_store  is 'Store key the Clover proxy knows this venue by; null = no card payments.';
comment on column pos_events.clover_device is 'Serial number of the Clover Flex at this venue.';

create table if not exists pos_card_payments (
  id                bigint generated always as identity primary key,
  client_record_id  text not null unique,              -- the sale's idempotency key; also the Clover order id
  event_id          uuid references pos_events (id),
  transaction_id    uuid references pos_transactions (id) on delete set null,
  amount            numeric(12,2) not null check (amount > 0),
  currency          char(3) not null,
  store_key         text not null,
  device            text not null,
  staff_name        text,
  status            text not null default 'started'
                    check (status in ('started', 'paid', 'failed', 'unknown')),
  clover_payment_id text,
  message           text,
  response          jsonb,
  cart              jsonb not null,                    -- the lines, discount and note, to record the sale once paid
  created_at        timestamptz not null default now(),
  updated_at        timestamptz not null default now()
);

create index if not exists pos_card_payments_open_idx on pos_card_payments (created_at desc) where status in ('started', 'unknown');

alter table pos_card_payments enable row level security;

comment on table pos_card_payments is
  'One row per card charge sent to the Clover Flex. paid rows point at the sale they became; unknown rows need a status check.';
