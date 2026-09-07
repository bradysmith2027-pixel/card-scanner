"""
vision/ — card detection + OCR, importable by the deployed API.

Moved here from the project root on 2026-09-06. They used to live one level
above backend/, which meant Railway (Root Directory = "backend") never shipped
them: /scan returned 503 because `import card_vision` failed in the container,
not because the CV dependencies were missing. Keeping them inside backend/ is
what makes scanning deployable at all.

Both modules still run as standalone CLI scripts:
    python backend/vision/ocr_card.py --capture-mode sports --front ... --back ...
"""
