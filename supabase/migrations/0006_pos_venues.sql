-- Venues: 0006
--
-- pos_events now holds two kinds of selling venue: an event (a show, with
-- dates) and a store (a permanent location, with an address). A deployment
-- runs in one mode (POS_MODE = event or store) and only lists venues of that
-- kind. The table keeps its name so existing sales and code are untouched.

alter table pos_events
  add column if not exists kind    text not null default 'event' check (kind in ('event', 'store')),
  add column if not exists address text;

alter table pos_events alter column starts_on drop not null;
alter table pos_events alter column ends_on   drop not null;

alter table pos_events drop constraint if exists pos_events_dates;
alter table pos_events add constraint pos_events_dates check (
  (kind = 'event' and starts_on is not null and ends_on is not null and ends_on >= starts_on)
  or (kind = 'store' and (starts_on is null or ends_on is null or ends_on >= starts_on))
);

comment on table pos_events is
  'Selling venues: events (shows with dates) and stores (permanent locations with an address). Sales carry event_id.';

-- The first real store.
insert into pos_events (code, name, kind, address, currency, created_by)
values ('markham-steeles', 'Markham · 4300 Steeles Ave E', 'store',
        'A1-201, 4300 Steeles Ave E, Markham, ON L3R 0Y5', 'CAD', 'migration')
on conflict (code) do nothing;
