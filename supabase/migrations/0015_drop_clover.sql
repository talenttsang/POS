-- 0015: the register no longer talks to a card reader. Card sales are taken
-- on a separate terminal and recorded with payment_method = 'card', so the
-- Clover charge log and each venue's Clover store key and device go.
drop table if exists pos_card_payments;

alter table pos_events
  drop column if exists clover_store,
  drop column if exists clover_device;
