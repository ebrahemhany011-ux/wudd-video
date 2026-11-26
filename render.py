# render.py v9.5 - Wudd explainer video renderer (continuous crossfades, icons, topic colours, natural pacing) (runs on a GitHub Actions runner, public repo safe).
# Input: env DATA = base64(iv[12] + AES-GCM ciphertext) of a JSON payload, key = SHA-256(CALLBACK_SECRET).
# Nothing readable (script, upload URL, job id) is ever in the workflow inputs or in the logs.
import os, re, sys, json, glob, math, base64, hashlib, asyncio, subprocess, textwrap, colorsys, wave, time
import requests, edge_tts, arabic_reshaper
try:
    from bidi import get_display
except ImportError:
    from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont, ImageFilter
import numpy as np

W, H, FPS = 1080, 1920, 24
NAME_SHOWN = "وَدّ"      # on screen: fatha on the waw
NAME_SPOKEN = "وِدّ"     # spoken: kasra

CREAM = (245, 240, 228)
BG_TOP = BG_BOT = GOLD = TEAL_D = CARD = LINE = PANEL = HEAD = GLOW = (0, 0, 0)
NAME_RE = re.compile(r"(?<![\u0621-\u064A])\u0648[\u064B-\u0652]*\u062F[\u064B-\u0652]*(?![\u0621-\u064A])")
EMOJI_RE = re.compile("[\U0001F000-\U0001FAFF\u2600-\u27BF\uFE0F\u200d]")
AR_RE = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")

RESHAPER = arabic_reshaper.ArabicReshaper(configuration={"delete_harakat": False, "support_ligatures": True})


def find_font(patterns):
    for p in patterns:
        g = sorted(glob.glob(p, recursive=True))
        if g:
            return g[0]
    raise RuntimeError("font not found: " + patterns[0])


AR_FONT = find_font(["/usr/share/fonts/**/NotoSansArabic-Bold.ttf", "/usr/share/fonts/**/NotoNaskhArabic-Bold.ttf",
                     "/usr/share/fonts/**/NotoSansArabic*.ttf"])
LAT_FONT = find_font(["/usr/share/fonts/**/NotoSans-Bold.ttf", "/usr/share/fonts/**/DejaVuSans-Bold.ttf"])
_fc = {}


def fnt(path, size):
    k = (path, size)
    if k not in _fc:  # BASIC layout: we do bidi ourselves, raqm would reorder a second time
        _fc[k] = ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.BASIC)
    return _fc[k]


def clean(t, limit):
    t = EMOJI_RE.sub("", str(t or "")).replace("\u200f", "").replace("\u200e", "")
    t = re.sub(r"[*#`>_~]+", "", t)
    return re.sub(r"\s+", " ", t).strip()[:limit]


def on_screen(t):
    return NAME_RE.sub(NAME_SHOWN, t)


def spoken(t):
    return NAME_RE.sub(NAME_SPOKEN, t)


# ---------- text drawing (Arabic shaping + RTL + Latin fallback) ----------
def shape(word):
    return get_display(RESHAPER.reshape(word))


def runs_of(visual):
    out, cur, cur_ar = [], "", None
    for ch in visual:
        if ch == " ":
            is_ar = cur_ar if cur_ar is not None else True
        else:
            is_ar = bool(AR_RE.match(ch)) or ch in "\u061f\u060c\u061b"
        if cur_ar is None or is_ar == cur_ar:
            cur += ch; cur_ar = is_ar
        else:
            out.append((cur, cur_ar)); cur, cur_ar = ch, is_ar
    if cur:
        out.append((cur, cur_ar))
    return out


def word_img(word, size, fill):
    """One word as an RGBA layer (RGB = fill everywhere, alpha = glyphs)."""
    visual = shape(word)
    fa, fl = fnt(AR_FONT, size), fnt(LAT_FONT, int(size * 0.92))
    runs = runs_of(visual)
    wid = sum(ImageDraw.Draw(Image.new("L", (1, 1))).textlength(r, font=(fa if ar else fl)) for r, ar in runs)
    img = Image.new("RGBA", (int(wid) + 8, int(size * 1.9)), tuple(fill) + (0,))
    d = ImageDraw.Draw(img)
    x, base = 4, int(size * 1.3)
    for r, ar in runs:
        f = fa if ar else fl
        d.text((x, base), r, font=f, fill=tuple(fill) + (255,), anchor="ls")
        x += d.textlength(r, font=f)
    return img


def layout(text, size, maxw, fill, line_h=None, gap=None):
    """Words laid out right-to-left. Returns (items[(img,x,y)], width, height)."""
    line_h = line_h or int(size * 1.45)
    gap = gap if gap is not None else int(size * 0.28)
    toks = []
    for w in text.split():                       # keep runs of Latin-only words together (left-to-right)
        if toks and not AR_RE.search(w) and not AR_RE.search(toks[-1]) and re.search(r"[A-Za-z0-9]", w + toks[-1]):
            toks[-1] += " " + w
        else:
            toks.append(w)
    words = [(w, word_img(w, size, fill)) for w in toks]
    lines, cur, cw = [], [], 0
    for w, im in words:
        add = im.width + (gap if cur else 0)
        if cur and cw + add > maxw:
            lines.append(cur); cur, cw = [], 0; add = im.width
        cur.append(im); cw += add
    if cur:
        lines.append(cur)
    widths = [sum(i.width for i in ln) + gap * (len(ln) - 1) for ln in lines]
    bw = max(widths) if widths else 1
    items = []
    for li, ln in enumerate(lines):
        x = (bw + widths[li]) / 2
        for im in ln:
            x -= im.width
            items.append((im, int(x), li * line_h))
            x -= gap
    return items, int(bw), max(1, len(lines)) * line_h


def ease(p):
    p = max(0.0, min(1.0, p)); return 1 - (1 - p) ** 3


def back(p):
    p = max(0.0, min(1.0, p)); c = 1.70158
    return 1 + (c + 1) * (p - 1) ** 3 + c * (p - 1) ** 2


def put(frame, layer, x, y, alpha=1.0):
    if alpha <= 0.01:
        return
    if alpha >= 0.99:
        frame.paste(layer, (int(x), int(y)), layer)
    else:
        m = layer.getchannel("A").point(lambda v: int(v * alpha))
        frame.paste(layer, (int(x), int(y)), m)


def put_words(frame, items, ox, oy, t, t0, span, rise=26):
    n = len(items)
    for k, (im, x, y) in enumerate(items):
        tw = t0 + (k / max(1, n)) * span
        p = (t - tw) / 0.28
        if p <= 0:
            continue
        put(frame, im, ox + x, oy + y + (1 - ease(p)) * rise, min(1.0, p))


# ---------- palette (follows the topic / customer request) ----------
# (keywords, hue 0-1)
THEMES = [
    (r"مصروف|فلوس|مال|ميزاني|ادخار|حساب|بنك|استثمار|اقتصاد|finance|money|budget|bank", 0.38),
    (r"صح|طب|دكتور|عياد|علاج|تمرين|رياض|جيم|دواء|health|medical|fitness|gym", 0.52),
    (r"اكل|أكل|مطعم|طبخ|وصف|قهو|حلو|كافيه|food|restaurant|cafe|coffee", 0.03),
    (r"موضه|موضة|جمال|مكياج|عطور|ملابس|تجميل|beauty|fashion|style", 0.9),
    (r"سفر|رحل|سياح|فندق|طيران|travel|trip|hotel", 0.57),
    (r"تعليم|مذاكر|كورس|مدرس|امتحان|طلاب|دراس|education|study|course", 0.62),
    (r"تقني|برمج|ذكاء|موقع|تطبيق|ai|tech|software|app|code", 0.7),
    (r"طبيع|زراع|بيئ|نبات|شجر|green|nature|eco", 0.3),
    (r"عقار|شقة|بيت|ديكور|home|house", 0.08),
    (r"مشروع|شركة|مبيعات|تسويق|عميل|بيع|business|sales|marketing", 0.6),
]


