from PIL import Image, ImageDraw, ImageFont
import os

lines = [
    ("$ ./venv/Scripts/python.exe ocr_card.py --capture-mode sports \\", "#c9d1d9"),
    ("      --front \"../WillHoward_front.jpeg\" --back \"../WillHoward_back.jpeg\"", "#c9d1d9"),
    ("", "#c9d1d9"),
    ("Detecting fields on front photo (../WillHoward_front.jpeg) ...", "#8b949e"),
    ("  Found: player_name, set_logo", "#8b949e"),
    ("Guessing card_type from the set_logo crop ...", "#8b949e"),
    ("  Guessed card_type: panini (from set_logo -- override anytime with --card-type)", "#8b949e"),
    ("Detecting fields on back photo (../WillHoward_back.jpeg) ...", "#8b949e"),
    ("  Found: year, set_name, card_number, player_name", "#8b949e"),
    ("", "#c9d1d9"),
    ("Sending crops to GPT-4o ...", "#8b949e"),
    ("", "#c9d1d9"),
    ("============================================================", "#58a6ff"),
    ("{", "#c9d1d9"),
    ('  "card_type": "panini",', "#7ee787"),
    ('  "card_type_source": "logo_guess",', "#7ee787"),
    ('  "year": "2025",', "#7ee787"),
    ('  "set_name": "Panini Prizm",', "#7ee787"),
    ('  "card_number": "367",', "#7ee787"),
    ('  "player_name": "WILL HOWARD"', "#7ee787"),
    ("}", "#c9d1d9"),
    ("============================================================", "#58a6ff"),
    ("", "#c9d1d9"),
    ("card_type was a default guessed from the set_logo crop, not manually", "#d29922"),
    ("confirmed -- double check it's right (rerun with --card-type topps/panini", "#d29922"),
    ("to override if not, per revised Decision #1).", "#d29922"),
]

font_path = None
for candidate in [
    r"C:\Windows\Fonts\consola.ttf",
    r"C:\Windows\Fonts\cour.ttf",
]:
    if os.path.exists(candidate):
        font_path = candidate
        break

font_size = 20
font = ImageFont.truetype(font_path, font_size) if font_path else ImageFont.load_default()

pad_x, pad_top, line_h = 28, 60, 28
width = 1180
height = pad_top + line_h * len(lines) + 30

img = Image.new("RGB", (width, height), "#0d1117")
draw = ImageDraw.Draw(img)

# Title bar
draw.rectangle([0, 0, width, 40], fill="#161b22")
for i, color in enumerate(["#ff5f56", "#ffbd2e", "#27c93f"]):
    draw.ellipse([20 + i * 26, 14, 32 + i * 26, 26], fill=color)
draw.text((width / 2 - 90, 10), "ocr_card.py — Terminal", font=font, fill="#8b949e")

y = pad_top
for text, color in lines:
    draw.text((pad_x, y), text, font=font, fill=color)
    y += line_h

img.save("../[C] WillHoward_ocr_terminal.png")
print("saved")
