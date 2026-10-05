-- ============================================================
-- 005_document_remaining_check_constraints.sql
-- Finishes what 004 started. Adds the CHECK constraints on the other tables
-- that were on the live database but not in any migration.
--
-- 004 only did cards. I ran this on the live database to check every table:
--
--     SELECT c.conrelid::regclass::text AS table_name, c.conname,
--            v.allowed_value
--     FROM pg_constraint c
--     CROSS JOIN LATERAL (
--       SELECT m[1] AS allowed_value
--       FROM regexp_matches(pg_get_constraintdef(c.oid),
--                           '''([^'']+)'''::text', 'g') AS m
--     ) v
--     WHERE c.connamespace = 'public'::regnamespace AND c.contype = 'c'
--     ORDER BY table_name, conname;
--
-- It found five more constraints across three tables. They're all below, with
-- the values copied straight from the live database, not guessed.
--
-- It also showed 004's two constraints on cards match the live database
-- exactly.
--
-- Safe to run more than once. Each constraint gets dropped and added back.
-- Like 004, it doesn't change the live database, it's for rebuilding from
-- scratch.
-- ============================================================


-- ============================================================
-- grading_submissions
-- ============================================================

-- --- Which grading company it went to ---
ALTER TABLE grading_submissions
  DROP CONSTRAINT IF EXISTS grading_submissions_grading_company_check;
ALTER TABLE grading_submissions
  ADD CONSTRAINT grading_submissions_grading_company_check
  CHECK (grading_company = ANY (ARRAY[
    'PSA'::text,
    'BGS'::text,
    'CGC'::text,
    'SGC'::text,
    'other'::text
  ]));
-- These are UPPERCASE (PSA/BGS/CGC/SGC) even though everything else in the
-- database is lowercase. That's how the live database has it. A dropdown has
-- to send the uppercase version or 'psa' will fail with 23514.

-- --- Where the submission is at ---
ALTER TABLE grading_submissions
  DROP CONSTRAINT IF EXISTS grading_submissions_status_check;
ALTER TABLE grading_submissions
  ADD CONSTRAINT grading_submissions_status_check
  CHECK (status = ANY (ARRAY[
    'submitted'::text,
    'in_progress'::text,
    'returned'::text,
    'lost'::text
  ]));


-- ============================================================
-- incoming_shipments
-- ============================================================

-- --- Where the package came from ---
ALTER TABLE incoming_shipments
  DROP CONSTRAINT IF EXISTS incoming_shipments_source_check;
ALTER TABLE incoming_shipments
  ADD CONSTRAINT incoming_shipments_source_check
  CHECK (source = ANY (ARRAY[
    'ebay'::text,
    'tcgplayer'::text,
    'whatnot'::text,
    'private_seller'::text,
    'grading_return'::text,
    'other'::text
  ]));
-- This isn't the same as cards.acquisition_source (HOW I got a card). This is
-- WHICH site or channel the package came from. Both have 'grading_return' and
-- 'other' but they're different lists, don't mix them up.

-- --- Delivery status of the package ---
ALTER TABLE incoming_shipments
  DROP CONSTRAINT IF EXISTS incoming_shipments_status_check;
ALTER TABLE incoming_shipments
  ADD CONSTRAINT incoming_shipments_status_check
  CHECK (status = ANY (ARRAY[
    'pending'::text,
    'in_transit'::text,
    'out_for_delivery'::text,
    'delivered'::text,
    'exception'::text,
    'unknown'::text
  ]));
-- Also has 'in_transit' like cards.status, but one is a package and one is a
-- card. Same word, different meaning, kind of like card_type (brand vs
-- parallel).


-- ============================================================
-- trade_items
-- ============================================================

-- --- Whether the card was given or received in the trade ---
ALTER TABLE trade_items
  DROP CONSTRAINT IF EXISTS trade_items_direction_check;
ALTER TABLE trade_items
  ADD CONSTRAINT trade_items_direction_check
  CHECK (direction = ANY (ARRAY[
    'given'::text,
    'received'::text
  ]));


-- ============================================================
-- Tables with no CHECK constraints:
--
--   * trades      - none. That's expected, the list of allowed values is on
--                   trade_items.direction instead.
--
--   * demo_cards  - none. This one is actually different from cards.
--
-- 003 keeps demo_cards matching cards, and 004 guessed it probably had the
-- same two constraints. It doesn't have either. So demo_cards accepts
-- acquisition_source and status values that cards would reject.
--
-- I'm not fixing that here. demo_cards is readable by anyone (002), and I
-- still haven't built the demo page, so I want to decide this when I build it.
--
-- The options:
--   (a) Add the same constraints to demo_cards so the demo follows the same
--       rules as the real app. This is what 003 was going for.
--   (b) Leave it as is and treat demo_cards as just sample data.
--
-- Either way, write down which one. Right now the migrations look like (a)
-- but the database does (b).
--
-- ============================================================
-- With this applied, every CHECK constraint in the database is in a migration
-- file. The only thing left from 004 is the demo_cards decision above.
-- ============================================================
