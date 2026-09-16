"""
ocr_card.py

WHAT THIS SCRIPT DOES (plain English):
  Give it a photo of the front of a card (and the back too, for Sports
  cards -- Topps/Panini), plus which capture mode it is: "sports" or
  "tcg". It finds the text regions with your trained model, crops them,
  sends all the crops to GPT-4o in ONE request, and gets back the card's
  year/set/number/player as structured data -- combining front and back
  into a single answer per the project's locked-in merge rules (Step 4
  Spec, Decision #5):
    - If front and back agree on a field, that's one value (not doubled --
      won't say "Ace Bailey Ace Bailey").
    - If front and back disagree, the field is left blank in the main
      result and flagged in "needs_review" instead of guessing which side
      is right. The actual confirmation screen is Step 6 (not built yet)
      -- for now this script just prints the conflict so you can see it.
    - Variation/rarity is never asked for (Decision #4) -- that's a
      separate user-driven dropdown step, not part of OCR.

CARD TYPE (revised Decision #1, 2026-07-08):
  You no longer have to know "topps" vs. "panini" up front. Just pick a
  capture mode:
    --capture-mode tcg     -> front-only, card_type is always "one_piece"
                               (the only TCG supported today).
    --capture-mode sports  -> front + back, card_type (topps vs. panini)
                               is GUESSED from the set_logo crop via
                               GPT-4o and used as a default.
  The guess is just a default, not an authoritative value -- pass
  --card-type topps/panini explicitly to skip the guess and force a
  value (same manual-override safety net every other OCR field already
  gets, per Risk #1). If the guess can't be made confidently (missing or
  unreadable set_logo crop), the script stops and asks you to rerun with
  --card-type set explicitly, rather than guessing blind.

HOW TO RUN IT:

  Sports card (Topps/Panini), let it guess card_type from the logo:
    python ocr_card.py --capture-mode sports --front "..\\Topps\\topps_001_front.png" --back "..\\Topps\\topps_001_back.png"

  Sports card, but force the card_type instead of guessing:
    python ocr_card.py --capture-mode sports --card-type panini --front "..\\Panini\\panini_001_front.png" --back "..\\Panini\\panini_001_back.png"

  TCG (One Piece, front only, per project spec):
    python ocr_card.py --capture-mode tcg --front "..\\One Piece\\op_001_front.png"

REQUIRES:
  ROBOFLOW_API_KEY (same one you already set up for crop_card_regions.py)
  OPENAI_API_KEY   (new -- see README.md for how to get one)
"""

import argparse
import base64
import json
import os
import re
import sys

try:
    # Normal case: imported as part of the `vision` package (the API does this).
    from . import card_vision
except ImportError:
    # Run directly as a CLI script, where there is no package context.
    import card_vision

# One Piece cards only fill card_type, card_number, player_name -- no
# year/set_name, per the project's Output Shape spec.
#
# "serial" added 2026-09-16 with migration 010. Until then the prompt correctly
# identified a serial and then returned null for it, because there was nowhere
# to put it -- the read was right and the value was thrown away.
#
# ⚠️ NOT added for one_piece. OP cards are not serial-numbered in the Topps /
# Panini sense, the Output Shape spec fixes their fields at two, and the
# 2026-09-15 evaluation measured One Piece at 10/10. Adding a field that is
# almost always null to the one card type that currently reads perfectly is
# pure downside. Revisit only if OP serials actually show up in inventory.
#
# ⚠️ DETECTOR MODE: there is no "serial" class in the Roboflow annotations, so
# build_messages simply finds no crop for it and sends none -- the schema still
# requires the key, and the model returns null. That is the honest outcome:
# the detector path never could read serials (confirmed 2026-09-15), and this
# makes that a visible null rather than a silent omission.
FIELDS_BY_CARD_TYPE = {
    "topps": ["year", "set_name", "card_number", "serial", "player_name"],
    "panini": ["year", "set_name", "card_number", "serial", "player_name"],
    "one_piece": ["card_number", "player_name"],
}

