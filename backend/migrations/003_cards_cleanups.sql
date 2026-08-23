-- ============================================================
-- 003_cards_cleanups.sql
-- Dreamboat Slabs — cards table taxonomy cleanup + updated_at (punch list #1 + #2).
--
-- Decisions (2026-07-23, with Brady):
--   1. RENAME `variation` -> `card_type`. This column holds the finish/parallel
--      (refractor, blue refractor, green refractor, electric, ...), picked
--      MANUALLY by the user since the vision model can't read a card's color.
--      Renaming (not add+drop) preserves any existing data. Stays nullable
--      (a base card has no parallel).
--   2. RENAME `sport` -> `category`. Holds the specific card subject/type:
--      basketball, football, soccer, tennis, golf, one piece, pokemon, etc.
--      Stays NOT NULL (required) — value is user-entered or model-guessed from
--      the set. Existing `sport` values (basketball/football) map cleanly.
--      No `brand` column: brand is carried by set_name (Topps Chrome, Panini
--      Prizm, ...), so a separate brand column isn't needed.
--   3. ADD `updated_at timestamptz` + a BEFORE UPDATE trigger on `cards` to
--      auto-bump it, mirroring incoming_shipments / grading_submissions.
--
-- Both renames are mirrored on `demo_cards` so the public demo stays a faithful
-- structural mirror of `cards` (minus real inventory).
--
-- Idempotent: each RENAME is guarded by a check that the old column still
-- exists, ADD COLUMN IF NOT EXISTS, CREATE OR REPLACE FUNCTION, DROP TRIGGER
-- IF EXISTS. Safe to re-run.
-- ============================================================

-- --- 1 + 2. Column renames on `cards` (guarded so re-runs are no-ops) ---
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

-- --- Same renames on `demo_cards` (mirror) ---
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

-- --- 3. updated_at + auto-bump trigger on cards ---
ALTER TABLE cards ADD COLUMN IF NOT EXISTS updated_at timestamptz NOT NULL DEFAULT now();

-- Reusable trigger function: stamp updated_at = now() on every UPDATE.
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
