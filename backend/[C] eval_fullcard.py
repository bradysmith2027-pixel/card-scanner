"""
[C] eval_fullcard.py -- can GPT-4o read a WHOLE card, with no detector at all?

WHY THIS EXISTS (2026-09-15)
    /scan has been down since August. Every route back to a self-hosted detector
    is blocked: Roboflow hosted inference is out of credits (402), raw weight
    export needs a paid Core plan (Brady: not paying again), and retraining
    YOLOv8n in Colab lands on Ultralytics' AGPL-3.0 -- the exact licensing the
    project rejected on 2026-07-20 for a commercial networked app.

    So this asks the question that dissolves all three: DOES THE DETECTOR NEED
    TO EXIST? Its only job is cropping fields so OCR is easier. GPT-4o does the
    actual reading. `ocr_card.build_messages` takes a plain {label: image} dict
    and everything downstream -- schema, front/back merge, needs_review -- never
    touches a bounding box.

    If full-card reads well enough, Roboflow, torch, credits, ONNX and the AGPL
    question all get deleted rather than worked around.

WHAT THIS IS NOT
    ⚠️ NOT an A/B against the current pipeline. The YOLO path CANNOT RUN on this
    machine: `inference` is not installed (the venv was rebuilt from
    backend/requirements.txt, which deliberately has no torch) and the hosted
    endpoint 402s. Comparing would mean a 2-4 GB install. This measures the NEW
    path on its own and reports what it read; Brady judges against the real
    cards. "Is full-card good enough?" is the decision, not "is it better?"

    ⚠️ Re-uses ocr_card's FIELDS_BY_CARD_TYPE, build_schema, normalize and
    merge_field ON PURPOSE, so this tests the real downstream logic and not a
    parallel copy of it. Only the message-building changes.

COST
    One GPT-4o call per card (not per field). Prints token usage and a running
    estimate. Start with --limit 5 before spending anything real.

USAGE
    python "[C] eval_fullcard.py" --category one_piece --limit 5
    python "[C] eval_fullcard.py" --category all --limit 12 --json out.json
"""

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from vision.ocr_card import (  # noqa: E402
    FIELDS_BY_CARD_TYPE,
    build_fullcard_messages,
    build_schema,
    merge_field,
)

# Project root holds the dataset folders (Topps/, Panini/, One Piece/).
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CATEGORY_DIRS = {
    "topps": "Topps",
    "panini": "Panini",
    "one_piece": "One Piece",
}

# gpt-4o pricing per 1M tokens (USD). Update if the rate changes -- this is only
# used for a rough running total so nobody is surprised by the bill.
PRICE_IN_PER_M = 2.50
PRICE_OUT_PER_M = 10.00

# The only known text ground truth recorded anywhere in the vault, from the
# 2026-07-08 daily log. Auto-checked so at least two rows are objective.
KNOWN_TRUTH = {
    "op_001": {"card_number": "OP01-024"},
    "op_005": {"card_number": "OP03-102"},
}

# The prompt, image encoding and message building all live in
# vision/ocr_card.py and are IMPORTED above. This script deliberately keeps no
# copy of them, so what it measures is exactly what production runs.


def discover_cards(category, limit):
    """Return [(card_id, card_type, front_path, back_path|None), ...]."""
    folder = os.path.join(PROJECT_ROOT, CATEGORY_DIRS[category])
    if not os.path.isdir(folder):
        return []
    names = sorted(
        f for f in os.listdir(folder)
        if f.lower().endswith((".png", ".jpg", ".jpeg"))
    )
    cards = []
    if category == "one_piece":
        for n in names:
            cards.append((os.path.splitext(n)[0], "one_piece",
                          os.path.join(folder, n), None))
    else:
        fronts = [n for n in names if "_front" in n.lower()]
        for f in fronts:
            stem = f.lower().replace("_front", "")
            back = next(
                (n for n in names if n.lower().replace("_back", "") == stem
                 and "_back" in n.lower()),
                None,
            )
            cards.append((
                os.path.splitext(f)[0].replace("_front", ""),
                category,
                os.path.join(folder, f),
                os.path.join(folder, back) if back else None,
            ))
    return cards[:limit]