def hsv(h, s, v, a=None):
    r, g, b = colorsys.hsv_to_rgb(h % 1.0, max(0, min(1, s)), max(0, min(1, v)))
    c = (int(r * 255), int(g * 255), int(b * 255))
    return c if a is None else c + (a,)


def hex_hue(hx):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(hx or "").strip())
    if not m:
        return None
    v = m.group(1)
    r, g, b = [int(v[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    h, s, _ = colorsys.rgb_to_hsv(r, g, b)
    return h if s > 0.12 else None


def set_palette(hue):
    global BG_TOP, BG_BOT, GOLD, TEAL_D, CARD, LINE, PANEL, HEAD, GLOW
    BG_TOP, BG_BOT = hsv(hue, 0.78, 0.40), hsv(hue + 0.05, 0.90, 0.13)
    warm = 0.08 < (hue % 1.0) < 0.2
    GOLD = hsv(0.52, 0.45, 1.0) if warm else hsv(0.115, 0.58, 1.0)     # accent: warm gold, or cyan on warm themes
    TEAL_D = hsv(hue, 0.9, 0.22)
    CARD = hsv(hue, 0.85, 0.16)
    LINE = hsv(hue, 0.55, 0.52)
    PANEL = hsv(hue, 0.9, 0.11)
    HEAD = hsv(hue, 0.8, 0.26)
    GLOW = hsv(hue, 0.7, 0.7)


def hex_rgb(hx):
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", str(hx or "").strip())
    if not m:
        return None
    v = m.group(1)
    return tuple(int(v[i:i + 2], 16) / 255 for i in (0, 2, 4))


def set_palette_colors(main_hx, accent_hx):
    """Exact customer colours: background = shades of `main` (any colour incl. black/grey),
    accent = bright colour for titles/icons. Backgrounds stay dark-toned so white text stays readable."""
    global BG_TOP, BG_BOT, GOLD, TEAL_D, CARD, LINE, PANEL, HEAD, GLOW
    mr, ar_ = hex_rgb(main_hx), hex_rgb(accent_hx)
    if mr is None:
        return False
    h, s, v = colorsys.rgb_to_hsv(*mr)
    if v > 0.85 and s < 0.2:               # white/very light main: cannot be a background, keep a charcoal one
        v, s = 0.25, 0.0
    sat = min(0.95, s)
    k = 0.55 + 0.45 * min(1.0, v / 0.6)      # darker input -> darker background
    BG_TOP, BG_BOT = hsv(h, sat * 0.9, 0.40 * k), hsv(h + 0.02, min(1, sat), 0.13 * k)
    if ar_ is not None:
        ah, as_, av = colorsys.rgb_to_hsv(*ar_)
        GOLD = hsv(ah, as_, max(av, 0.82)) if av < 0.82 and as_ > 0.05 else tuple(int(c * 255) for c in ar_)
        if av < 0.3 and as_ < 0.2:
            GOLD = (235, 235, 235)
    else:
        GOLD = hsv(h, max(0.0, sat * 0.5), 1.0) if sat > 0.1 else (235, 235, 235)
    TEAL_D = hsv(h, sat, 0.22 * k)
    CARD = hsv(h, sat * 0.9, 0.16 * k)
    LINE = hsv(h, sat * 0.6, 0.52 * k + 0.1)
    PANEL = hsv(h, sat, 0.11 * k)
    HEAD = hsv(h, sat * 0.85, 0.26 * k)
    GLOW = hsv(h, sat * 0.7, 0.7 * k + 0.1)
    return True


def pick_hue(plan_meta, text):
    h = hex_hue((plan_meta or {}).get("main")) if isinstance(plan_meta, dict) else None
    if h is not None:
        return h
    for pat, hue in THEMES:
        if re.search(pat, text, re.I):
            return hue
    return 0.5   # default Wudd teal


# ---------- icons (vector-style, drawn at 4x then downscaled) ----------
ICONS = {}


def _icon(name):
    def deco(fn):
        ICONS[name] = fn
        return fn
    return deco


@_icon("check")
def _i_check(d, fg, bg):
    d.line([(24, 52), (43, 71), (77, 32)], fill=fg, width=11, joint="curve")


@_icon("chat")
def _i_chat(d, fg, bg):
    d.rounded_rectangle([16, 20, 84, 64], 12, fill=fg)
    d.polygon([(30, 60), (28, 84), (52, 62)], fill=fg)
    for x in (34, 50, 66):
        d.ellipse([x - 5, 37, x + 5, 47], fill=bg)


@_icon("clock")
def _i_clock(d, fg, bg):
    d.ellipse([18, 18, 82, 82], outline=fg, width=8)
    d.line([(50, 50), (50, 30)], fill=fg, width=7)
    d.line([(50, 50), (66, 59)], fill=fg, width=7)


@_icon("money")
def _i_money(d, fg, bg):
    d.ellipse([16, 16, 84, 84], fill=fg)
    d.line([(50, 28), (50, 72)], fill=bg, width=6)
    d.arc([36, 32, 64, 52], 90, 360, fill=bg, width=6)
    d.arc([36, 48, 64, 68], 270, 180 + 360, fill=bg, width=6)


@_icon("chart")
def _i_chart(d, fg, bg):
    for x0, y0 in ((20, 56), (42, 40), (64, 22)):
        d.rounded_rectangle([x0, y0, x0 + 16, 80], 4, fill=fg)
    d.line([(18, 38), (40, 26), (56, 30), (80, 12)], fill=fg, width=5)


@_icon("heart")
def _i_heart(d, fg, bg):
    d.ellipse([20, 22, 52, 54], fill=fg)
    d.ellipse([48, 22, 80, 54], fill=fg)
    d.polygon([(22, 44), (78, 44), (50, 80)], fill=fg)


@_icon("phone")
def _i_phone(d, fg, bg):
    d.rounded_rectangle([32, 14, 68, 86], 9, fill=fg)
    d.rounded_rectangle([37, 22, 63, 70], 3, fill=bg)
    d.ellipse([45, 74, 55, 84], fill=bg)


@_icon("bell")
def _i_bell(d, fg, bg):
    d.pieslice([27, 18, 73, 64], 180, 360, fill=fg)
    d.polygon([(27, 41), (73, 41), (80, 68), (20, 68)], fill=fg)
    d.ellipse([42, 70, 58, 86], fill=fg)


@_icon("shield")
def _i_shield(d, fg, bg):
    d.polygon([(50, 14), (80, 25), (77, 57), (50, 86), (23, 57), (20, 25)], fill=fg)
    d.line([(36, 48), (47, 60), (66, 36)], fill=bg, width=8, joint="curve")


@_icon("star")
def _i_star(d, fg, bg):
    pts = []
    for k in range(10):
        r = 38 if k % 2 == 0 else 16
        a = -math.pi / 2 + k * math.pi / 5
        pts.append((50 + r * math.cos(a), 54 + r * math.sin(a)))
    d.polygon(pts, fill=fg)


@_icon("bolt")
def _i_bolt(d, fg, bg):
    d.polygon([(58, 12), (28, 54), (47, 54), (40, 88), (72, 44), (53, 44)], fill=fg)


@_icon("users")
def _i_users(d, fg, bg):
    d.ellipse([27, 24, 49, 46], fill=fg)
    d.ellipse([55, 28, 75, 48], fill=fg)
    d.pieslice([14, 50, 62, 100], 180, 360, fill=fg)
    d.pieslice([46, 52, 86, 100], 180, 360, fill=fg)


@_icon("gift")
def _i_gift(d, fg, bg):
    d.rectangle([22, 44, 78, 82], fill=fg)
    d.rectangle([17, 32, 83, 46], fill=fg)
    d.rectangle([46, 32, 54, 82], fill=bg)
    d.ellipse([30, 14, 50, 34], outline=fg, width=6)
    d.ellipse([50, 14, 70, 34], outline=fg, width=6)


@_icon("cart")
def _i_cart(d, fg, bg):
    d.line([(14, 22), (28, 22), (36, 58), (76, 58)], fill=fg, width=7, joint="curve")
    d.polygon([(30, 32), (84, 32), (76, 54), (36, 54)], fill=fg)
    d.ellipse([36, 68, 50, 82], fill=fg)
    d.ellipse([64, 68, 78, 82], fill=fg)


@_icon("book")
def _i_book(d, fg, bg):
    d.polygon([(48, 28), (48, 80), (16, 74), (16, 22)], fill=fg)
    d.polygon([(52, 28), (52, 80), (84, 74), (84, 22)], fill=fg)
    for y in (38, 50, 62):
        d.line([(24, y), (40, y + 2)], fill=bg, width=4)


@_icon("calendar")
def _i_calendar(d, fg, bg):
    d.rounded_rectangle([18, 24, 82, 84], 9, fill=fg)
    d.rectangle([18, 24, 82, 40], fill=fg)
    d.rounded_rectangle([32, 14, 40, 30], 3, fill=fg)
    d.rounded_rectangle([60, 14, 68, 30], 3, fill=fg)
    for r in range(2):
        for c in range(3):
            d.rounded_rectangle([28 + c * 16, 48 + r * 15, 38 + c * 16, 58 + r * 15], 2, fill=bg)


@_icon("pin")
def _i_pin(d, fg, bg):
    d.ellipse([27, 14, 73, 60], fill=fg)
    d.polygon([(30, 50), (70, 50), (50, 88)], fill=fg)
    d.ellipse([41, 28, 59, 46], fill=bg)


@_icon("mail")
def _i_mail(d, fg, bg):
    d.rounded_rectangle([14, 26, 86, 74], 8, fill=fg)
    d.line([(18, 32), (50, 56), (82, 32)], fill=bg, width=6, joint="curve")


@_icon("food")
def _i_food(d, fg, bg):
    d.pieslice([18, 30, 82, 94], 0, 180, fill=fg)
    d.rectangle([14, 56, 86, 62], fill=fg)
    d.ellipse([44, 22, 56, 34], fill=fg)
    d.line([(30, 78), (70, 78)], fill=bg, width=3)


@_icon("leaf")
def _i_leaf(d, fg, bg):
    d.polygon([(18, 78), (22, 40), (44, 18), (80, 14), (78, 52), (56, 78), (34, 82)], fill=fg)
    d.line([(24, 76), (64, 32)], fill=bg, width=5)


@_icon("gear")
def _i_gear(d, fg, bg):
    for k in range(8):
        a = k * math.pi / 4
        cx, cy = 50 + 30 * math.cos(a), 50 + 30 * math.sin(a)
        px, py = -math.sin(a) * 7, math.cos(a) * 7
        qx, qy = math.cos(a) * 9, math.sin(a) * 9
        d.polygon([(cx - px - qx, cy - py - qy), (cx + px - qx, cy + py - qy), (cx + px + qx, cy + py + qy), (cx - px + qx, cy - py + qy)], fill=fg)
    d.ellipse([22, 22, 78, 78], fill=fg)
    d.ellipse([39, 39, 61, 61], fill=bg)


@_icon("bulb")
def _i_bulb(d, fg, bg):
    d.ellipse([27, 12, 73, 58], fill=fg)
    d.polygon([(36, 50), (64, 50), (58, 68), (42, 68)], fill=fg)
    d.rounded_rectangle([40, 70, 60, 77], 3, fill=fg)
    d.rounded_rectangle([43, 80, 57, 86], 3, fill=fg)


@_icon("doc")
def _i_doc(d, fg, bg):
    d.polygon([(24, 12), (60, 12), (78, 30), (78, 88), (24, 88)], fill=fg)
    for y in (46, 58, 70):
        d.line([(34, y), (68, y)], fill=bg, width=5)


@_icon("search")
def _i_search(d, fg, bg):
    d.ellipse([18, 16, 64, 62], outline=fg, width=9)
    d.line([(58, 58), (82, 82)], fill=fg, width=11)


@_icon("rocket")
def _i_rocket(d, fg, bg):
    d.polygon([(50, 10), (66, 36), (66, 68), (34, 68), (34, 36)], fill=fg)
    d.polygon([(34, 50), (18, 74), (34, 68)], fill=fg)
    d.polygon([(66, 50), (82, 74), (66, 68)], fill=fg)
    d.ellipse([42, 34, 58, 50], fill=bg)
    d.polygon([(42, 72), (58, 72), (50, 90)], fill=fg)


@_icon("lock")
def _i_lock(d, fg, bg):
    d.arc([32, 14, 68, 58], 180, 360, fill=fg, width=8)
    d.line([(34, 36), (34, 50)], fill=fg, width=8)
    d.line([(66, 36), (66, 50)], fill=fg, width=8)
    d.rounded_rectangle([24, 44, 76, 86], 9, fill=fg)
    d.ellipse([44, 56, 56, 68], fill=bg)


@_icon("smile")
def _i_smile(d, fg, bg):
    d.ellipse([14, 14, 86, 86], fill=fg)
    d.ellipse([34, 36, 44, 48], fill=bg)
    d.ellipse([56, 36, 66, 48], fill=bg)
    d.arc([32, 40, 68, 72], 20, 160, fill=bg, width=6)


@_icon("warning")
def _i_warning(d, fg, bg):
    d.polygon([(50, 12), (90, 82), (10, 82)], fill=fg)
    d.rounded_rectangle([46, 36, 54, 62], 3, fill=bg)
    d.ellipse([45, 67, 55, 77], fill=bg)


@_icon("trophy")
def _i_trophy(d, fg, bg):
    d.polygon([(28, 16), (72, 16), (66, 54), (34, 54)], fill=fg)
    d.arc([10, 20, 36, 46], 90, 270, fill=fg, width=6)
    d.arc([64, 20, 90, 46], 270, 90, fill=fg, width=6)
    d.rectangle([45, 54, 55, 70], fill=fg)
    d.rounded_rectangle([30, 70, 70, 82], 4, fill=fg)


@_icon("wallet")
def _i_wallet(d, fg, bg):
    d.rounded_rectangle([14, 26, 86, 80], 11, fill=fg)
    d.rounded_rectangle([56, 44, 90, 64], 8, fill=bg)
    d.ellipse([66, 49, 76, 59], fill=fg)


@_icon("video")
def _i_video(d, fg, bg):
    d.rounded_rectangle([12, 28, 62, 72], 9, fill=fg)
    d.polygon([(66, 46), (88, 32), (88, 68), (66, 54)], fill=fg)


@_icon("sparkle")
def _i_sparkle(d, fg, bg):
    d.polygon([(50, 10), (59, 41), (90, 50), (59, 59), (50, 90), (41, 59), (10, 50), (41, 41)], fill=fg)


ICON_KEYS = [
    (r"فلوس|مصروف|سعر|دفع|مال|تكلف|ربح|رخيص|خصم|coin|money|price|pay", "money"),
    (r"محفظ|ميزاني|ادخار|wallet|budget", "wallet"),
    (r"وقت|ساعة|سريع|ثواني|دقيق|مواعيد|تأخير|time|fast|quick", "clock"),
    (r"رسال|شات|محادث|واتساب|رد |ردود|تواصل|chat|message|whatsapp", "chat"),
    (r"تقرير|إحصا|احصا|نمو|زياد|تحليل|مبيعات|chart|report|growth|stats", "chart"),
    (r"حب|صح|قلب|راحة|اهتمام|health|love|care", "heart"),
    (r"موبايل|تليفون|تطبيق|app|phone|mobile", "phone"),
    (r"تنبيه|تذكير|اشعار|إشعار|notif|remind|alert", "bell"),
    (r"أمان|امان|حماية|خصوصية|سري|secure|safe|privacy", "shield"),
    (r"عميل|عملاء|ناس|فريق|مجتمع|أصحاب|users|team|customer", "users"),
    (r"هدية|عرض|مفاجأة|مجاني|gift|offer|free", "gift"),
    (r"شراء|طلب|متجر|توصيل|منتج|cart|order|shop|store", "cart"),
    (r"كتاب|تعلم|مذاكر|دراس|كورس|شرح|learn|study|book|course", "book"),
    (r"ميعاد|حجز|جدول|يوم|calendar|booking|schedule", "calendar"),
    (r"مكان|عنوان|فرع|موقع|لوكيشن|location|map|address", "pin"),
    (r"ايميل|إيميل|بريد|mail|email", "mail"),
    (r"اكل|أكل|وجبة|مطعم|قهوة|طعام|food|meal", "food"),
    (r"طبيع|بيئة|اخضر|leaf|nature|eco", "leaf"),
    (r"اعداد|إعداد|ضبط|تخصيص|سهل|settings|setup", "gear"),
    (r"فكرة|ابتكار|حل|ذكي|ذكاء|idea|smart|ai", "bulb"),
    (r"ملف|مستند|فاتورة|عقد|pdf|document|invoice|file", "doc"),
    (r"بحث|دور|ابحث|search|find", "search"),
    (r"انطلاق|بداية|ابدأ|نجاح|launch|start", "rocket"),
    (r"قفل|خاص|password|lock", "lock"),
    (r"سعاد|مبسوط|ارتياح|راحة بال|happy|smile", "smile"),
    (r"مشكلة|خطر|ضياع|نسيان|ازعاج|تعب|مشاكل|ورق|warning|problem|lost|forget", "warning"),
    (r"فوز|جائزة|أفضل|افضل|win|best|prize", "trophy"),
    (r"فيديو|تصوير|video|camera", "video"),
    (r"طاقة|قوة|بسرعة|power|energy", "bolt"),
]
CYCLE = ["check", "star", "bolt", "heart", "sparkle", "rocket", "shield", "bulb"]
_icache = {}


def icon_name(name, text, i=0, problem=False):
    n = str(name or "").strip().lower()
    if n in ICONS:
        return n
    for pat, nm in ICON_KEYS:
        if re.search(pat, text or "", re.I):
            return nm
    return "warning" if problem else CYCLE[i % len(CYCLE)]


def icon_img(name, d, ring=True):
    """Round badge: accent disc, dark glyph. d = pixel diameter."""
    key = (name, d, GOLD, TEAL_D)
    if key in _icache:
        return _icache[key]
    S = 4
    big = d * S
    im = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    dr = ImageDraw.Draw(im)
    dr.ellipse([0, 0, big - 1, big - 1], fill=GOLD + (255,))
    g = Image.new("RGBA", (100 * S, 100 * S), (0, 0, 0, 0))
    gd = ImageDraw.Draw(g)
    # draw the glyph at 100-unit coordinates scaled by S using a scaled proxy
    proxy = _Scaled(gd, S)
    ICONS[name](proxy, tuple(TEAL_D) + (255,), GOLD + (255,))
    k = 0.62
    gs = g.resize((int(big * k), int(big * k)), Image.LANCZOS)
    im.paste(gs, ((big - gs.width) // 2, (big - gs.height) // 2), gs)
    out = im.resize((d, d), Image.LANCZOS)
    _icache[key] = out
    return out


class _Scaled:
    """Scale every coordinate/width of the ImageDraw calls the glyph functions use."""
    def __init__(self, d, s):
        self.d, self.s = d, s

    def _p(self, v):
        if isinstance(v, (list, tuple)) and v and isinstance(v[0], (list, tuple)):
            return [(a * self.s, b * self.s) for a, b in v]
        return [x * self.s for x in v]

    def _kw(self, kw):
        kw = dict(kw)
        if "width" in kw:
            kw["width"] = int(kw["width"] * self.s)
        if "joint" in kw:
            kw.pop("joint")
        if "font" in kw:
            kw.pop("font")
        return kw

    def line(self, pts, **kw):
        w = kw.get("width", 1) * self.s
        pts = self._p(pts)
        self.d.line(pts, fill=kw.get("fill"), width=int(w), joint="curve")
        r = w / 2
        for (x, y) in (pts[0], pts[-1]):          # round caps
            self.d.ellipse([x - r, y - r, x + r, y + r], fill=kw.get("fill"))

    def ellipse(self, box, **kw):
        self.d.ellipse(self._p(box), **self._kw(kw))

    def rectangle(self, box, **kw):
        self.d.rectangle(self._p(box), **self._kw(kw))

    def rounded_rectangle(self, box, r, **kw):
        self.d.rounded_rectangle(self._p(box), r * self.s, **self._kw(kw))

    def polygon(self, pts, **kw):
        self.d.polygon(self._p(pts), **self._kw(kw))

    def pieslice(self, box, a, b, **kw):
        self.d.pieslice(self._p(box), a, b, **self._kw(kw))

    def arc(self, box, a, b, **kw):
        self.d.arc(self._p(box), a, b, **self._kw(kw))

    def text(self, *a, **k):
        pass


# ---------- background, shared pieces ----------
def make_bg():
    ys = np.linspace(0, 1, H)[:, None, None]
    top, bot = np.array(BG_TOP)[None, None, :], np.array(BG_BOT)[None, None, :]
    arr = (top * (1 - ys) + bot * ys) * np.ones((1, W, 1))
    xs = np.linspace(-1, 1, W)[None, :, None]            # soft vignette
    arr = arr * (1 - 0.16 * xs ** 2)
    return Image.fromarray(arr.astype("uint8"), "RGB")


def make_glow(size=1000, color=(30, 170, 160), peak=0.33):
    yy, xx = np.mgrid[0:size, 0:size]
    r = np.hypot(xx - size / 2, yy - size / 2) / (size / 2)
    a = (np.clip(1 - r, 0, 1) ** 2 * peak * 255).astype("uint8")
    g = Image.new("RGBA", (size, size), tuple(color) + (0,))
    g.putalpha(Image.fromarray(a, "L"))
    return g


def rrect(w, h, r, fill, outline=None, ow=0):
    im = Image.new("RGBA", (w, h), tuple(fill[:3]) + (0,))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=tuple(fill), outline=outline, width=ow)
    return im


class Style:
    def __init__(self):
        self.bg = make_bg()
        self.glow = make_glow(1000, GLOW, 0.34)
        self.dots = [(make_glow(s, GLOW, 0.30), x, y, sp, ph) for s, x, y, sp, ph in
                     ((260, 120, 400, 0.6, 0.0), (180, 820, 700, 0.5, 1.7), (320, 600, 1300, 0.4, 3.1),
                      (200, 90, 1500, 0.7, 4.4), (240, 880, 1750, 0.55, 2.3))]
        self.logo = self.make_logo(1.0)
        self.logo_big = self.make_logo(1.7)

    def make_logo(self, k):
        w, h = int(420 * k), int(140 * k)
        im = rrect(w, h, int(48 * k), CREAM + (255,))
        t = word_img(NAME_SHOWN, int(78 * k), TEAL_D)
        im.paste(t, ((w - t.width) // 2, int(h * 0.5 - t.height * 0.62)), t)
        return im


def base_frame(st, T, with_logo=True):
    f = st.bg.copy()
    put(f, st.glow, -250 + 120 * math.sin(T * 0.35), 150 + 90 * math.cos(T * 0.3), 1.0)
    put(f, st.glow, 380 + 140 * math.cos(T * 0.33), 1000 + 110 * math.sin(T * 0.27), 0.8)
    for g, x, y, sp, ph in st.dots:
        put(f, g, x + 60 * math.sin(T * 0.3 * sp + ph), y + 90 * math.cos(T * 0.25 * sp + ph), 1.0)
    if with_logo:
        put(f, st.logo, (W - st.logo.width) // 2, 130 + 5 * math.sin(T * 1.6))
    return f


def pop(p):
    """0 -> 1 with a small overshoot."""
    return back(p)


def put_scaled(f, im, cx, cy, s, alpha=1.0):
    if s <= 0.02:
        return
    w, h = max(1, int(im.width * s)), max(1, int(im.height * s))
    im2 = im if abs(s - 1) < 0.01 else im.resize((w, h), Image.BILINEAR)
    put(f, im2, cx - im2.width / 2, cy - im2.height / 2, alpha)


# ---------- scenes ----------
def ripple(f, cx, cy, r0, t, t0):
    if t < t0:
        return
    for k in range(2):
        q = ((t - t0) * 0.45 + k * 0.5) % 1.0
        r = r0 + q * 90
        a = (1 - q) * 0.5
        ring = Image.new("RGBA", (int(2 * r) + 8, int(2 * r) + 8), (0, 0, 0, 0))
        ImageDraw.Draw(ring).ellipse([3, 3, ring.width - 4, ring.height - 4], outline=GOLD + (255,), width=4)
        put(f, ring, cx - ring.width / 2, cy - ring.height / 2, a)


class Scene:
    def __init__(self, kind, say, dur):
        self.kind, self.say, self.dur = kind, say, dur
        self.marks = []     # local start time of each spoken sentence

    def at(self, i, n):
        """Reveal time of item i of n: tied to the spoken sentences when there are enough of them."""
        m = self.marks
        if m and len(m) >= n:
            return max(0.25, m[i * len(m) // n] - 0.15)
        return 0.4 + i * (self.dur * 0.62) / max(1, n)


class ProblemScene(Scene):
    def __init__(self, st, say, text, dur, icon="warning"):
        super().__init__("problem", say, dur)
        self.items, self.bw, self.bh = layout(text, 94, 820, CREAM, line_h=142)
        pad = 70
        self.card = rrect(self.bw + pad * 2, self.bh + pad * 2 - 20, 60, CARD + (225,), LINE + (255,), 3)
        self.bar = rrect(18, self.card.height - 80, 9, GOLD + (255,))
        self.ic = icon_img(icon, 250)

    def draw(self, f, t):
        cx = (W - self.card.width) // 2
        cy = 1000 - self.card.height // 2 + int(8 * math.sin(t * 1.3))
        p = ease(t / 0.5)
        put(f, self.card, cx, cy + (1 - p) * 60, p)
        put(f, self.bar, cx + self.card.width - 50, cy + 40 + (1 - p) * 60, p)
        put_words(f, self.items, cx + 70, cy + 50, t, 0.4, max(0.5, self.dur * 0.5))
        s = pop((t - 0.1) / 0.6)
        icy = cy - 190 + 10 * math.sin(t * 2.2)
        ripple(f, W / 2, icy, 125, t, 0.5)
        put_scaled(f, self.ic, W / 2, icy, s * (1 + 0.025 * math.sin(t * 3)))


class BenefitScene(Scene):
    def __init__(self, st, say, title, items, dur, icons=None):
        super().__init__("benefit", say, dur)
        self.title = layout(title, 64, 860, GOLD, line_h=100) if title else None
        self.cards = []
        icons = icons or []
        for i, it in enumerate(items[:4]):
            its, bw, bh = layout(it, 58, 620, CREAM, line_h=88)
            ch = max(200, bh + 74)
            card = rrect(920, ch, 44, CARD + (232,), LINE + (255,), 3)
            self.cards.append((card, its, bw, bh, icon_img(icon_name(icons[i] if i < len(icons) else "", it, i), 132)))

    def draw(self, f, t):
        tot = sum(c[0].height + 34 for c in self.cards) + ((self.title[2] + 50) if self.title else 0)
        y = max(420, 330 + (1300 - tot) // 2)
        if self.title:
            put_words(f, self.title[0], (W - self.title[1]) // 2, y, t, 0.1, 0.7)
            y += self.title[2] + 50
        n = len(self.cards)
        for i, (card, its, bw, bh, ic) in enumerate(self.cards):
            ti = self.at(i, n)
            p = (t - ti) / 0.55
            if p > 0:
                x = (W - 920) // 2 + (1 - ease(p)) * 160
                put(f, card, x, y, min(1.0, p * 1.6))
                put_scaled(f, ic, x + 920 - 95, y + card.height / 2, pop((p - 0.15) / 0.7), min(1.0, p * 2))
                if p > 0.25:
                    put_words(f, its, x + 36 + (620 - bw) / 2, y + (card.height - bh) // 2 + 8, t, ti + 0.15, 0.4, 14)
            y += card.height + 34


class ChatScene(Scene):
    def __init__(self, st, say, bubbles, dur):
        super().__init__("chat", say, dur)
        self.bubbles = []
        for b in bubbles[:4]:
            mine = (b.get("from") == "user")
            its, bw, bh = layout(b["text"], 46, 600, (255, 255, 255) if mine else (12, 48, 52), line_h=70)
            w, h = bw + 76, bh + 52
            img = rrect(w, h, 36, (0, 112, 92, 255) if mine else (240, 242, 234, 255))
            for it, x, y in its:
                img.paste(it, (38 + x, 18 + y - 4), it)
            self.bubbles.append((mine, img))
        self.dots = rrect(190, 84, 40, (240, 242, 234, 255))
        ph_ = 170 + 50 + sum(b[1].height + 38 for b in self.bubbles) + 30
        self.panel = rrect(960, max(760, ph_), 56, PANEL + (245,), LINE + (255,), 3)
        head = Image.new("RGBA", (960, 170), (0, 0, 0, 0))
        hd = ImageDraw.Draw(head)
        hd.rounded_rectangle([0, 0, 959, 169], 56, fill=HEAD + (255,))
        hd.rectangle([0, 100, 959, 169], fill=HEAD + (255,))
        hd.ellipse([800, 35, 910, 145], fill=CREAM + (255,))
        nm = word_img(NAME_SHOWN, 52, TEAL_D)
        head.paste(nm, (855 - nm.width // 2, 82 - int(nm.height * 0.5)), nm)
        t1 = word_img(NAME_SHOWN, 56, CREAM)
        head.paste(t1, (780 - t1.width, 22), t1)
        t2 = word_img("متصل الآن", 34, (120, 220, 190))
        head.paste(t2, (780 - t2.width, 92), t2)
        self.panel.paste(head, (0, 0), head)
        self.times = None

    def draw(self, f, t):
        n = len(self.bubbles)
        if self.times is None:
            self.times = []
            for i, (mine, _) in enumerate(self.bubbles):
                ti = self.at(i, n) if self.marks and len(self.marks) >= n else 0.6 + i * max(1.0, self.dur * 0.8 - 0.6) / max(1, n)
                if not mine:
                    ti = max(ti, 1.3)
                if self.times and ti < self.times[-1] + 1.0:
                    ti = self.times[-1] + 1.0
                self.times.append(ti)
        px, py = (W - 960) // 2, 330 + max(0, (1300 - self.panel.height) // 2)
        p0 = ease(t / 0.5)
        put(f, self.panel, px, py + (1 - p0) * 50, p0)
        y = py + 170 + 50 + (1 - p0) * 50
        for i, (mine, img) in enumerate(self.bubbles):
            ti = self.times[i]
            x = px + 960 - 40 - img.width if mine else px + 40
            if not mine and ti - 1.0 < t < ti:
                put(f, self.dots, x, y)
                dd = ImageDraw.Draw(f)
                for k in range(3):
                    oy = 9 * math.sin(t * 9 + k * 0.9)
                    dd.ellipse([x + 52 + k * 36, y + 33 + oy, x + 72 + k * 36, y + 53 + oy], fill=(90, 120, 124))
            p = (t - ti) / 0.4
            if p > 0:
                s = 0.7 + 0.3 * back(p)
                im2 = img if s >= 0.995 else img.resize((max(1, int(img.width * s)), max(1, int(img.height * s))))
                ox = (img.width - im2.width) if mine else 0
                put(f, im2, x + ox, y + (img.height - im2.height) / 2, min(1.0, p * 2))
            y += img.height + 38


class EndScene(Scene):
    """Default: 'the link is in the first comment' with a comment icon and a bouncing arrow.
    If a link is passed (payload "link") it is shown in a pill; "end_text" overrides the main line."""
    def __init__(self, st, say, text, link, dur, end_text=""):
        super().__init__("end", say, dur)
        self.st, self.link = st, link
        self.tag = layout(text, 66, 820, CREAM, line_h=100) if text else None
        main = end_text or ("ابعتلي على واتساب وجرب بنفسك" if link else "الرابط في أول تعليق")
        self.cta = layout(main, 78 if not link else 62, 860, GOLD, line_h=112 if not link else 96)
        if link:
            pw, ph = 860, 170
            self.pill = rrect(pw, ph, 85, CREAM + (255,))
            d = ImageDraw.Draw(self.pill)
            d.ellipse([34, 25, 154, 145], fill=(37, 211, 102, 255))
            d.ellipse([60, 48, 130, 112], outline=(255, 255, 255, 255), width=8)
            d.polygon([(66, 104), (58, 132), (88, 116)], fill=(255, 255, 255, 255))
            size = 74
            while size > 30 and d.textlength(link, font=fnt(LAT_FONT, size)) > 640:
                size -= 2
            d.text((190, ph // 2 + 2), link, font=fnt(LAT_FONT, size), fill=TEAL_D + (255,), anchor="lm")
            self.pw, self.ph = pw, ph
        else:
            ic = Image.new("RGBA", (260, 230), GOLD + (0,))
            d = ImageDraw.Draw(ic)
            d.rounded_rectangle([0, 0, 259, 180], 56, fill=CREAM + (255,))
            d.polygon([(60, 170), (50, 228), (118, 176)], fill=CREAM + (255,))
            for k in range(3):
                d.ellipse([62 + k * 62, 72, 98 + k * 62, 108], fill=TEAL_D + (255,))
            self.icon = ic
            ar = Image.new("RGBA", (120, 150), GOLD + (0,))
            d = ImageDraw.Draw(ar)
            d.polygon([(40, 0), (80, 0), (80, 70), (110, 70), (60, 140), (10, 70), (40, 70)], fill=GOLD + (255,))
            self.arrow = ar

    def draw(self, f, t):
        p = ease(t / 0.5)
        k = 1 + 0.035 * math.sin(t * 3)
        lg = self.st.logo_big
        lg2 = lg.resize((int(lg.width * k), int(lg.height * k)))
        put(f, lg2, (W - lg2.width) // 2, 330 - (lg2.height - lg.height) // 2 + (1 - p) * 40, p)
        y = 650
        if self.tag:
            put_words(f, self.tag[0], (W - self.tag[1]) // 2, y, t, 0.4, max(0.6, self.dur * 0.3))
            y += self.tag[2] + 70
        put_words(f, self.cta[0], (W - self.cta[1]) // 2, max(y, 1010), t, 0.8, 0.9)
        pp = ease((t - 1.0) / 0.5)
        if not self.link:
            if pp > 0:
                bob = 14 * math.sin(t * 4)
                put(f, self.icon, (W - 260) // 2, 1330 + (1 - pp) * 50, pp)
                put(f, self.arrow, (W - 120) // 2, 1590 + bob, pp)
            return
        px, py = (W - self.pw) // 2, 1300
        if pp > 0:
            for r in range(3):
                q = ((t * 0.7) + r / 3) % 1.0
                g = int(q * 70)
                ring = Image.new("RGBA", (self.pw + 2 * g + 8, self.ph + 2 * g + 8), (0, 0, 0, 0))
                ImageDraw.Draw(ring).rounded_rectangle([2, 2, ring.width - 3, ring.height - 3], (self.ph + 2 * g) // 2,
                                                       outline=GOLD + (255,), width=4)
                put(f, ring, px - g - 4, py - g - 4, (1 - q) * 0.55 * pp)
            put(f, self.pill, px, py + (1 - pp) * 60, pp)


# ---------- script -> scenes ----------
def split_sentences(t):
    parts = [s.strip() for s in re.split(r"(?<=[.!؟?۔\n])\s*", t) if s.strip()]
    out = []
    for s in parts:
        out += textwrap.wrap(s, 110) if len(s) > 110 else [s]
    return out[:14]


def build_plan(script, brand=True):
    """Returns (scenes, meta). meta may carry {"main": "#hex"} from the script generator."""
    scenes, meta = [], {}
    s = script.strip()
    data = None
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except Exception:
            data = None
    if data and isinstance(data.get("theme"), dict):
        th = data["theme"]
        meta = {"main": str(th.get("main", ""))[:9], "accent": str(th.get("accent", ""))[:9], "exact": bool(th.get("exact"))}
    if data and isinstance(data.get("scenes"), list):
        for sc in data["scenes"][:13]:
            if not isinstance(sc, dict):
                continue
            kind = sc.get("kind")
            say = clean(sc.get("say") or sc.get("text"), 420)
            if kind == "problem" and (sc.get("text") or say):
                scenes.append(dict(kind="problem", say=say or clean(sc.get("text"), 300), text=clean(sc.get("text") or say, 110),
                                   icon=clean(sc.get("icon"), 20).lower()))
            elif kind == "chat" and isinstance(sc.get("bubbles"), list):
                bs = [dict(**{"from": "user" if b.get("from") == "user" else "bot"}, text=clean(b.get("text"), 70))
                      for b in sc["bubbles"][:4] if isinstance(b, dict) and clean(b.get("text"), 70)]
                if bs:
                    if bs[0]["from"] != "user":
                        bs[0]["from"] = "user"
                    scenes.append(dict(kind="chat", say=say, bubbles=bs))
            elif kind == "benefit" and isinstance(sc.get("items"), list):
                its, ics = [], []
                for i in sc["items"][:4]:
                    if isinstance(i, dict):
                        tx, ic = clean(i.get("text"), 60), clean(i.get("icon"), 20).lower()
                    else:
                        tx, ic = clean(i, 60), ""
                    if tx:
                        its.append(tx); ics.append(ic)
                if its:
                    scenes.append(dict(kind="benefit", say=say, title=clean(sc.get("text"), 50), items=its, icons=ics))
            elif kind == "end":
                scenes.append(dict(kind="end", say=say or "الرابط في أول تعليق", text=clean(sc.get("text"), 80)))
    if not scenes:
        ss = [clean(x, 300) for x in split_sentences(s if not data else "")] or [clean(s, 300)]
        ss = [x for x in ss if x]
        scenes.append(dict(kind="problem", say=ss[0], text=clean(ss[0], 110), icon=""))
        for x in ss[1:]:
            scenes.append(dict(kind="benefit", say=x, title="", items=[clean(x, 110)], icons=[""]))
    if not brand:
        scenes = [x for x in scenes if x["kind"] != "end"] or scenes
    elif not scenes or scenes[-1]["kind"] != "end":
        scenes.append(dict(kind="end", say="جربه دلوقتي، الرابط في أول تعليق", text=""))
    for sc in scenes:
        sc["say"] = sc.get("say") or "."
    return scenes[:14], meta


# ---------- voice: text normalisation, sentence-by-sentence TTS, natural pauses ----------
SAMPLE_RATE = 24000
SHOW_LOGO = True
SPOKEN_MAP = [
    (r"(?i)\bwhats\s?app\b", "واتساب"), (r"(?i)\btelegram\b", "تيليجرام"), (r"(?i)\bcrm\b", "سي آر إم"),
    (r"(?i)\bpdf\b", "بي دي إف"), (r"(?i)\bqr\b", "كيو آر"), (r"(?i)\bai\b", "الذكاء الاصطناعي"),
    (r"(?i)\bonline\b", "أونلاين"), (r"(?i)\bapp\b", "تطبيق"), (r"(?i)\bvip\b", "في آي بي"),
    (r"[%٪]", " في المية"), (r"\+", " و "), (r"&", " و "), (r"[/\\|]", " "), (r"=", " "),
    (r"[\"“”«»()\[\]{}<>]", " "), (r"\u0640", ""), (r"[-–—]{1,}", " "), (r"\.{2,}", "،"),
]


def speak_text(t):
    t = spoken(clean(t, 600))
    for pat, rep in SPOKEN_MAP:
        t = re.sub(pat, rep, t)
    t = re.sub(r"[^\w\s\u0600-\u06FF\u064B-\u0652.,!?؟،:؛-]", " ", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def sentences_for_tts(say):
    t = speak_text(say)
    parts = [p.strip(" ،") for p in re.split(r"(?<=[.!؟?!۔])\s+|\n+", t) if p.strip(" ،")]
    out = []
    for p in parts:
        if len(p) > 160:   # long: break on a comma, keeps breathing points
            segs = [x.strip() for x in re.split(r"(?<=،)\s+", p) if x.strip()]
            cur = ""
            for sg in segs:
                if cur and len(cur) + len(sg) > 120:
                    out.append(cur); cur = sg
                else:
                    cur = (cur + " " + sg).strip()
            if cur:
                out.append(cur)
        else:
            out.append(p)
    return [o for o in out if re.search(r"[\w\u0600-\u06FF]", o)] or ["."]


async def tts(text, out, voice, rate, pitch):
    await edge_tts.Communicate(text, voice, rate=rate, pitch=pitch).save(out)


def decode_pcm(path):
    raw = subprocess.run(["ffmpeg", "-v", "error", "-i", path, "-f", "s16le", "-ac", "1", "-ar", str(SAMPLE_RATE), "-"],
                         check=True, capture_output=True).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def trim_silence(x, keep=0.05):
    if x.size == 0:
        return x
    idx = np.where(np.abs(x) > 0.012)[0]
    if idx.size == 0:
        return x[: int(SAMPLE_RATE * 0.2)]
    a = max(0, idx[0] - int(keep * SAMPLE_RATE)); b = min(x.size, idx[-1] + int(keep * SAMPLE_RATE))
    return x[a:b]


def gemini_tts(text, out, voice_name):
    key = os.environ.get("GEMINI_TTS_KEY", "")
    if not key:
        raise RuntimeError("no gemini key")
    model = os.environ.get("GEMINI_TTS_MODEL", "gemini-3.1-flash-tts-preview")
    body = {"contents": [{"parts": [{"text": "اقرأ بالعامية المصرية بصوت طبيعي ودافئ وبسرعة متوسطة: " + text}]}],
            "generationConfig": {"responseModalities": ["AUDIO"],
                                 "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}}}}
    r = requests.post(f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
                      headers={"x-goog-api-key": key, "Content-Type": "application/json"}, json=body, timeout=90)
    if r.status_code != 200:
        raise RuntimeError("gemini http %d" % r.status_code)
    parts = (r.json().get("candidates") or [{}])[0].get("content", {}).get("parts") or []
    b64 = next((p["inlineData"]["data"] for p in parts if isinstance(p, dict) and p.get("inlineData")), "")
    if not b64:
        raise RuntimeError("gemini empty")
    pcm = base64.b64decode(b64)
    subprocess.run(["ffmpeg", "-y", "-f", "s16le", "-ar", "24000", "-ac", "1", "-i", "-", out],
                   input=pcm, check=True, capture_output=True)


def synth_sentence(text, voice, rate, pitch, idx):
    gem = voice.startswith("gemini:")
    out = "s_" + hashlib.md5((voice + rate + pitch + text).encode()).hexdigest()[:12] + (".wav" if gem else ".mp3")
    if os.path.exists(out) and os.path.getsize(out) > 500:
        pass
    elif os.environ.get("DUMMY_TTS"):
        d = max(0.9, len(text) * 0.06)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={d}", "-q:a", "6", out],
                       check=True, capture_output=True)
    elif gem:
        last = None
        for att in range(3):
            try:
                gemini_tts(text, out, voice.split(":", 1)[1]); break
            except Exception as e:
                last = e
                time.sleep(2 * (att + 1))
        else:
            raise RuntimeError("tts failed: " + str(last)[:120])
    else:
        last = None
        for att, (rt, pt) in enumerate(((rate, pitch), (rate, pitch), (rate, "+0Hz"), ("+0%", "+0Hz"))):
            try:
                asyncio.run(tts(text, out, voice, rt, pt)); break
            except Exception as e:
                last = e
                time.sleep(1.0 * (att + 1))
        else:
            raise RuntimeError("tts failed: " + type(last).__name__ + " voice=" + voice + " text=" + text[:40])
    return trim_silence(decode_pcm(out))


def fade_edges(x, ms=12):
    n = int(SAMPLE_RATE * ms / 1000)
    if x.size > 2 * n:
        r = np.linspace(0, 1, n, dtype=np.float32)
        x = x.copy(); x[:n] *= r; x[-n:] *= r[::-1]
    return x


def write_wav(path, x):
    pcm = (np.clip(x, -1, 1) * 32767).astype(np.int16)
    with wave.open(path, "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE); w.writeframes(pcm.tobytes())


PAUSE_S = 0.34       # between sentences
LEAD_S = 0.45        # visuals start this long before their speech
TAIL_S = 0.55        # breathing room after a scene's last word
FADE_S = 0.6         # crossfade length


def build_timeline(plan, voice, rate, pitch, novoice=False, target=0):
    """Returns (starts, scene_dur, marks, audio_array). starts[i] = when scene i's speech begins (global seconds)."""
    chunks, starts, durs, marks = [], [], [], []
    pos = 0.35   # global time of first speech
    sil = lambda s: np.zeros(int(SAMPLE_RATE * s), dtype=np.float32)
    chunks.append(sil(pos))
    k = 0
    all_s = [sentences_for_tts(sc["say"]) for sc in plan]
    from concurrent.futures import ThreadPoolExecutor
    flat = [s for ss in all_s for s in ss]
    if novoice:        # silent video: each sentence stays on screen for its reading time, scaled to the target length
        base = [max(1.3, len(x) * 0.065) for x in flat]
        if target and base:
            budget = max(2.0, target - 0.35 - 0.7 - PAUSE_S * len(flat) - 0.3 * len(plan))
            f = budget / sum(base)
            base = [max(1.0, b * f) for b in base]
        audios = [sil(b) for b in base]
    else:
        with ThreadPoolExecutor(4) as ex:          # network-bound: fetch the sentences in parallel
            audios = list(ex.map(lambda a: fade_edges(synth_sentence(a[1], voice, rate, pitch, a[0])), enumerate(flat)))
    for sc, sents in zip(plan, all_s):
        starts.append(pos); mk = []
        for j, s in enumerate(sents):
            a = audios[k]; k += 1
            mk.append(LEAD_S + (pos - starts[-1]))
            chunks.append(a); pos += a.size / SAMPLE_RATE
            gap = PAUSE_S if j < len(sents) - 1 else TAIL_S
            chunks.append(sil(gap)); pos += gap
        durs.append(pos - starts[-1] + LEAD_S)
        marks.append(mk)
    chunks.append(sil(0.7))
    return starts, durs, marks, np.concatenate(chunks)


def master_audio(wav_in, out_m4a, silent=False):
    if silent:
        subprocess.run(["ffmpeg", "-y", "-i", wav_in, "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "64k", out_m4a], check=True, capture_output=True)
        return
    af = "highpass=f=75,lowpass=f=11000,acompressor=threshold=-20dB:ratio=3:attack=8:release=120:makeup=3,loudnorm=I=-16:TP=-1.5:LRA=9"
    subprocess.run(["ffmpeg", "-y", "-i", wav_in, "-af", af, "-ar", "44100", "-ac", "2", "-c:a", "aac", "-b:a", "160k", out_m4a],
                   check=True, capture_output=True)


# ---------- render ----------
def make_scene(st, sc, dur, link, end_text=""):
    k = sc["kind"]
    if k == "problem":
        return ProblemScene(st, sc["say"], on_screen(sc["text"]), dur, icon_name(sc.get("icon"), sc["text"] + " " + sc["say"], 0, True))
    if k == "chat":
        return ChatScene(st, sc["say"], [dict(b, text=on_screen(b["text"])) for b in sc["bubbles"]], dur)
    if k == "benefit":
        return BenefitScene(st, sc["say"], on_screen(sc.get("title", "")), [on_screen(i) for i in sc["items"]], dur, sc.get("icons"))
    return EndScene(st, sc["say"], on_screen(sc.get("text", "")), link, dur, end_text)


def decrypt_payload():
    if os.environ.get("PAYLOAD_JSON"):          # local testing only
        return json.loads(open(os.environ["PAYLOAD_JSON"], encoding="utf8").read())
    from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    raw = base64.b64decode(os.environ["DATA"])
    key = hashlib.sha256(os.environ["CALLBACK_SECRET"].encode()).digest()
    return json.loads(AESGCM(key).decrypt(raw[:12], raw[12:], None).decode("utf8"))


def callback(P, body):
    try:
        requests.post(P["callback_url"], json=body, timeout=30,
                      headers={"x-callback-secret": os.environ.get("CALLBACK_SECRET", "")})
    except Exception:
        pass


def render_all(st, scenes, starts, total, audio_m4a, out, limit=None, frames_dir=None):
    n = len(scenes)
    frames = int(total * FPS) if limit is None else limit
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", audio_m4a, "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "21",
           "-pix_fmt", "yuv420p", "-c:a", "copy", "-movflags", "+faststart", out]
    errf = open("ff.err", "wb")
    pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
    vis0 = [max(0.0, s - LEAD_S) for s in starts]       # when each scene's visuals begin
    for i in range(frames):
        T = i / FPS
        k = max(j for j in range(n) if vis0[j] <= T + 1e-9)
        def draw(j):
            f = base_frame(st, T, with_logo=(scenes[j].kind != "end" and SHOW_LOGO))
            scenes[j].draw(f, T - vis0[j])
            return f
        f = draw(k)
        if k > 0 and T - vis0[k] < FADE_S:
            a = ease((T - vis0[k]) / FADE_S)
            f = Image.blend(draw(k - 1), f, a)
        if frames_dir and i % int(FPS * 1.0) == 0:
            f.save(f"{frames_dir}/f{i:05d}.jpg", quality=85)
        pr.stdin.write(f.tobytes())
    pr.stdin.close()
    if pr.wait() != 0:
        raise RuntimeError("ffmpeg failed: " + open("ff.err", "rb").read()[-200:].decode("utf8", "ignore"))


def main():
    P = decrypt_payload()
    for k in ("upload_url", "callback_url", "job_id"):   # hide from logs even if something echoes them
        if P.get(k) and len(str(P[k])) >= 8:
            print("::add-mask::" + str(P[k]))
    voice = P.get("voice") or os.environ.get("VOICE") or "ar-EG-ShakirNeural"
    rate, pitch = P.get("rate") or "-6%", P.get("pitch") or "-1Hz"
    link = re.sub(r"^https?://", "", clean(P.get("link"), 60)).rstrip("/")   # empty = "link in first comment"
    end_text = on_screen(clean(P.get("end_text"), 60))
    JOB = P["job_id"]
    try:
        novoice = bool(P.get("novoice")); brand = P.get("brand", True) is not False
        try:
            target = max(0, min(120, int(float(P.get("target_s") or 0))))
        except Exception:
            target = 0
        global SHOW_LOGO
        SHOW_LOGO = brand
        plan, meta = build_plan(P["script"], brand)
        alltext = " ".join(sc.get("say", "") + " " + sc.get("text", "") for sc in plan)
        if not (isinstance(meta, dict) and meta.get("exact") and set_palette_colors(meta.get("main"), meta.get("accent"))):
            set_palette(pick_hue(meta, alltext))
        st = Style()
        chain = [voice]
        for v in ("ar-EG-SalmaNeural", "ar-EG-ShakirNeural"):
            if v not in chain:
                chain.append(v)
        if os.environ.get("GEMINI_TTS_KEY"):
            chain.append("gemini:" + (os.environ.get("GEMINI_TTS_VOICE") or "Kore"))
        built = None; errs = []
        if novoice:
            built = build_timeline(plan, voice, rate, pitch, True, target)
        else:
            for v in chain:   # one voice for the whole video: if a voice fails on any sentence, redo all with the next one
                try:
                    built = build_timeline(plan, v, rate, pitch)
                    print("voice used:", v)
                    if target and built[3].size / SAMPLE_RATE > target * 1.3:   # too long for the asked length: speak faster once
                        try:
                            fast = build_timeline(plan, v, "+30%", pitch)
                            if fast[3].size < built[3].size:
                                built = fast
                        except RuntimeError:
                            pass
                    break
                except RuntimeError as e:
                    errs.append(str(e)[:150]); print("voice failed:", str(e)[:200])
        if built is None:
            raise RuntimeError("tts failed all voices: " + " | ".join(errs)[:200])
        starts, durs, marks, audio = built
        write_wav("voice.wav", audio)
        master_audio("voice.wav", "voice.m4a", novoice)
        scenes = []
        for sc, d, mk in zip(plan, durs, marks):
            s = make_scene(st, sc, d, link, end_text)
            s.marks = mk
            scenes.append(s)
        total = audio.size / SAMPLE_RATE
        render_all(st, scenes, starts, total, "voice.m4a", "out.mp4", frames_dir=os.environ.get("FRAMES_DIR"))
        print("duration %.1fs, scenes %d" % (total, len(plan)))
        if os.environ.get("NO_UPLOAD"):
            print("rendered out.mp4"); return
        data = open("out.mp4", "rb").read()
        up = P["upload_url"]
        r = requests.put(up, data={"cacheControl": "3600"}, files=[("", ("v.mp4", data, "video/mp4"))], timeout=300)
        if r.status_code >= 300:
            r = requests.put(up, data=data, headers={"Content-Type": "video/mp4"}, timeout=300)
        ok = r.status_code < 300
        callback(P, {"job_id": JOB, "ok": ok, "status": r.status_code})
        if not ok:
            raise SystemExit(f"upload failed {r.status_code}")
    except SystemExit:
        raise
    except Exception as e:
        callback(P, {"job_id": JOB, "ok": False, "error": type(e).__name__})
        print("render failed:", type(e).__name__, str(e)[:150].replace(P.get("upload_url", "\0"), "***"))
        raise SystemExit(1)


if __name__ == "__main__":
    main()
