"""
vision/ has the card detection and OCR code that /scan uses.

These have to stay inside backend/. Railway only deploys the backend folder, so
when they were in the project root they never made it to the server and /scan
didn't work.

You can still run them on their own from the command line:
    python backend/vision/ocr_card.py --capture-mode sports --front ... --back ...
"""