# The sport/game, inferred from the image rather than read off it (2026-09-16).
#
# WHY IT CAN BE DONE NOW: `category` is NOT NULL in the DB and the scanner
# could never detect it, so the confirm screen has always forced a manual pick
# and blocked submit until the user made one. That was correct while the
# DETECTOR drove scanning — it only ever sent five cropped text regions, and
# you cannot tell basketball from hockey from a crop of a card number.
# Full-card mode (9/15) sends the whole card, so the sport is simply visible.
#
# ⚠️ These MUST stay identical to CATEGORIES in the frontend's cardOptions.ts.
# `category` has no CHECK constraint (confirmed 8/18), so a bad value will NOT
# fail the insert — it will be silently stored and quietly fragment every
# per-category report. Unconstrained is more dangerous here, not less.
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

# Extra crops sent alongside the fields above purely as VISUAL CONTEXT --
# not text to transcribe, and not their own output field. set_logo is the
# set's visual logo mark (e.g. the Topps Chrome logo) -- it can help GPT-4o
# confirm/recognize the set_name even when the printed text itself is
# stylized, foil-glared, or otherwise hard to read cleanly. One Piece cards
# were never annotated with a set_logo class, so there's nothing to add
# there. This same set_logo crop also drives the card_type guess below.
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

# Used only for the card_type guess (revised Decision #1) -- a separate,
# narrower GPT-4o call over just the set_logo crop, before the main
# multi-field OCR call runs.
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
    Turns --capture-mode (+ optional --card-type override) into a starting
    card_type and where it came from, per revised Decision #1:
      - capture_mode "tcg"    -> always "one_piece" (source "fixed_tcg")
      - capture_mode "sports" + explicit --card-type topps/panini
                               -> that value (source "user_override")
      - capture_mode "sports" with no override
                               -> None for now (source "logo_guess"),
                                  resolved later from the set_logo crop
    Exits with an explanation if the combination doesn't make sense (e.g.
    --back with --capture-mode tcg, which is front-only per project spec).
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
    Revised Decision #1: for Sports captures without an explicit
    --card-type override, guess Topps vs. Panini from the set_logo crop
    alone, in its own small GPT-4o call (separate from the main batched
    OCR call, since it has to happen before we know which card_type to
    build the main call's fields/schema around).

    Returns "topps", "panini", or None if there's no logo crop to check
    or GPT-4o isn't confident. This is meant to be a DEFAULT the user can
    override, not an authoritative value -- the caller is responsible for
    stopping and asking for a manual --card-type if this returns None,
    same failure-handling pattern as every other OCR field (Risk #1).
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
    # Belt-and-suspenders: only trust exactly "topps" or "panini". Anything
    # else (null, a typo, an unexpected string) is treated as "not confident"
    # rather than trusting it blindly.
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


# --- FULL-CARD MODE (added 2026-09-15) ------------------------------------
#
# Reads the WHOLE card instead of detector-cropped fields. This exists because
# every route back to self-hosted weights was blocked: hosted inference is out
# of credits (402), raw weight export requires a paid Core plan, and retraining
# YOLOv8n lands on Ultralytics' AGPL-3.0 -- rejected 2026-07-20 as "risky for a
# commercial network app".
#
# It works because the detector never did the reading. Its only job was cropping
# so OCR was easier; GPT-4o always did the identification, and everything
# downstream (build_schema, merge_field, needs_review) operates on text, not on
# boxes. Measured 2026-09-15 over 17 dataset cards: 45/50 fields populated,
# 2/2 exact matches against the only known ground truth (OP01-024, OP03-102),
# ~$0.005/card. Every miss was a front/back DISAGREEMENT that merge_field
# correctly refused to resolve -- never a wrong value written silently -- and
# those conflicts are downstream of detection, so the detector path hits them
# identically.

# Phone photos are far larger than the model needs, but shrinking too
# aggressively is exactly what would destroy small print like a "44/99" serial.
# 1600px matches the cap the hosted-detection path already used.
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
    """Base64-encode a whole-card photo, downscaled to a sane upload size."""
    import cv2

    h, w = image_bgr.shape[:2]
    scale = min(1.0, max_edge / max(h, w))
    if scale < 1.0:
        image_bgr = cv2.resize(
            image_bgr, (int(w * scale), int(h * scale)), interpolation=cv2.INTER_AREA
        )
    return encode_image(image_bgr)


def build_fullcard_messages(front_image, back_image, card_type):
    """Same contract as build_messages, but from whole-card photos.

    Returns the identical message shape, so build_schema / merge_field and the
    rest of the pipeline are untouched.
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
                # "high" forces full tiling instead of one downsampled thumbnail.
                # Without it, small print (card numbers, serials) is not
                # resolvable and reads would fail for the wrong reason.
                "detail": "high",
            },
        })
    return [
        {"role": "system", "content": FULLCARD_SYSTEM_PROMPT},
        {"role": "user", "content": content},
    ]


def guess_card_type_from_card(client, front_image):
    """Topps vs. Panini from the whole front, with no detector.

    Replaces guess_card_type_from_logo in full-card mode, which needed a
    set_logo crop that only a detector could produce. Same contract: returns
    "topps", "panini", or None, and None means "ask the user" rather than
    "pick one" -- the 2026-07-08 Panini-reads-as-Topps bug came from defaulting
    blind, and that lesson holds regardless of how the image is framed.
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
            # `category` sits at the TOP LEVEL, deliberately not inside
            # front/back (added 2026-09-16).
            #
            # Every other field is printed text, so it has a front reading and a
            # back reading and `merge_field` reconciles them. The sport is not
            # printed text — it is a judgement about the whole object, and one
            # physical card has exactly one sport. Putting it per-side would
            # invent a disagreement that cannot exist and send it to manual
            # review for no reason.
            #
            # It rides along in the SAME call, so it costs nothing extra. A
            # second request (the way brand detection works in
            # guess_card_type_from_card) would double the per-scan price.
            "properties": {
                "front": side_schema(),
                "back": side_schema(),
                "category": {"type": ["string", "null"]},
                # `parallel` + `parallel_confidence` (2026-09-16). Also whole-
                # card, for the same reason as category.
                #
                # The confidence is NOT a subjective score — the prompt defines
                # it objectively: "high" means the parallel's name is PRINTED on
                # the card, "low" means it was inferred from foil, colour or
                # pattern. That is a question a model can answer reliably,
                # unlike "how sure are you?", and it maps straight onto whether
                # the value can be trusted without a human looking.
                "parallel": {"type": ["string", "null"]},
                "parallel_confidence": {"type": ["string", "null"]},
            },
            # strict mode requires every property to be listed here, including
            # the nullable ones.
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
    The lowercased alphanumeric "words" in a reading, as a set. Used to
    tell whether two readings describe the same thing worded differently
    (e.g. "Prizm" vs "2025 Panini - Prizm Football", or "No. 338" vs
    "338"). Punctuation, spacing, the em-dash, and word order all drop
    out -- only the actual words matter for the comparison.
    """
    return set(re.findall(r"[a-z0-9]+", value.casefold()))


def merge_field(front_val, back_val):
    """
    Decision #5's merge rule, made smarter about COMPATIBLE readings so we
    stop flagging non-conflicts:
      - Exact (case-insensitive) match  -> one value, no review.
      - One reading's words are a subset of the other's -> they're the
        SAME THING, one side just read more of the label than the other
        (e.g. front "Prizm" vs back "2025 Panini - Prizm Football", or a
        glare-clipped "44" vs the full "44/99"). Not a conflict. Keep the
        more complete reading (the one with more words) -- it never loses
        information, and it's the right call for partial reads like the
        card_number example above. Ties (same words, different order) keep
        the front.
      - Genuinely different words on each side (e.g. "Prizm" vs "Mosaic",
        or "44" vs "45") -> still a real conflict. NOT auto-resolved --
        return None plus both candidates so the caller flags it for manual
        review, exactly as before.
    """
    f = normalize(front_val)
    b = normalize(back_val)
    if not (f and b):
        return (f or b), None

    if f.casefold() == b.casefold():
        return f, None

    ft, bt = _tokens(f), _tokens(b)
    # Only treat as compatible when both sides actually have words AND one
    # word-set contains the other. If a reading is all punctuation (no
    # tokens), or each side has words the other lacks, fall through to the
    # real-conflict path rather than silently merging.
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
        # Sports capture, no explicit override -- guess topps vs. panini
        # from the set_logo crop (revised Decision #1).
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
