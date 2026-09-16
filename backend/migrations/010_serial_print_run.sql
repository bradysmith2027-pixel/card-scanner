-- ============================================================
-- 010_serial_print_run.sql
-- Dreamboat Slabs — somewhere to put the serial / print run.
--
-- WHY THIS EXISTS (2026-09-16):
--   The 2026-09-15 full-card scan evaluation surfaced this on real cards:
--
--       topps_001   front='9/25'    back='127'
--       topps_002   front='22/50'   back='255'
--
--   BOTH readings are correct. The front number is the SERIAL (the stamped
--   print run — this is card 9 of 25 ever made). The back number is the CARD
--   NUMBER (its position in the set checklist). They are different facts, and
--   they were competing for the single `card_number` column.
--
--   The scan prompt was taught to tell them apart on 2026-09-15, which stopped
--   the wrong value being written — but it did so by returning NULL for the
--   serial, because `cards` has no column to put it in (verified across all
--   nine prior migrations). So the current behaviour is: read it correctly,
--   then throw it away.
--
--   ⚠️ THIS IS NOT A SCANNER BUG. Manual entry loses the serial too, and the
--   old detector path lost it as well. It is a schema gap, which is why the
--   fix is a migration and not a prompt change.
--
-- WHY IT MATTERS MORE THAN IT LOOKS:
--   On a numbered parallel the print run IS most of the value. A /25 and a
--   /199 of the same player, same year, same set are the same row in this
--   database today and are not remotely the same asset. With ~$50k of
--   inventory being allocated across lanes on the strength of these numbers,
--   the serial is the most value-relevant attribute after the player — and the
--   `raw_to_grade` and `graded_arb` lanes in particular live on it.
--
--   It also silently breaks identity: two different physical cards from the
--   same parallel run are indistinguishable rows, so "do I already own this?"
--   cannot be answered.
--
-- ------------------------------------------------------------
-- DESIGN DECISION: ONE text column, storing the serial AS PRINTED.
--
--   Rejected: splitting into serial_number + print_run integers.
--
--   It is tempting, because "how many /25s do I hold?" is a real question and
--   an integer answers it directly. It is still the wrong call here, for the
--   same reason migration 007 refused to decompose the spreadsheet's all-in
--   "Card Cost": a lossy transformation applied at write time cannot be undone
--   later, and the real world does not fit the two-integer shape —
--
--       "1/1"        fits
--       "9/25"       fits
--       "FOTL 12/99" does not
--       "A/50"       does not (lettered runs)
--       "1 of 1"     does not
--       "/25"        does not (run known, number unreadable — common on
--                    glare-heavy fronts, and a real OCR outcome)
--
--   Anything that does not fit would be dropped or mangled on entry, and the
--   card's face is the only place that fact exists. Store what is printed;
--   derive the integers later in a report, where a parse failure is visible
--   and costs nothing. `numeric` fields can be added in a future migration
--   from this text — the reverse is impossible.
--
-- ⚠️ NULL vs '' — NULL MEANS "NOT NUMBERED", and that is a real fact about the
--   card, not missing data. Most base cards are unnumbered. Do not backfill
--   this column with anything, and do not let the UI send '' for a blank box:
--   the CHECK below rejects the empty string outright so a blank input cannot
--   masquerade as a serial that failed to read.
--
-- IDEMPOTENT — ADD COLUMN IF NOT EXISTS, safe to re-run.
-- ============================================================

-- ------------------------------------------------------------
-- 1. The column
--
--    Nullable on purpose (see above). The CHECK enforces two things:
--      * no empty string — blank must be NULL
--      * no leading/trailing whitespace — ' 9/25' and '9/25' must not become
--        two different serials for the same print run
--    Capped at 32 characters: every real-world form above fits comfortably,
--    and the cap stops a mis-parsed OCR blob landing in an identity field.
-- ------------------------------------------------------------
alter table cards add column if not exists serial text
  check (
    serial is null
    or (serial = btrim(serial) and length(serial) between 1 and 32)
  );

-- ------------------------------------------------------------
-- 2. Index
--
--    Partial, because the overwhelming majority of cards are unnumbered and
--    NULL rows are never grouped or filtered on. Supports the report this
--    column exists to enable: "show me everything numbered," and the
--    per-lane numbered-inventory breakdown in the monthly review.
-- ------------------------------------------------------------
create index if not exists cards_serial_idx
  on cards (user_id, serial) where serial is not null;

-- ------------------------------------------------------------
-- 3. Documentation — the conventions belong next to the column, not only
--    in this file. The next person reading the schema in the Supabase UI
--    should not have to find migration 010 to know what NULL means.
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
-- VERIFY (run manually after applying)
--
--   -- column landed, nullable, no default?
--   select column_name, data_type, is_nullable, column_default
--     from information_schema.columns
--    where table_name = 'cards' and column_name = 'serial';
--
--   -- constraint landed?
--   select conname, pg_get_constraintdef(oid)
--     from pg_constraint
--    where conrelid = 'cards'::regclass and contype = 'c'
--      and pg_get_constraintdef(oid) ilike '%serial%';
--
--   -- index landed?
--   select indexname, indexdef from pg_indexes
--    where tablename = 'cards' and indexname = 'cards_serial_idx';
--
--   -- nothing should have been backfilled: expect 0
--   select count(*) from cards where serial is not null;
--
--   -- the CHECK should REJECT each of these (all three must error 23514):
--   --   update cards set serial = ''       where id = '<some id>';
--   --   update cards set serial = ' 9/25'  where id = '<some id>';
--   --   update cards set serial = repeat('x', 33) where id = '<some id>';
--   -- and ACCEPT this one:
--   --   update cards set serial = '9/25'   where id = '<some id>';
--   -- (roll back afterwards — don't leave a fake serial on a real card)
-- ============================================================
