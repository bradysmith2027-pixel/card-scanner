"""
ocr_card.py

Reads a card from a photo.

Give it a photo of the front of a card (and the back for sports cards, Topps
and Panini) and say if it's "sports" or "tcg". It finds the text with my
model, crops it, sends all the crops to GPT-4o in one request, and gets back
the year, set, number and player. Then it combines the front and back into
one answer:
  - If the front and back match, it's just one value (it won't say
    "Ace Bailey Ace Bailey").
  - If they don't match, the field is left blank and flagged in
    "needs_review" instead of guessing which side is right.
  - It doesn't try to figure out the parallel. That gets picked separately.

Card type:
  You don't have to know Topps vs Panini ahead of time. Just pick a mode:
    --capture-mode tcg     -> front only, card_type is always "one_piece"
                               (the only TCG it handles right now).
    --capture-mode sports  -> front and back, and GPT-4o guesses Topps vs
                               Panini from the set logo.
  The guess is just a starting point. Pass --card-type topps or panini to
  skip it. If it can't tell from the logo, it stops and asks you to run it
  again with --card-type instead of just guessing.

How to run it:

  Sports card (Topps/Panini), let it guess card_type from the logo:
    python ocr_card.py --capture-mode sports --front "..\\Topps\\topps_001_front.png" --back "..\\Topps\\topps_001_back.png"

  Sports card, but force the card_type instead of guessing:
    python ocr_card.py --capture-mode sports --card-type panini --front "..\\Panini\\panini_001_front.png" --back "..\\Panini\\panini_001_back.png"

  TCG (One Piece, front only):
    python ocr_card.py --capture-mode tcg --front "..\\One Piece\\op_001_front.png"

Needs:
  ROBOFLOW_API_KEY (same one crop_card_regions.py uses)
  OPENAI_API_KEY   (see README.md for how to get one)
"""

import argparse
import base64
import json
import os
import re
import sys

try:
    # Normal case, imported as part of the vision package (the API does this).
    from . import card_vision
except ImportError:
    # Running it straight from the command line, where there's no package.
    import card_vision

# One Piece cards only have card_type, card_number and player_name. No year or
# set_name.
#
# I added "serial" when I added the serial column (migration 010). Before that
# GPT was reading the serial right but there was nowhere to save it.
#
# One Piece doesn't get a serial. Those cards aren't numbered like Topps and
# Panini, and One Piece already reads 10/10 in my tests, so I don't want to
# mess with it.
#
# In detector mode there's no "serial" box in my Roboflow labels, so no crop
# gets sent for it and it just comes back null. The detector could never read
# serials anyway.
FIELDS_BY_CARD_TYPE = {
    "topps": ["year", "set_name", "card_number", "serial", "player_name"],
    "panini": ["year", "set_name", "card_number", "serial", "player_name"],
    "one_piece": ["card_number", "player_name"],
}

# The sport or game. GPT figures this out from what the card looks like, it
# isn't printed text.
#
# The detector couldn't do this since it only sent crops of the text, and you
# can't tell basketball from hockey from a crop of a card number. So I always
# had to pick it by hand. Full card mode sends the whole card, so the sport is
# easy to see.
#
# These have to match CATEGORIES in the frontend's cardOptions.ts exactly. The
# category column doesn't have a CHECK constraint, so a wrong value would save
# without an error and then mess up any report grouped by category.
ALLOWED_CATEGORIES = [
    "basketball",
    "football",
    "baseball",
    "hockey",
    "soccer",
    "one piece",
    "pokemon",
    "other",
]

# Extra crops that get sent just to help, not to read text from. set_logo is
# the set's logo (like the Topps Chrome logo). It helps GPT-4o figure out the
# set_name when the text is hard to read from foil or glare. I never labeled a
# set_logo on One Piece cards so there's nothing for those. The same logo crop
# is also used to guess Topps vs Panini below.
CONTEXT_CLASSES_BY_CARD_TYPE = {
    "topps": ["set_logo"],
    "panini": ["set_logo"],
    "one_piece": [],
}

