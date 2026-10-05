-- ============================================================
-- 010_serial_print_run.sql
-- A column for the serial / print run.
--
-- When I tested full card scanning on my real cards I got this:
--
--       topps_001   front='9/25'    back='127'
--       topps_002   front='22/50'   back='255'
--
-- Both readings were right. The front number is the serial (card 9 of 25
-- made). The back number is the card number (its spot in the set). They're
-- different things, but they were both going into the one card_number column.
--
-- I fixed the scan prompt so it could tell them apart, but it just returned
-- null for the serial since there was no column for it. So it was reading it
-- right and then throwing it away.
--
-- This isn't a scanner problem. Adding a card by hand loses the serial too,
-- and so did the old detector. The table just didn't have a spot for it.
--
-- Why it matters:
--   On a numbered card the print run is a lot of the value. A /25 and a /199
--   of the same player, year and set were the same row in my database, and
--   they're nowhere near the same price. After the player, the serial is
--   probably the most important thing about a card for pricing.
--
--   It also means two different numbered cards from the same set look the
--   same, so I couldn't tell if I already own one.
--
-- ------------------------------------------------------------
-- One text column, saved exactly how it's printed.
--
--   I thought about splitting it into two numbers (serial_number and
--   print_run), since "how many /25s do I have?" is something I'd want to
--   know. But a lot of real serials don't fit that:
--
--       "1/1"        fits
--       "9/25"       fits
--       "FOTL 12/99" doesn't
--       "A/50"       doesn't (lettered)
--       "1 of 1"     doesn't
--       "/25"        doesn't (can read the print run but the number has glare
--                    on it, which happens a lot)
--
--   Anything that didn't fit would get lost, and the card itself is the only
--   place that info exists. So I save what's printed, and if I want numbers
--   later a report can pull them out of the text. You can go from text to
--   numbers later, but not the other way.
--
-- NULL means the card isn't numbered. That's a real answer, not missing data,
-- and most base cards aren't numbered. Don't fill this column in with anything,
-- and don't let the app send '' for a blank box. The CHECK below blocks ''
-- so a blank can't look like a serial that didn't read right.
--
-- Safe to run more than once (ADD COLUMN IF NOT EXISTS).
-- ============================================================

-- ------------------------------------------------------------
-- 1. The column
--
--    Can be empty on purpose (see above). The CHECK makes sure:
--      * no empty string, blank has to be NULL
--      * no spaces at the start or end, so ' 9/25' and '9/25' aren't saved as
--        two different serials
--    Max 32 characters. Every real serial fits easily, and it stops a bad OCR
--    read from getting saved.
-- ------------------------------------------------------------
alter table cards add column if not exists serial text
  check (
    serial is null
    or (serial = btrim(serial) and length(serial) between 1 and 32)
  );

-- ------------------------------------------------------------
-- 2. Index
--
--    Only indexes cards that have a serial, since most don't. It's for showing
--    everything numbered and the numbered cards by lane in my monthly review.
-- ------------------------------------------------------------
create index if not exists cards_serial_idx
  on cards (user_id, serial) where serial is not null;

-- ------------------------------------------------------------
-- 3. Column description, so I don't have to find this file to know what
--    NULL means when I'm looking at it in Supabase.
-- ------------------------------------------------------------
comment on column cards.serial is
  'Serial / print run AS PRINTED on the card (e.g. "9/25", "1/1", "FOTL 12/99"). '
  'NULL means the card is NOT numbered — that is a fact, not missing data. '
  'Never store an empty string. Deliberately NOT split into integers: the printed '
  'form is lossy to decompose and the card face is the only source. See migration 010.';

comment on column cards.card_number is
  'The card''s number in the set checklist (usually on the BACK near the copyright). '
  'This is NOT the serial/print run — that is cards.serial. Conflating the two was the '
  'bug migration 010 exists to fix.';

-- ============================================================
-- Check it worked (run after applying)
--
--   -- is the column there, nullable, with no default?
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_name = 'cards' and column_name = 'serial';
--
--   -- is the constraint there?
--   select conname, pg_get_constraintdef(oid)
--     from pg_constraint
--    where conrelid = 'cards'::regclass and contype = 'c'
--      and pg_get_constraintdef(oid) ilike '%serial%';
--
--   -- is the index there?
--   select indexname, indexdef from pg_indexes
--    where tablename = 'cards' and indexname = 'cards_serial_idx';
--
--   -- nothing should have been filled in: expect 0
--   select count(*) from cards where serial is not null;
--
--   -- the CHECK should reject all three of these (each should error 23514):
--   --   update cards set serial = ''       where id = '<some id>';
--   --   update cards set serial = ' 9/25'  where id = '<some id>';
--   --   update cards set serial = repeat('x', 33) where id = '<some id>';
--   -- and allow this one:
--   --   update cards set serial = '9/25'   where id = '<some id>';
--   -- (roll it back after, don't leave a fake serial on a real card)
-- ============================================================
