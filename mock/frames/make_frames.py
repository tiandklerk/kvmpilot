"""Generate the canned test frames served by the mock kvmd.

Run once: python mock/frames/make_frames.py
Kept in-repo so the frames are reproducible without committing binaries blindly.
"""

from pathlib import Path

from PIL import Image, ImageDraw

HERE = Path(__file__).parent


def desktop() -> Image.Image:
    img = Image.new("RGB", (1280, 720), (48, 10, 36))  # aubergine-ish background
    d = ImageDraw.Draw(img)

    # top bar with clock
    d.rectangle([0, 0, 1280, 28], fill=(20, 20, 24))
    d.text((16, 8), "Activities", fill=(230, 230, 230))
    d.text((600, 8), "Jul 14  09:41", fill=(230, 230, 230))
    d.text((1200, 8), "100%", fill=(230, 230, 230))

    # dock on the left
    d.rectangle([0, 28, 64, 720], fill=(28, 28, 32))
    for i, color in enumerate([(233, 84, 32), (60, 120, 216), (46, 160, 67), (150, 90, 190)]):
        d.rounded_rectangle([12, 60 + i * 72, 52, 100 + i * 72], radius=8, fill=color)

    # terminal window
    d.rounded_rectangle([220, 120, 1060, 560], radius=10, fill=(30, 30, 34),
                        outline=(90, 90, 100), width=2)
    d.rectangle([220, 120, 1060, 152], fill=(50, 50, 58))
    d.text((240, 130), "user@mockhost: ~", fill=(220, 220, 220))
    lines = [
        "user@mockhost:~$ uname -a",
        "Linux mockhost 6.8.0-mock #1 SMP x86_64 GNU/Linux",
        "user@mockhost:~$ uptime",
        " 09:41:02 up 12 days,  3:07,  1 user,  load average: 0.14, 0.09, 0.05",
        "user@mockhost:~$ _",
    ]
    for i, line in enumerate(lines):
        d.text((240, 170 + i * 22), line, fill=(160, 220, 160))

    return img


if __name__ == "__main__":
    desktop().save(HERE / "desktop.jpg", quality=88)
    print(f"wrote {HERE / 'desktop.jpg'}")
