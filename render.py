# render.py - Wudd explainer video renderer (runs on a GitHub Actions runner, public repo safe).
# Input: env DATA = base64(iv[12] + AES-GCM ciphertext) of a JSON payload, key = SHA-256(CALLBACK_SECRET).
# Nothing readable (script, upload URL, job id) is ever in the workflow inputs or in the logs.
import os, re, sys, json, glob, math, base64, hashlib, asyncio, subprocess, textwrap
import requests, edge_tts, arabic_reshaper
try:
    from bidi import get_display
except ImportError:
    from bidi.algorithm import get_display
from PIL import Image, ImageDraw, ImageFont
import numpy as np

W, H, FPS = 1080, 1920, 24
NAME_SHOWN = "وَدّ"      # on screen: fatha on the waw
NAME_SPOKEN = "وِدّ"     # spoken: kasra

BG_TOP, BG_BOT = (7, 66, 72), (3, 26, 36)
CREAM, GOLD, TEAL_D = (245, 236, 215), (236, 190, 92), (8, 70, 76)
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


# ---------- background, header, shared pieces ----------
def make_bg():
    ys = np.linspace(0, 1, H)[:, None, None]
    top, bot = np.array(BG_TOP)[None, None, :], np.array(BG_BOT)[None, None, :]
    arr = (top * (1 - ys) + bot * ys) * np.ones((1, W, 1))
    return Image.fromarray(arr.astype("uint8"), "RGB")


def make_glow(size=1000, color=(30, 170, 160), peak=0.33):
    yy, xx = np.mgrid[0:size, 0:size]
    r = np.hypot(xx - size / 2, yy - size / 2) / (size / 2)
    a = (np.clip(1 - r, 0, 1) ** 2 * peak * 255).astype("uint8")
    g = Image.new("RGBA", (size, size), color + (0,))
    g.putalpha(Image.fromarray(a, "L"))
    return g


def rrect(w, h, r, fill, outline=None, ow=0):
    im = Image.new("RGBA", (w, h), tuple(fill[:3]) + (0,))
    ImageDraw.Draw(im).rounded_rectangle([0, 0, w - 1, h - 1], r, fill=tuple(fill), outline=outline, width=ow)
    return im


