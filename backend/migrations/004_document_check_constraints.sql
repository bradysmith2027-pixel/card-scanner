-- ============================================================
-- 004_document_check_constraints.sql
-- Dreamboat Slabs — bring the repo in sync with CHECK constraints that
-- already exist in the live database but appear in NO migration file.
--
-- WHY THIS EXISTS (2026-08-18 / 2026-08-19):
--   The first real save from the new confirm screen failed with Postgres
--   error 23514 (check_violation) on `cards_acquisition_source_check`.
--   `acquisition_source` had been built in the UI as a free-text "Bought
--   from" box, but the live DB restricts it to a fixed value set. Because
--   the INSERT is a single transaction, the ENTIRE card was rejected over
--   one optional field.
--
--   Root cause was not the constraint — it was that the constraint was
--   INVISIBLE. It was applied directly to the database (via the Supabase
--   UI / an ad-hoc statement) and never captured in version control, so
--   anyone reading `migrations/` would conclude these columns were free
--   text. This migration closes that gap.
--
--   This matters more than it looks: before onboarding any outside seller,
--   the repo must be a truthful description of the schema. A rebuild from
--   migrations alone currently produces a DB that silently accepts data
--   the real one rejects.
--
-- VALUES (read from the live DB on 2026-08-19 via pg_get_constraintdef):
--   acquisition_source: purchase, pull, trade, grading_return, other
--   status:             in_hand, in_transit, at_grading, traded_away, sold
--
-- SEMANTICS WORTH NOTING:
--   `acquisition_source` records HOW a card was acquired — not free-text
--   "where from." A store name, seller handle, or eBay URL does NOT belong
--   here; that's Notes (or a future dedicated column). See the Field
--   Dictionary.
--
--   Neither column is made NOT NULL here. In Postgres a CHECK that
--   evaluates to NULL passes, so NULL remains legal for both — this
--   migration deliberately reproduces the live behavior exactly rather
--   than tightening it.
--
--   `category` is intentionally NOT constrained (verified 2026-08-19), so
--   the category dropdown in the confirm screen is free to add values.
--
-- IDEMPOTENT: each constraint is dropped IF EXISTS and re-added, so this is
-- safe to re-run. Re-adding revalidates existing rows; current data already
-- conforms, so it is a no-op against the live DB. Its real job is to make a
-- from-scratch rebuild correct.
-- ============================================================

-- --- 1. acquisition_source: how the card was acquired ---
ALTER TABLE cards DROP CONSTRAINT IF EXISTS cards_acquisition_source_check;
ALTER TABLE cards ADD CONSTRAINT cards_acquisition_source_check
  CHECK (acquisition_source = ANY (ARRAY[
    'purchase'::text,
    'pull'::text,
    'trade'::text,
    'grading_return'::text,
    'other'::text
  ]));

-- --- 2. status: where the card is in its lifecycle ---
-- Mirrors the pipeline: in_hand -> in_transit -> at_grading -> traded_away / sold
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
-- NOT COVERED HERE — follow-up needed:
--
-- `demo_cards` was NOT audited for equivalent constraints. 003 treats it as
-- a faithful structural mirror of `cards`, so it likely needs the same two,
-- but that was not verified against the live DB and this migration does not
-- guess. To check, run:
--
--   SELECT conrelid::regclass AS table_name, conname,
--          pg_get_constraintdef(oid) AS definition
--   FROM pg_constraint
--   WHERE conrelid IN ('public.cards'::regclass, 'public.demo_cards'::regclass)
--     AND contype = 'c'
--   ORDER BY table_name, conname;
--
-- The other four tables (grading_submissions, incoming_shipments, trades,
-- trade_items) were never audited for undocumented constraints either. The
-- same class of bug can be hiding in any of them. Widen the query above by
-- dropping the conrelid filter to sweep the whole public schema.
-- ============================================================
