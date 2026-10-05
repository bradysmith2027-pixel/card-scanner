-- ============================================================
-- 004_document_check_constraints.sql
-- Adds the CHECK constraints that were already on the live database but
-- weren't in any migration file.
--
-- The first card I tried to save from the new confirm screen failed with a
-- Postgres 23514 error on cards_acquisition_source_check. The form had a free
-- text "Bought from" box, but the database only allows a few set values. Since
-- the whole insert is one transaction, the entire card got rejected because of
-- one optional field.
--
-- The real problem was that I didn't know the constraint existed. It was added
-- straight in the Supabase dashboard and never saved in a migration, so looking
-- at migrations/ you'd think these columns were free text. If I ever rebuilt
-- the database from the migrations, it would accept stuff the real one doesn't.
--
-- Allowed values (pulled from the live database):
--   acquisition_source: purchase, pull, trade, grading_return, other
--   status:             in_hand, in_transit, at_grading, traded_away, sold
--
-- acquisition_source is HOW I got the card, not where from. A store name,
-- seller or eBay link goes in Notes.
--
-- Neither column is made required here. A CHECK passes on NULL in Postgres, so
-- NULL is still allowed for both, same as the live database.
--
-- category has no constraint on purpose, so the category dropdown can add
-- new values.
--
-- Safe to run more than once. Each constraint gets dropped and added back.
-- The data already follows these rules, so on the live database it doesn't
-- change anything. It's really for rebuilding from scratch.
-- ============================================================

-- --- 1. acquisition_source: how I got the card ---
ALTER TABLE cards DROP CONSTRAINT IF EXISTS cards_acquisition_source_check;
ALTER TABLE cards ADD CONSTRAINT cards_acquisition_source_check
  CHECK (acquisition_source = ANY (ARRAY[
    'purchase'::text,
    'pull'::text,
    'trade'::text,
    'grading_return'::text,
    'other'::text
  ]));

-- --- 2. status: where the card is right now ---
-- in_hand -> in_transit -> at_grading -> traded_away / sold
ALTER TABLE cards DROP CONSTRAINT IF EXISTS cards_status_check;
ALTER TABLE cards ADD CONSTRAINT cards_status_check
  CHECK (status = ANY (ARRAY[
    'in_hand'::text,
    'in_transit'::text,
    'at_grading'::text,
    'traded_away'::text,
    'sold'::text
  ]));

-- ============================================================
-- Not covered here, still need to check:
--
-- I didn't check demo_cards for the same constraints. 003 keeps it matching
-- cards, so it probably needs the same two, but I haven't checked the live
-- database so I'm not guessing. To check:
--
--   SELECT conrelid::regclass AS table_name, conname,
--          pg_get_constraintdef(oid) AS definition
--   FROM pg_constraint
--   WHERE conrelid IN ('public.cards'::regclass, 'public.demo_cards'::regclass)
--     AND contype = 'c'
--   ORDER BY table_name, conname;
--
-- I also never checked the other four tables (grading_submissions,
-- incoming_shipments, trades, trade_items). Take out the conrelid filter
-- above to check every table.
-- ============================================================
