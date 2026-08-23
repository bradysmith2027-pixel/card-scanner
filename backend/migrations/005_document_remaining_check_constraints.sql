-- ============================================================
-- 005_document_remaining_check_constraints.sql
-- Dreamboat Slabs — finish what 004 started: capture the CHECK constraints
-- on the remaining tables that exist in the live database but appear in NO
-- migration file.
--
-- WHY THIS EXISTS (2026-08-20):
--   004 closed this gap for `cards` only. Its footer flagged that
--   `demo_cards` and the other four tables (grading_submissions,
--   incoming_shipments, trades, trade_items) had never been audited for the
--   same class of undocumented constraint — the class of bug that made the
--   8/18 Postgres 23514 failure undebuggable.
--
--   The sweep was run on 2026-08-20 against the live DB:
--
--     SELECT c.conrelid::regclass::text AS table_name, c.conname,
--            v.allowed_value
--     FROM pg_constraint c
--     CROSS JOIN LATERAL (
--       SELECT m[1] AS allowed_value
--       FROM regexp_matches(pg_get_constraintdef(c.oid),
--                           '''([^'']+)''::text', 'g') AS m
--     ) v
--     WHERE c.connamespace = 'public'::regnamespace AND c.contype = 'c'
--     ORDER BY table_name, conname;
--
--   It found FIVE further undocumented CHECK constraints across three
--   tables. All five are captured below, with values read directly from the
--   live DB — not inferred. (Guessing at constraint values is precisely what
--   caused the original bug.)
--
--   The sweep also CONFIRMED that 004's two `cards` constraints match the
--   live DB exactly, value for value. 004 is accurate; it was just partial.
--
-- IDEMPOTENT: every constraint is dropped IF EXISTS and re-added, so this is
-- safe to re-run. Re-adding revalidates existing rows; current data already
-- conforms, so it is a no-op against the live DB. Its real job — like 004's —
-- is to make a from-scratch rebuild correct.
-- ============================================================


-- ============================================================
-- grading_submissions
-- ============================================================

-- --- Which grader the submission went to ---
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
-- NOTE: these five are UPPERCASE (PSA/BGS/CGC/SGC) while every other value
-- set in the schema is lowercase snake_case. That is the live DB's actual
-- shape, reproduced faithfully here. Any UI dropdown must send the uppercase
-- form exactly — a lowercase 'psa' will fail with 23514.

-- --- Where the submission is in the grading lifecycle ---
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

-- --- Where the inbound package came from ---
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
-- NOTE: unlike `cards.acquisition_source` (which records HOW a card was
-- acquired), this records WHICH MARKETPLACE/CHANNEL a package came from.
-- The two share the token 'grading_return' and 'other' but are NOT the same
-- vocabulary — do not map one onto the other.

-- --- Delivery state of the inbound package ---
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
-- NOTE: shares the token 'in_transit' with `cards.status`, but these are
-- different lifecycles (a package vs. a card). Same word, two meanings —
-- the same trap as the card_type BRAND/FINISH collision. See Field Dictionary.


-- ============================================================
-- trade_items
-- ============================================================

-- --- Which way the item moved in the trade ---
ALTER TABLE trade_items
  DROP CONSTRAINT IF EXISTS trade_items_direction_check;
ALTER TABLE trade_items
  ADD CONSTRAINT trade_items_direction_check
  CHECK (direction = ANY (ARRAY[
    'given'::text,
    'received'::text
  ]));


-- ============================================================
-- AUDIT RESULT — tables with NO check constraints
--
-- The sweep returned zero CHECK constraints for two tables. This is recorded
-- here deliberately so the question is never re-opened:
--
--   * `trades`      — no CHECK constraints. Expected; it is a header/join
--                     table (the constrained vocabulary lives on
--                     `trade_items.direction`).
--
--   * `demo_cards`  — no CHECK constraints. ⚠️ THIS IS A REAL DIVERGENCE,
--                     NOT AN OVERSIGHT IN THIS FILE.
--
-- ⚠️ THE `demo_cards` FINDING
--
--   003 mirrors `cards`' column renames onto `demo_cards` to keep it a
--   "faithful structural mirror," and 004 assumed it "likely needs the same
--   two" constraints. The live DB says otherwise: it has NEITHER. So
--   `demo_cards` will accept an `acquisition_source` or `status` value that
--   `cards` rejects outright.
--
--   This is deliberately NOT fixed here. Adding constraints to `demo_cards`
--   is a behavioral change to a table that is publicly readable (002 grants
--   anon SELECT), and a demo endpoint is still on the hardening backlog —
--   that endpoint should be designed against a decided answer, not have one
--   silently imposed at 11 PM.
--
--   THE DECISION TO MAKE (before any outside seller sees the demo):
--     (a) Mirror both constraints onto `demo_cards` so the demo enforces the
--         same rules as the real product — the honest option, and the one
--         consistent with 003's stated intent.
--     (b) Leave it unconstrained on purpose and document `demo_cards` as
--         fixture-only scratch data that is never a schema reference.
--
--   Whichever is chosen, write it down. Right now the repo implies (a) while
--   the database does (b), and that mismatch is the same failure mode 004
--   was written to eliminate.
--
-- ============================================================
-- STATUS: with this file applied, every CHECK constraint in the public
-- schema as of 2026-08-20 is captured in version control. The `004` footer's
-- follow-up item is CLOSED, except for the `demo_cards` decision above.
-- ============================================================
