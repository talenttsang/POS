-- Fuzzy product search: 0012
--
-- The register matches each typed word anywhere in a product's name or SKU,
-- ignoring case, punctuation and word order, and tolerates small typos with
-- pg_trgm's word_similarity. Additive: it installs the extension and changes
-- no data.
create schema if not exists extensions;
create extension if not exists pg_trgm with schema extensions;