SYSTEM_PROMPT = """You are reading printed text off cropped close-up photos of a trading \
card. Each image you're given is labeled with which side of the card it's from (front or \
back) and which field it represents (e.g. year, set name, card number, player name).

Some images are labeled "set_logo" -- this is the set's visual logo mark, not text to \
transcribe. It is NOT one of the fields you're asked to output. Use it only as supporting \
visual evidence to help you get "set_name" right -- for example, if the printed set_name \
text is stylized, foil, or partly obscured by glare but you recognize the logo, let that \
inform your reading of set_name. Do not output any value for the logo itself.

Rules:
- Read exactly what is printed. Do not guess, autocomplete, or infer a value that isn't \
clearly legible in the image.
- If a crop is blurry, cut off, glare-obscured, or doesn't clearly show the expected text, \
return null for that field rather than guessing -- unless a set_logo image lets you confirm \
the set_name with real confidence, per the rule above.
- For "set_name", output ONLY the brand and set name (e.g. "Panini Prizm", "Topps Chrome", \
"Bowman Chrome"). Do NOT include the year, the sport (e.g. "Football", "Baseball"), the card \
number, or the player -- those are separate fields or not wanted at all. If the printed text \
around the logo reads something like "2025 Panini - Prizm Football", extract just the brand + \
set portion ("Panini Prizm").
- For "card_number", output the card's number as printed, removing ONLY a leading label \
word or symbol such as "No.", "#", or "Card" (e.g. "No. 388" -> "388", "#44/99" -> "44/99"). \
Do NOT strip a set or series code that is itself part of the number -- e.g. a One Piece \
number printed as "OP01-024" must stay complete as "OP01-024" (never "024"), and a serial \
like "44/99" keeps both parts. When unsure whether something is a label or part of the \
number, keep it.
- "serial" is the stamped limited print run (a fraction like "9/25", meaning this is card 9 \
of only 25 made). It is NOT the card number and the two must never be swapped. There is no \
cropped image for it in this mode, so unless a serial is plainly visible inside another \
crop, return null for "serial". Most cards are not numbered and null is the correct answer.
- Do not attempt to identify the card's rarity, parallel type, or visual variation -- only \
extract the literal printed text for the fields listed.
- If no images are labeled "back", return null for every field under "back" -- do not \
invent values.
"""

# Only used to guess Topps vs Panini. It's a separate, smaller GPT-4o call on
# just the logo crop, before the main call.
CARD_TYPE_GUESS_SYSTEM_PROMPT = """You are looking at a cropped close-up photo of a \
trading card's set logo mark (e.g. the Topps or Panini logo).

Rules:
- Only answer "topps" or "panini" if you can clearly recognize the logo.
- If the image is missing, blurry, cropped off, or too ambiguous to tell confidently, \
return null -- do not guess.
"""


def parse_args():
    parser = argparse.ArgumentParser(
        description="Run the full OCR step: detect + crop + GPT-4o read, front and back merged."
    )
    parser.add_argument("--front", required=True, help="Path to the front photo of the card.")
    parser.add_argument(
        "--back", default=None,
        help="Path to the back photo. Only used with --capture-mode sports.",
    )
    parser.add_argument(
        "--capture-mode", required=True, choices=["sports", "tcg"],
        help=(
            "'sports' = front + back (Topps/Panini). 'tcg' = front only "
            "(One Piece is the only TCG supported today). Picked by the "
            "user on the scan screen, per revised Decision #1."
        ),
    )
    parser.add_argument(
        "--card-type", default=None, choices=["topps", "panini", "one_piece"],
        help=(
            "Optional. With --capture-mode tcg this is always forced to "
            "'one_piece' regardless of this flag. With --capture-mode "
            "sports, omit this to let GPT-4o guess topps vs. panini from "
            "the set_logo crop (the new default, per revised Decision #1) "
            "-- or pass 'topps'/'panini' explicitly to skip the guess and "
            "force a value."
        ),
    )
    parser.add_argument("--roboflow-model-id", default=card_vision.DEFAULT_MODEL_ID)
    parser.add_argument(
        "--roboflow-api-key", default=None,
        help="Defaults to the ROBOFLOW_API_KEY environment variable.",
    )
    parser.add_argument(
        "--openai-api-key", default=None,
        help="Defaults to the OPENAI_API_KEY environment variable.",
    )
    parser.add_argument("--confidence", type=float, default=0.25)
    parser.add_argument(
        "--save-crops", default=None,
        help="Optional folder to also save the exact crops sent to GPT-4o, for debugging.",
    )
    return parser.parse_args()