class Style:
    def __init__(self):
        self.bg = make_bg(); self.glow = make_glow()
        self.logo = self.make_logo(1.0)
        self.logo_big = self.make_logo(1.7)

    def make_logo(self, k):
        w, h = int(420 * k), int(140 * k)
        im = rrect(w, h, int(48 * k), CREAM + (255,))
        t = word_img(NAME_SHOWN, int(78 * k), TEAL_D)
        im.paste(t, ((w - t.width) // 2, int(h * 0.5 - t.height * 0.62)), t)
        return im


def base_frame(st, t, idx, n, scene_p, with_logo=True):
    f = st.bg.copy()
    put(f, st.glow, -250 + 120 * math.sin(t * 0.5), 150 + 90 * math.cos(t * 0.4), 1.0)
    put(f, st.glow, 380 + 140 * math.cos(t * 0.45), 1000 + 110 * math.sin(t * 0.35), 0.8)
    d = ImageDraw.Draw(f)
    seg = (W - 200 - (n - 1) * 12) / n
    for i in range(n):
        x0 = 100 + i * (seg + 12)
        d.rounded_rectangle([x0, 70, x0 + seg, 80], 5, fill=(40, 100, 104))
        pr = 1.0 if i < idx else (scene_p if i == idx else 0.0)
        if pr > 0:
            d.rounded_rectangle([x0, 70, x0 + max(10, seg * pr), 80], 5, fill=GOLD)
    if with_logo:
        put(f, st.logo, (W - st.logo.width) // 2, 130 + 6 * math.sin(t * 2))
    return f


# ---------- scenes ----------
class Scene:
    def __init__(self, kind, say, dur):
        self.kind, self.say, self.dur = kind, say, dur


class ProblemScene(Scene):
    def __init__(self, st, say, text, dur):
        super().__init__("problem", say, dur)
        self.items, self.bw, self.bh = layout(text, 84, 800, CREAM, line_h=130)
        pad = 70
        self.card = rrect(self.bw + pad * 2, self.bh + pad * 2 - 20, 60, (4, 36, 44, 215), (60, 150, 150, 255), 3)
        self.bar = rrect(18, self.card.height - 80, 9, GOLD + (255,))

    def draw(self, f, t):
        cx = (W - self.card.width) // 2; cy = 900 - self.card.height // 2
        p = ease(t / 0.4)
        put(f, self.card, cx, cy + (1 - p) * 60, p)
        put(f, self.bar, cx + self.card.width - 50, cy + 40 + (1 - p) * 60, p)
        put_words(f, self.items, cx + 70, cy + 40 + 10, t, 0.35, max(0.5, self.dur * 0.55))


class BenefitScene(Scene):
    def __init__(self, st, say, title, items, dur):
        super().__init__("benefit", say, dur)
        self.title = layout(title, 64, 860, GOLD, line_h=100) if title else None
        self.cards = []
        for it in items[:4]:
            its, bw, bh = layout(it, 54, 640, CREAM, line_h=84)
            ch = max(170, bh + 70)
            card = rrect(900, ch, 44, (6, 52, 60, 235), (60, 160, 160, 255), 3)
            ck = Image.new("RGBA", (92, 92), GOLD + (0,)); cd = ImageDraw.Draw(ck)
            cd.ellipse([0, 0, 91, 91], fill=GOLD + (255,))
            cd.line([(24, 48), (40, 64), (70, 28)], fill=TEAL_D + (255,), width=11, joint="curve")
            card.paste(ck, (900 - 92 - 34, (ch - 92) // 2), ck)
            self.cards.append((card, its, bw, bh))

    def draw(self, f, t):
        tot = sum(c[0].height + 34 for c in self.cards) + ((self.title[2] + 50) if self.title else 0)
        y = max(420, 330 + (1300 - tot) // 2)
        if self.title:
            put_words(f, self.title[0], (W - self.title[1]) // 2, y, t, 0.1, 0.7)
            y += self.title[2] + 50
        n = len(self.cards)
        for i, (card, its, bw, bh) in enumerate(self.cards):
            ti = 0.35 + i * (self.dur * 0.7) / max(1, n)
            p = (t - ti) / 0.5
            if p > 0:
                x = (W - 900) // 2 + (1 - ease(p)) * 700
                put(f, card, x, y, min(1.0, p * 2))
                if p > 0.3:
                    put_words(f, its, x + 40 + (640 - bw) / 2 + 0, y + (card.height - bh) // 2 + 8, t, ti + 0.2, 0.4, 14)
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
        self.panel = rrect(960, max(760, ph_), 56, (9, 30, 36, 245), (60, 150, 150, 255), 3)
        head = Image.new("RGBA", (960, 170), (0, 0, 0, 0)); hd = ImageDraw.Draw(head)
        hd.rounded_rectangle([0, 0, 959, 169], 56, fill=(14, 64, 70, 255)); hd.rectangle([0, 100, 959, 169], fill=(14, 64, 70, 255))
        av = st.logo.resize((150, 50))
        hd.ellipse([800, 35, 910, 145], fill=CREAM + (255,))
        nm = word_img(NAME_SHOWN, 52, TEAL_D); head.paste(nm, (855 - nm.width // 2, 82 - int(nm.height * 0.5)), nm)
        t1 = word_img(NAME_SHOWN, 56, CREAM); head.paste(t1, (780 - t1.width, 22), t1)
        t2 = word_img("متصل الآن", 34, (120, 220, 190)); head.paste(t2, (780 - t2.width, 92), t2)
        self.panel.paste(head, (0, 0), head)
        n = len(self.bubbles); self.times = []
        span = max(1.0, self.dur * 0.82 - 0.4)
        for i, (mine, _) in enumerate(self.bubbles):
            ti = 0.5 + i * span / max(1, n)
            if not mine:
                ti = max(ti, 1.4)
            self.times.append(ti)

    def draw(self, f, t):
        px, py = (W - 960) // 2, 330 + max(0, (1300 - self.panel.height) // 2)
        p0 = ease(t / 0.4)
        put(f, self.panel, px, py + (1 - p0) * 50, p0)
        y = py + 170 + 50 + (1 - p0) * 50
        for i, (mine, img) in enumerate(self.bubbles):
            ti = self.times[i]; x = px + 960 - 40 - img.width if mine else px + 40
            if not mine and ti - 1.0 < t < ti:   # typing dots
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
    """Default: 'the link is in the first comment' with a comment icon and a bouncing arrow (no number, no link).
    If a link is passed (payload "link"), it is shown in a pill instead; "end_text" overrides the main line."""
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
        else:   # comment-bubble icon
            ic = Image.new("RGBA", (260, 230), GOLD + (0,)); d = ImageDraw.Draw(ic)
            d.rounded_rectangle([0, 0, 259, 180], 56, fill=CREAM + (255,))
            d.polygon([(60, 170), (50, 228), (118, 176)], fill=CREAM + (255,))
            for k in range(3):
                d.ellipse([62 + k * 62, 72, 98 + k * 62, 108], fill=TEAL_D + (255,))
            self.icon = ic
            ar = Image.new("RGBA", (120, 150), GOLD + (0,)); d = ImageDraw.Draw(ar)
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
            put_words(f, self.tag[0], (W - self.tag[1]) // 2, y, t, 0.4, max(0.6, self.dur * 0.35)); y += self.tag[2] + 70
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
    return out[:10]


def build_plan(script):
    scenes = []
    s = script.strip()
    data = None
    if s.startswith("{"):
        try:
            data = json.loads(s)
        except Exception:
            data = None
    if data and isinstance(data.get("scenes"), list):
        for sc in data["scenes"][:9]:
            if not isinstance(sc, dict):
                continue
            kind = sc.get("kind")
            say = clean(sc.get("say") or sc.get("text"), 300)
            if kind == "problem" and (sc.get("text") or say):
                scenes.append(dict(kind="problem", say=say or clean(sc.get("text"), 300), text=clean(sc.get("text") or say, 110)))
            elif kind == "chat" and isinstance(sc.get("bubbles"), list):
                bs = [dict(**{"from": "user" if b.get("from") == "user" else "bot"}, text=clean(b.get("text"), 70))
                      for b in sc["bubbles"][:4] if isinstance(b, dict) and clean(b.get("text"), 70)]
                if bs:
                    if bs[0]["from"] != "user":
                        bs[0]["from"] = "user"
                    scenes.append(dict(kind="chat", say=say, bubbles=bs))
            elif kind == "benefit" and isinstance(sc.get("items"), list):
                its = [clean(i, 60) for i in sc["items"][:4] if clean(i, 60)]
                if its:
                    scenes.append(dict(kind="benefit", say=say, title=clean(sc.get("text"), 50), items=its))
            elif kind == "end":
                scenes.append(dict(kind="end", say=say or "الرابط في أول تعليق", text=clean(sc.get("text"), 80)))
    if not scenes:
        ss = [clean(x, 300) for x in split_sentences(s if not data else "")] or [clean(s, 300)]
        ss = [x for x in ss if x]
        scenes.append(dict(kind="problem", say=ss[0], text=clean(ss[0], 110)))
        for x in ss[1:]:
            scenes.append(dict(kind="benefit", say=x, title="", items=[clean(x, 110)]))
    if not scenes or scenes[-1]["kind"] != "end":
        scenes.append(dict(kind="end", say="جربه دلوقتي، الرابط في أول تعليق", text=""))
    for sc in scenes:
        sc["say"] = sc.get("say") or "."
    return scenes[:10]


# ---------- audio / ffmpeg ----------
async def tts(text, out, voice):
    await edge_tts.Communicate(spoken(text), voice).save(out)


def make_audio(text, out, voice):
    if os.environ.get("DUMMY_TTS"):
        d = max(1.5, len(text) * 0.07)
        subprocess.run(["ffmpeg", "-y", "-f", "lavfi", "-i", f"sine=frequency=220:duration={d}", "-q:a", "6", out],
                       check=True, capture_output=True)
        return
    last = None
    for _ in range(3):
        try:
            asyncio.run(tts(text, out, voice)); return
        except Exception as e:
            last = e
    raise RuntimeError("tts failed: " + str(last)[:120])


def dur(p):
    return float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", p]).strip())


def render_scene(st, sc, idx, n, audio, out):
    total = dur(audio) + 0.7
    frames = int(total * FPS)
    cmd = ["ffmpeg", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{W}x{H}", "-r", str(FPS), "-i", "-",
           "-i", audio, "-af", "apad", "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", "veryfast", "-crf", "24",
           "-pix_fmt", "yuv420p", "-c:a", "aac", "-ar", "44100", "-ac", "2", out]
    errf = open("ff.err", "wb")
    pr = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=errf)
    sc.dur = total
    for i in range(frames):
        t = i / FPS
        f = base_frame(st, t, idx, n, min(1.0, (t) / total), with_logo=(sc.kind != "end"))
        sc.draw(f, t)
        pr.stdin.write(f.tobytes())
    pr.stdin.close()
    if pr.wait() != 0:
        raise RuntimeError("ffmpeg failed: " + open("ff.err", "rb").read()[-200:].decode("utf8", "ignore"))
    return total


def make_scene(st, sc, dur_guess, link, end_text=""):
    k = sc["kind"]
    if k == "problem":
        return ProblemScene(st, sc["say"], on_screen(sc["text"]), dur_guess)
    if k == "chat":
        return ChatScene(st, sc["say"], [dict(b, text=on_screen(b["text"])) for b in sc["bubbles"]], dur_guess)
    if k == "benefit":
        return BenefitScene(st, sc["say"], on_screen(sc.get("title", "")), [on_screen(i) for i in sc["items"]], dur_guess)
    return EndScene(st, sc["say"], on_screen(sc.get("text", "")), link, dur_guess, end_text)


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


def main():
    P = decrypt_payload()
    for k in ("upload_url", "callback_url", "job_id"):   # hide from logs even if something echoes them
        if P.get(k) and len(str(P[k])) >= 8:
            print("::add-mask::" + str(P[k]))
    voice = P.get("voice") or "ar-EG-ShakirNeural"
    link = re.sub(r"^https?://", "", clean(P.get("link"), 60)).rstrip("/")   # empty = "link in first comment"
    end_text = on_screen(clean(P.get("end_text"), 60))
    JOB = P["job_id"]
    try:
        plan = build_plan(P["script"])
        st = Style()
        clips = []
        n = len(plan)
        for i, sc in enumerate(plan):
            a, mp4 = f"a{i}.mp3", f"c{i}.mp4"
            make_audio(sc["say"], a, voice)
            scene = make_scene(st, sc, dur(a) + 0.7, link, end_text)
            render_scene(st, scene, i, n, a, mp4)
            clips.append(mp4)
        open("list.txt", "w").write("".join(f"file '{c}'\n" for c in clips))
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", "list.txt", "-c", "copy",
                        "-movflags", "+faststart", "out.mp4"], check=True, capture_output=True)
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
