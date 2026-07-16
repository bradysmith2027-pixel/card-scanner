# Step 4, Part 1: Check Your Card Crops

This is the first thing to do for Step 4 (GPT-4o OCR Pipeline). Before we
teach the computer to *read* the text on your cards, we need to make sure
it's cutting out the *right* text cleanly. This script does the cutting.
You do the looking.

## Why this version doesn't need a downloaded weights file

Your Roboflow account is on the free Public plan, and manually exporting
the raw `.pt` weights file is locked behind their paid Core plan
($79–99/month). Rather than pay for that, this script uses Roboflow's
free **`inference`** package instead. You give it your model ID and API
key, and it downloads and caches the model automatically the first time it
runs — after that it works offline. It's still running entirely on your
own machine (not billed per scan), so it still fits the project's
self-hosted, ~$2/month plan. It just skips the step of you handling a
loose `.pt` file yourself.

## What you need before you start

1. **Python installed** on your computer (3.9 or newer is fine).
2. **Your Roboflow API key.** In Roboflow, go to Settings → API Keys and
   copy your key. Treat it like a password — don't share it or commit it
   to a public repo.
3. **A few test card photos.** You can reuse photos from your existing
   dataset (`/card-dataset/topps`, `/card-dataset/panini`,
   `/card-dataset/one_piece`), or take a couple fresh ones. Pick at least
   one Topps, one Panini, one One Piece, and one shiny/foil card if you
   have one — foil is called out as a risk in your notes.

## One-time setup

Open a terminal in this folder and run:

```
pip install -r requirements.txt
```

This installs two things: `inference` (Roboflow's free self-hosted model
runner) and `opencv-python` (handles the images).

Then set your API key so you don't have to type it every time (recommended
over passing it on the command line, so it doesn't end up in your terminal
history):

```
export ROBOFLOW_API_KEY=your_key_here          # Mac/Linux
setx ROBOFLOW_API_KEY your_key_here             # Windows (restart terminal after)
```

## Running it

The script already points at your trained model
(`bradys-workspace-wqkgm/dreamboat-slabs-1-yolov8n-t1`) by default, so you
don't need to specify it.

**On one photo:**

```
python crop_card_regions.py --image test_card.jpg
```

**On a whole folder of photos at once:**

```
python crop_card_regions.py --images-dir ./card-dataset/topps
```

**If you're testing a One Piece card**, add `--card-type one_piece` so it
only keeps `player_name` and `card_number` (matching your project's rule
that One Piece cards don't have a usable year/set field):

```
python crop_card_regions.py --image op_001_front.jpg --card-type one_piece
```

The very first run will take a bit longer while it downloads and caches
the model. Every run after that is faster and works offline.

## What you'll get back

A new folder called `crops_output` with two subfolders:

- **`crops/`** — every detected field, cropped and saved as its own small
  image, sorted into subfolders like `crops/player_name/`,
  `crops/card_number/`, `crops/year/`, `crops/set_name/`,
  `crops/set_logo/`.
- **`annotated/`** — a copy of each original photo with boxes drawn around
  everything the model found, so you can see at a glance what it detected
  without opening every single crop.

The script also prints a summary in the terminal, including a callout for
any detection with confidence below 50% — those are the ones most worth a
second look.

## What to check

Open the `crops` folder and look at a handful of images from each field
type. Ask yourself, plainly: **can I read this text myself?**

- If most crops are clean and readable — great, you're ready to move on to
  the next part of Step 4, which is having GPT-4o actually read the text
  out of these crops.
- If a lot of crops are blurry, cut off at the edge, or clearly the wrong
  part of the card — that's worth fixing (retraining, more padding, better
  photo quality) before we spend time/money wiring up GPT-4o, since bad
  crops in means bad text out no matter how good the OCR is.

## Adjusting if things look off

- Crops cutting off text at the edges? Open `crop_card_regions.py` and
  increase `PADDING_PIXELS` near the top (currently 6).
- Missing obvious fields? Try lowering `--confidence` (default 0.25), e.g.
  `--confidence 0.15`.
- Getting junk/false boxes? Try raising `--confidence`, e.g. `--confidence 0.4`.
- See a line like `Skipping a prediction with unexpected shape: ...`? The
  `inference` package has changed its response format slightly between
  versions before. Copy that printed line and send it to me — it tells me
  exactly which field name changed so I can fix the script.
- "No Roboflow API key found"? Double-check you ran the `export`/`setx`
  command in the *same* terminal window you're running the script from.

## Once you're happy with the crops

Once crops look solid, move on to Part 2 below — the real OCR step.

---

# Step 4, Part 2: Read the Cards with GPT-4o

This is `ocr_card.py`. It does everything `crop_card_regions.py` does
(detect + crop each field), then sends those crops to GPT-4o in **one
request** and gets back the actual card data — year, set, card number,
player name — as structured JSON. If you give it both a front and back
photo, it combines them into a single answer:

- If front and back agree on a field, you get one value (it won't say
  "Ace Bailey Ace Bailey" just because both photos show the name).
- If front and back genuinely disagree on a field, that field is left
  blank and listed under `"needs_review"` instead of the script silently
  guessing which side is right — you'll see both readings printed so you
  can judge for yourself. (The real "pick the right one" screen is Step 6,
  not built yet — for now this just surfaces the conflict.)
- Card rarity/variation is never asked for — that's a separate, later
  step where you pick from a dropdown, not something OCR figures out.

## What you need before you start

Everything from Part 1, plus a new one: an **OpenAI API key**. This is
separate from your Roboflow key, and unlike Roboflow's free plan, OpenAI
bills you per request — GPT-4o OCR calls are cheap (this is the "~$1/month"
line in your Cost Estimate table), but it does require adding a payment
method.

### Getting an OpenAI API key

1. Go to **platform.openai.com** and sign up or log in. (This is different
   from the regular ChatGPT website — it's the developer/API side.)
2. Go to **Settings → Billing** and add a payment method. OpenAI's API
   doesn't have a meaningful free tier for GPT-4o, so this step is
   required before any request will work.
3. (Optional but recommended) Under Billing, set a **usage limit** — e.g.
   $5/month — so you can't be surprised by a runaway bill while testing.
4. Go to **API Keys** (left sidebar) and click **Create new secret key**.
   Give it a name like "dreamboat-slabs" and copy the key immediately —
   OpenAI only shows it to you once.
5. Set it as an environment variable, same pattern as your Roboflow key:
   ```
   setx OPENAI_API_KEY your_key_here          # Windows (then reopen your terminal)
   export OPENAI_API_KEY=your_key_here        # Mac/Linux
   ```

## One-time setup

The `openai` package is already in `requirements.txt`, so if you ran
`pip install -r requirements.txt` fresh it's covered. If you set up your
venv before this update, just run it again (with the venv active):

```
pip install -r requirements.txt
```

## Running it

**Topps or Panini, front and back:**

```
python ocr_card.py --card-type topps --front "..\Topps\topps_001_front.png" --back "..\Topps\topps_001_back.png"
```

**One Piece, front only** (per your project's dataset spec):

```
python ocr_card.py --card-type one_piece --front "..\One Piece\op_001_front.png"
```

**Front only, no back photo** (works fine, back fields just come back
null — no error):

```
python ocr_card.py --card-type panini --front "..\Panini\panini_001_front.png"
```

**Want to see exactly what crops got sent to GPT-4o?** Add `--save-crops`
with a folder path — useful for debugging if a result looks wrong and you
want to check whether the crop or the OCR was at fault:

```
python ocr_card.py --card-type topps --front "..\Topps\topps_001_front.png" --back "..\Topps\topps_001_back.png" --save-crops debug_crops
```

## What you'll get back

Printed JSON in your terminal, e.g.:

```json
{
  "card_type": "topps",
  "year": "2025",
  "set_name": "Topps Chrome",
  "card_number": "44/99",
  "player_name": "Ace Bailey"
}
```

If front and back disagreed on something, you'll instead see that field
come back `null`, plus extra fields showing what happened:

```json
{
  "card_type": "topps",
  "year": "2025",
  "set_name": "Topps Chrome",
  "card_number": null,
  "player_name": "Ace Bailey",
  "needs_review": ["card_number"],
  "conflicts": {
    "card_number": { "front": "44/99", "back": "44/999" }
  }
}
```

## What to check

Run it on a handful of cards across all three types and compare the JSON
to what's actually printed on the card. Specifically look for:

- Fields it got right vs. fields it missed (missed = `null`, same
  failure-handling behavior as a bad crop).
- Any `needs_review` conflicts — open the two photos and check which side
  (front or back) was actually correct, so you get a feel for how often
  this happens.
- Made-up-looking values — the system prompt tells GPT-4o not to guess,
  but worth spot-checking that it's actually being honest about unclear
  text rather than inventing something plausible.

## Adjusting if things look off

- Same crop-quality knobs as Part 1 apply here too (`--confidence`,
  `PADDING_PIXELS` in `card_vision.py`) since this script detects and
  crops the same way.
- "No OpenAI API key found"? Same fix pattern as the Roboflow key — check
  you ran `setx`/`export` in the same terminal window, and reopened it
  afterward if you used `setx`.
- Getting a billing/quota error from OpenAI? Check platform.openai.com →
  Billing — you likely need to add a payment method or raise your usage
  limit.

## Once this is working well

That's Step 4 functionally done. Step 5 is wiring this same logic (model
loading + detect + crop + GPT-4o call) into the real FastAPI backend on
Railway, behind a proper upload endpoint instead of command-line
arguments — `card_vision.py`'s functions are written so they can be
imported directly into that backend rather than rewritten from scratch.
