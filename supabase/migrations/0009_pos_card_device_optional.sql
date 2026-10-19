-- 0009: the Clover proxy keeps the Flex serial for each store key itself, so
-- a venue only needs the store key; the device is optional and, when blank,
-- resolved from the proxy's roster at charge time.
alter table pos_card_payments alter column device drop not null;
