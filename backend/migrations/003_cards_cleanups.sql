-- ============================================================
-- 003_cards_cleanups.sql
-- Cleans up some column names on cards and adds updated_at.
--
--   1. Rename variation -> card_type. This is the parallel (refractor, blue
--      refractor, electric, ...). I pick it myself since the model can't tell
--      a card's color. Renaming keeps the data that's already there. It can be
--      empty since base cards don't have a parallel.
--   2. Rename sport -> category. What the card is: basketball, football,
--      soccer, golf, one piece, pokemon, etc. Still required. There's no brand
--      column because the brand is already in set_name (Topps Chrome, Panini
--      Prizm, ...).
--   3. Add updated_at and a trigger that updates it on every change, same as
--      incoming_shipments and grading_submissions.
--
-- Both renames happen on demo_cards too so it matches cards.
--
-- Safe to run more than once.
-- ============================================================

-- --- 1 + 2. Rename the columns on cards (only if they haven't been already) ---
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'cards' AND column_name = 'variation') THEN
    ALTER TABLE cards RENAME COLUMN variation TO card_type;
  END IF;

  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'cards' AND column_name = 'sport') THEN
    ALTER TABLE cards RENAME COLUMN sport TO category;
  END IF;
END $$;

-- --- Same renames on demo_cards ---
DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'demo_cards' AND column_name = 'variation') THEN
    ALTER TABLE demo_cards RENAME COLUMN variation TO card_type;
  END IF;

  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_name = 'demo_cards' AND column_name = 'sport') THEN
    ALTER TABLE demo_cards RENAME COLUMN sport TO category;
  END IF;
END $$;

-- --- 3. updated_at and the trigger that sets it ---
ALTER TABLE cards ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- Sets updated_at = now() every time a row changes. Other tables can use it too.
CREATE OR REPLACE FUNCTION set_updated_at()
RETURNS trigger AS $$
BEGIN
  NEW.updated_at = now();
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_cards_updated_at ON cards;
CREATE TRIGGER trg_cards_updated_at
  BEFORE UPDATE ON cards
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