def resolve_card_type(args):
    """
    Uses --capture-mode (and --card-type if it was given) to get a starting
    card_type and where it came from:
      - "tcg"                       -> always "one_piece" (source "fixed_tcg")
      - "sports" + --card-type      -> that value (source "user_override")
      - "sports" with no card type  -> None for now (source "logo_guess"),
                                       guessed later from the logo
    Stops with an explanation if the options don't make sense, like passing
    --back with tcg (One Piece is front only).
    """
    if args.capture_mode == "tcg":
        if args.back:
            print(
                "--capture-mode tcg is front-only (One Piece has no back scan, per "
                "Dataset Collection) -- drop --back or switch to --capture-mode sports."
            )
            sys.exit(1)
        if args.card_type not in (None, "one_piece"):
            print(
                f"--capture-mode tcg only supports card_type 'one_piece' today -- "
                f"ignoring --card-type {args.card_type}."
            )
        return "one_piece", "fixed_tcg"

    # capture_mode == "sports"
    if args.card_type == "one_piece":
        print("--card-type one_piece isn't valid with --capture-mode sports.")
        sys.exit(1)
    if args.card_type in ("topps", "panini"):
        return args.card_type, "user_override"
    return None, "logo_guess"


def encode_image(image_bgr):
    import cv2
    ok, buffer = cv2.imencode(".jpg", image_bgr)
    if not ok:
        raise RuntimeError("Failed to encode a crop as JPEG.")
    return base64.b64encode(buffer).decode("utf-8")


def gather_side_crops(model, image_path, side, card_type, confidence, save_dir):
    import cv2

    image = cv2.imread(image_path)
    if image is None:
        print(f"Could not read image: {image_path}")
        sys.exit(1)

    boxes = card_vision.detect(model, image, confidence=confidence, card_type=card_type)
    best = card_vision.best_crop_per_class(image, boxes)

    if save_dir:
        side_dir = os.path.join(save_dir, side)
        os.makedirs(side_dir, exist_ok=True)
        for class_name, crop_img in best.items():
            cv2.imwrite(os.path.join(side_dir, f"{class_name}.jpg"), crop_img)

    return best  # dict: class_name -> cropped BGR image (numpy array)


def guess_card_type_from_logo(client, logo_crop_img):
    """
    For sports cards where I didn't pick the brand, guess Topps vs Panini from
    the logo crop. It's its own small GPT-4o call because it has to happen
    before the main call, which needs to know the card type.

    Returns "topps", "panini", or None if there's no logo crop or GPT-4o isn't
    sure. It's just a starting guess I can change. If it returns None, the
    caller should stop and ask for --card-type.
    """
    if logo_crop_img is None:
        return None

    schema = {
        "name": "card_type_guess",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"card_type": {"type": ["string", "null"]}},
            "required": ["card_type"],
            "additionalProperties": False,
        },
    }
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": CARD_TYPE_GUESS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Which brand is this set logo?"},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{encode_image(logo_crop_img)}"},
                    },
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )
    raw = json.loads(response.choices[0].message.content)
    guess = raw.get("card_type")
    # Only accept exactly "topps" or "panini". Anything else (null, a typo,
    # something random) counts as not sure.
    return guess if guess in ("topps", "panini") else None