def read_card(client, card_type, front_path, back_path):
    # Calls the PRODUCTION message builder on decoded images, not a local copy
    # of the prompt. An eval that drifts from the code it measures is worse
    # than no eval -- the same "one definition" rule that profit.py exists for.
    import cv2

    front_img = cv2.imread(front_path)
    back_img = cv2.imread(back_path) if back_path else None
    if front_img is None:
        raise RuntimeError(f"Could not read image: {front_path}")
    messages = build_fullcard_messages(front_img, back_img, card_type)
    started = time.time()
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        response_format={"type": "json_schema", "json_schema": build_schema(card_type)},
    )
    elapsed = time.time() - started
    raw = json.loads(response.choices[0].message.content)

    # Same merge the real pipeline uses, so front/back agreement and the
    # needs_review flagging are exercised, not bypassed.
    merged, needs_review = {}, []
    for field in FIELDS_BY_CARD_TYPE[card_type]:
        value, conflict = merge_field(
            (raw.get("front") or {}).get(field),
            (raw.get("back") or {}).get(field),
        )
        merged[field] = value
        if conflict:
            needs_review.append(field)

    usage = response.usage
    # Keep the raw per-side readings. A merged NULL can mean two very different
    # things -- "neither side could read it" vs "both read it fine but printed
    # DIFFERENT numbers" -- and only the raw values distinguish them.
    return merged, needs_review, usage, elapsed, raw


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--category", default="one_piece",
                        choices=["topps", "panini", "one_piece", "all"])
    parser.add_argument("--limit", type=int, default=5,
                        help="Cards per category. Keep small -- each is a paid call.")
    parser.add_argument("--json", default=None, help="Write full results here.")
    args = parser.parse_args()

    if not os.environ.get("OPENAI_API_KEY"):
        print("OPENAI_API_KEY is not set. Aborting before spending anything.")
        sys.exit(1)

    from openai import OpenAI
    client = OpenAI()

    categories = (["topps", "panini", "one_piece"]
                  if args.category == "all" else [args.category])

    cards = []
    for c in categories:
        found = discover_cards(c, args.limit)
        if not found:
            print(f"  (no images found for {c})")
        cards.extend(found)

    if not cards:
        print("No cards found. Nothing to do.")
        sys.exit(1)

    print(f"\nReading {len(cards)} cards, FULL-CARD (no detector), gpt-4o.\n")
    print("=" * 78)

    results, tok_in, tok_out, checked, correct = [], 0, 0, 0, 0

    for card_id, card_type, front, back in cards:
        try:
            merged, needs_review, usage, elapsed, raw = read_card(
                client, card_type, front, back
            )
        except Exception as exc:
            print(f"\n{card_id} [{card_type}]  ERROR: {type(exc).__name__}: {exc}")
            results.append({"card_id": card_id, "error": str(exc)})
            continue

        tok_in += usage.prompt_tokens
        tok_out += usage.completion_tokens

        sides = "front+back" if back else "front only"
        print(f"\n{card_id}  [{card_type}, {sides}]  {elapsed:.1f}s")
        for field in FIELDS_BY_CARD_TYPE[card_type]:
            value = merged.get(field)
            shown = "NULL" if value in (None, "") else value
            flag = ""
            truth = KNOWN_TRUTH.get(card_id, {}).get(field)
            if truth is not None:
                checked += 1
                if str(value).strip().upper() == truth.upper():
                    correct += 1
                    flag = f"  <-- MATCHES known truth ({truth})"
                else:
                    flag = f"  <-- WRONG, expected {truth}"
            elif field in needs_review:
                fv = (raw.get("front") or {}).get(field)
                bv = (raw.get("back") or {}).get(field)
                flag = f"  <-- DISAGREE  front={fv!r}  back={bv!r}"
            print(f"    {field:14s} {shown}{flag}")
        results.append({
            "card_id": card_id, "card_type": card_type,
            "fields": merged, "needs_review": needs_review, "raw": raw,
        })

    cost = tok_in / 1_000_000 * PRICE_IN_PER_M + tok_out / 1_000_000 * PRICE_OUT_PER_M
    ok_cards = [r for r in results if "error" not in r]
    filled = sum(
        1 for r in ok_cards for v in r["fields"].values() if v not in (None, "")
    )
    total_fields = sum(len(r["fields"]) for r in ok_cards)

    print("\n" + "=" * 78)
    print(f"Cards read      : {len(ok_cards)}/{len(cards)}")
    print(f"Fields non-null : {filled}/{total_fields}"
          + (f"  ({filled / total_fields * 100:.0f}%)" if total_fields else ""))
    if checked:
        print(f"Known-truth     : {correct}/{checked} exact matches")
    print(f"Tokens          : {tok_in:,} in / {tok_out:,} out")
    print(f"Est. cost       : ${cost:.4f}  (~${cost / max(len(ok_cards), 1):.4f}/card)")
    print("=" * 78)
    # No emoji here on purpose: the Windows console is cp1252 and a stray
    # symbol crashed the whole run AFTER the results had been computed.
    print(
        "\nNOTE: a non-null rate is NOT accuracy -- a confidently wrong value counts\n"
        "      as filled. Check these against the real cards before trusting it.\n"
    )

    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(results, fh, indent=2)
        print(f"Wrote {args.json}")


if __name__ == "__main__":
    main()