def build_messages(front_crops, back_crops, card_type):
    fields = FIELDS_BY_CARD_TYPE[card_type]
    context_classes = CONTEXT_CLASSES_BY_CARD_TYPE.get(card_type, [])

    intro = f"This is a {card_type} card. Extract these fields: {', '.join(fields)}.\n"
    if context_classes:
        intro += (
            f"You'll also see image(s) labeled {', '.join(context_classes)} -- these are "
            "visual context only (e.g. the set's logo mark), not fields to output. Use them "
            "to help confirm set_name per the system instructions.\n"
        )
    intro += "Images below are each labeled by side and field."

    content = [{"type": "text", "text": intro}]

    any_images = False
    for side, crops in (("front", front_crops), ("back", back_crops or {})):
        for class_name in fields + context_classes:
            crop_img = crops.get(class_name)
            if crop_img is None:
                continue
            any_images = True
            label = f"{side.upper()} - {class_name}"
            if class_name in context_classes:
                label += " (context only, not an output field)"
            content.append({"type": "text", "text": f"{label}:"})
            content.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{encode_image(crop_img)}"},
            })

    if not any_images:
        print(
            "Warning: no crops were detected on either photo -- nothing useful to send to "
            "GPT-4o. Try crop_card_regions.py on these same photos first to debug detection."
        )

    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


# --- FULL CARD MODE ------------------------------------------------------
#
# Reads the whole card instead of the cropped fields. I added this because I
# couldn't keep using my detector without paying for Roboflow or the weights.
#
# It works because the detector never actually read anything. It just cropped
# the fields so they were easier to read. GPT-4o always did the reading, and
# everything after that (build_schema, merge_field, needs_review) only cares
# about the text. When I tested it on 17 of my cards it filled in 45 of 50
# fields for about half a cent a card. Every miss was the front and back not
# matching, and merge_field flagged those instead of guessing. The detector
# runs into the same thing.

# Phone photos are way bigger than needed, but shrinking them too much would
# make small text like a "44/99" serial unreadable. 1600px is the same size the
# hosted detector uses.
FULLCARD_MAX_EDGE_PIXELS = 1600

FULLCARD_SYSTEM_PROMPT = """You are reading printed text off photographs of a complete \
trading card. Each image is labeled with which side of the card it shows (front or back). \
You are seeing the ENTIRE card, so you must locate each requested field yourself.

Rules:
- Read exactly what is printed. Do not guess, autocomplete, or infer a value that is not \
clearly legible on the card.
- If a field is not present, is illegible, or is obscured by glare, return null for it \
rather than guessing. A null is a correct answer when the text cannot be read; an invented \
value is not.
- For "set_name", output ONLY the brand and set name (e.g. "Panini Prizm", "Topps Chrome", \
"Bowman Chrome"). Do NOT include the year, the sport (e.g. "Football", "Baseball"), the card \
number, or the player. If the card reads "2025 Panini - Prizm Football", extract just \
"Panini Prizm".
- For "card_number", output the number as printed, removing ONLY a leading label word or \
symbol such as "No.", "#", or "Card" (e.g. "No. 388" -> "388", "#44/99" -> "44/99"). Do NOT \
strip a set or series code that is part of the number -- a One Piece number printed as \
"OP01-024" must stay complete as "OP01-024" (never "024"), and a serial like "44/99" keeps \
both parts. When unsure whether something is a label or part of the number, keep it.
- "card_number" and "serial" are DIFFERENT FIELDS and must never be swapped. The card \
number is the card's position in the set checklist, usually printed on the BACK near the \
copyright text. The serial is a stamped limited print run, usually on the FRONT, written \
as a fraction like "9/25" (this card is number 9 of only 25 made).
- For "serial", output it exactly as printed, keeping both parts of the fraction and any \
prefix that is stamped with it (e.g. "9/25", "1/1", "FOTL 12/99"). Remove only a leading \
"#". If the run size is legible but the card's own number is not, output what you can read \
(e.g. "/25") rather than guessing the missing half.
- Most cards are NOT numbered. If there is no stamped print run anywhere on the side you \
are looking at, return null for "serial". Do not invent one, and never copy the card number \
into it.
- If a side shows only a serial and no card number, return null for card_number on that \
side rather than substituting the serial.
- The player name is the person featured on the card. On a One Piece card it is the \
character's name.
- Do not identify rarity or grade.
- If no image labeled "back" is provided, return null for every field under "back". Do not \
invent values.

"parallel" is the card's parallel / variation / finish -- the version of the card, not the \
card itself. Examples: "Silver Prizm", "Refractor", "Gold", "Camo", "Disco", "Wave", \
"X-Fractor", "Holo". You are seeing the whole card, so you may use its appearance.
- Give the parallel name only. Do NOT repeat the player, year, set or card number into it.
- If this is an ordinary BASE card with no special finish, return null. Base is not a \
parallel, and "Base" is not an acceptable answer.
- If you cannot name the parallel, return null. Do not describe the card instead -- \
"shiny", "silver-ish", "some kind of refractor" are all wrong answers; null is the right one.

"parallel_confidence" says HOW you know, and it must be exactly "high", "low", or null:
- "high" -- ONLY when the parallel's name is actually PRINTED somewhere on the card (many \
Topps Chrome backs print "REFRACTOR"; some parallels are named on the front). You read it.
- "low" -- when you inferred it from appearance: foil colour, pattern, border, texture, \
shine. Anything you judged rather than read is "low", however obvious it looks.
- null -- when "parallel" is null.
Be honest here. A "low" answer is useful and will be checked by a person. A "high" answer \
on something you did not actually read is the one outcome that causes real harm, because it \
will not be checked.

"category" is the ONE field that is not printed text. It is the sport or game the card \
belongs to, judged from the whole card -- the uniform, the equipment, the playing surface, \
the league marks, the artwork. Answer with EXACTLY one of these lowercase strings and \
nothing else:
basketball, football, baseball, hockey, soccer, one piece, pokemon, other.
- "football" means American football. A soccer card is "soccer".
- Use "other" only for a real trading card of some other sport or game.
- If you cannot tell confidently, return null. Null is a correct answer and the user will \
pick from a list; a wrong sport is stored without complaint and quietly corrupts every \
per-category report.
"""

FULLCARD_TYPE_GUESS_SYSTEM_PROMPT = """You are looking at a photograph of a complete \
trading card. Identify the manufacturer from its branding.

Rules:
- Only answer "topps" or "panini" if you can clearly recognize the brand.
- If the card is too blurry, cropped, or ambiguous to tell confidently, return null -- \
do not guess.
"""


def encode_full_image(image_bgr, max_edge=FULLCARD_MAX_EDGE_PIXELS):
    """Shrink the card photo to a normal size and base64 encode it."""
    import cv2

    h, w = image_bgr.shape[:2]
    scale = min(1.0, max_edge / max(h, w))
    if scale < 1.0:
        image_bgr = cv2.resize(
            image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
    return encode_image(image_bgr)


def build_fullcard_messages(front_image, back_image, card_type):
    """Same as build_messages, but with the whole card photos.

    Returns the same shape, so build_schema, merge_field and everything after
    it work the same.
    """
    fields = FIELDS_BY_CARD_TYPE[card_type]
    intro = (
        f"This is a {card_type} trading card. Extract these fields: "
        f"{', '.join(fields)}.\n"
        "You are shown the complete card; locate each field yourself."
    )
    content = [{"type": "text", "text": intro}]
    for side, image in (("front", front_image), ("back", back_image)):
        if image is None:
            continue
        content.append({"type": "text", "text": f"{side.upper()} of the card:"})
        content.append({
            "type": "image_url",
            "image_url": {
                "url": f"data:image/jpeg;base64,{encode_full_image(image)}",
                # "high" makes GPT look at the full detail instead of a small
                # thumbnail. Without it, small text like card numbers and
                # serials can't be read.
                "detail": "high",
            },
        })
    return [
        {"role": "system", "content": FULLCARD_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def guess_card_type_from_card(client, front_image):
    """Guess Topps vs Panini from the whole front, no detector.

    This replaces guess_card_type_from_logo in full card mode, since that one
    needs a logo crop from the detector. Works the same way: returns "topps",
    "panini", or None, and None means ask me instead of picking one. Back in
    July a Panini card got read as Topps because it just defaulted, so it
    shouldn't guess blind.
    """
    if front_image is None:
        return None

    schema = {
        "name": "card_type_guess",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"card_type": {"type": ["string", "null"]}},
            "required": ["card_type"],
            "additionalProperties": False,
        },
    }
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": FULLCARD_TYPE_GUESS_SYSTEM_PROMPT},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Which brand made this card?"},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{encode_full_image(front_image)}"
                        },
                    },
                ],
            },
        ],
        response_format={"type": "json_schema", "json_schema": schema},
    )
    raw = json.loads(response.choices[0].message.content)
    guess = raw.get("card_type")
    return guess if guess in ("topps", "panini") else None


def build_schema(card_type):
    fields = FIELDS_BY_CARD_TYPE[card_type]

    def side_schema():
        return {
            "type": "object",
            "properties": {f: {"type": ["string", "null"]} for f in fields},
            "required": fields,
            "additionalProperties": False,
        }

    return {
        "name": "card_ocr_result",
        "strict": True,
        "schema": {
            "type": "object",
            # category is at the top level on purpose, not inside front/back.
            #
            # Every other field is printed text, so there's a front reading and
            # a back reading and merge_field combines them. The sport isn't
            # printed, it's about the whole card, and a card only has one sport.
            # If it was per side, the two sides could "disagree" and get flagged
            # for no reason.
            #
            # It's part of the same call so it doesn't cost anything extra. A
            # second call like the brand guess would double the cost per scan.
            "properties": {
                "front": side_schema(),
                "back": side_schema(),
                "category": {"type": ["string", "null"]},
                # parallel and parallel_confidence. Also for the whole card, same
                # reason as category.
                #
                # The confidence isn't just "how sure are you". The prompt
                # defines it: "high" means the parallel name is printed on the
                # card, "low" means GPT guessed from the foil, color or pattern.
                # That's something it can actually answer, and it tells me if I
                # need to double check it.
                "parallel": {"type": ["string", "null"]},
                "parallel_confidence": {"type": ["string", "null"]},
            },
            # strict mode needs every field listed here, even the ones that can
            # be null.
            "required": [
                "front",
                "back",
                "category",
                "parallel",
                "parallel_confidence",
            ],
            "additionalProperties": False,
        },
    }


def normalize(value):
    if value is None:
        return None
    value = value.strip()
    return value or None


def _tokens(value):
    """
    The lowercase words in a reading, as a set. Used to tell if two readings
    are the same thing written differently (like "Prizm" vs "2025 Panini -
    Prizm Football", or "No. 338" vs "338"). Punctuation, spaces and word
    order get ignored, only the actual words matter.
    """
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def merge_field(front_val, back_val):
    """
    Combines the front and back reading for one field:
      - Same reading (ignoring caps) -> one value, nothing flagged.
      - One reading's words are all in the other -> same thing, one side
        just read more of it (like "Prizm" vs "2025 Panini - Prizm Football",
        or "44" cut off by glare vs "44/99"). Not a conflict. Keep the longer
        one so nothing gets lost. If they're the same words in a different
        order, keep the front.
      - Actually different words (like "Prizm" vs "Mosaic", or "44" vs "45")
        -> a real conflict. Doesn't pick one. Returns None plus both readings
        so it gets flagged for me to check.
    """
    f = normalize(front_val)
    b = normalize(back_val)
    if not (f and b):
        return (f or b), None

    if f.casefold() == b.casefold():
        return f, None

    ft, bt = _tokens(f), _tokens(b)
    # Only count them as matching if both sides have words and one side's
    # words are all in the other. If one is just punctuation, or each side has
    # words the other doesn't, treat it as a real conflict.
    if ft and bt and (ft <= bt or bt <= ft):
        return (b if len(bt) > len(ft) else f), None

    return None, {"front": f, "back": b}


def main():
    args = parse_args()

    roboflow_key = args.roboflow_api_key or os.environ.get("ROBOFLOW_API_KEY")
    if not roboflow_key:
        print("No Roboflow API key found. Pass --roboflow-api-key or set ROBOFLOW_API_KEY.")
        sys.exit(1)

    openai_key = args.openai_api_key or os.environ.get("OPENAI_API_KEY")
    if not openai_key:
        print(
            "No OpenAI API key found.\n"
            "Either pass --openai-api-key YOUR_KEY, or set it as an environment variable:\n\n"
            "    setx OPENAI_API_KEY your_key_here      (Windows -- then reopen your terminal)\n"
            "    export OPENAI_API_KEY=your_key_here    (Mac/Linux)\n\n"
            "Get a key at platform.openai.com -> API Keys. See README.md for the full walkthrough.\n"
        )
        sys.exit(1)

    try:
        from openai import OpenAI
    except ImportError:
        print(
            "The 'openai' package isn't installed.\n"
            "Install it first with:\n\n    pip install openai\n"
        )
        sys.exit(1)

    card_type, card_type_source = resolve_card_type(args)

    print(f"Loading model {args.roboflow_model_id} ...")
    model = card_vision.load_model(args.roboflow_model_id, roboflow_key)

    print(f"Detecting fields on front photo ({args.front}) ...")
    front_crops = gather_side_crops(
        model, args.front, "front", card_type, args.confidence, args.save_crops
    )
    print(f"  Found: {', '.join(front_crops) or '(nothing)'}")

    client = OpenAI(api_key=openai_key)

    if card_type is None:
        # Sports card with no brand picked, so guess Topps vs Panini from
        # the logo.
        print("Guessing card_type from the set_logo crop ...")
        guessed = guess_card_type_from_logo(client, front_crops.get("set_logo"))
        if guessed is None:
            print(
                "Couldn't confidently guess card_type from the set_logo crop (missing, "
                "blurry, or ambiguous). Per revised Decision #1 this falls back to a "
                "manual pick, same as the original design -- rerun with --card-type "
                "topps or --card-type panini."
            )
            sys.exit(1)
        card_type = guessed
        print(f"  Guessed card_type: {card_type} (from set_logo -- override anytime with --card-type)")

    back_crops = None
    if args.back:
        print(f"Detecting fields on back photo ({args.back}) ...")
        back_crops = gather_side_crops(
            model, args.back, "back", card_type, args.confidence, args.save_crops
        )
        print(f"  Found: {', '.join(back_crops) or '(nothing)'}")

    messages = build_messages(front_crops, back_crops, card_type)
    schema = build_schema(card_type)

    print("\nSending crops to GPT-4o ...")
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_schema", "json_schema": schema},
    )
    raw = json.loads(response.choices[0].message.content)

    fields = FIELDS_BY_CARD_TYPE[card_type]
    result = {"card_type": card_type, "card_type_source": card_type_source}
    needs_review = []
    conflicts = {}

    for field in fields:
        value, conflict = merge_field(raw.get("front", {}).get(field), raw.get("back", {}).get(field))
        result[field] = value
        if conflict:
            needs_review.append(field)
            conflicts[field] = conflict

    if needs_review:
        result["needs_review"] = needs_review
        result["conflicts"] = conflicts

    print("\n" + "=" * 60)
    print(json.dumps(result, indent=2))
    print("=" * 60)

    if card_type_source == "logo_guess":
        print(
            "\ncard_type was a default guessed from the set_logo crop, not manually "
            "confirmed -- double check it's right (rerun with --card-type topps/panini "
            "to override if not, per revised Decision #1)."
        )

    if needs_review:
        print(
            f"\n{len(needs_review)} field(s) need manual review because front and back "
            f"disagreed: {', '.join(needs_review)}. See 'conflicts' above for both readings."
        )

    missing = [f for f in fields if result.get(f) is None and f not in needs_review]
    if missing:
        print(f"\n{len(missing)} field(s) couldn't be read on either side: {', '.join(missing)}.")


if __name__ == "__main__":
    main()
