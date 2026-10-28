"""The Downtime Ghost's image cards: 1080x1080, HTML and CSS rendered with Playwright's Chromium.

A card carries the verse, the one verbatim quote and the source, in the ghost's colours:
night navy, ghost white, a red lamp for an outage, a green one once the service has risen,
an amber candle for Throwback Thursday. Fonts are bundled under fonts/ (SIL Open Font License).

    python ghost_card.py sample.json out.jpg     # render one card from a spec file
"""
import base64
import html
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
FONTS = HERE / "fonts"
SIZE = 1080
INK = {"haunting": "#EB4650", "risen": "#5BE49B", "throwback": "#F2C14E", "ofrenda": "#F2C14E"}
LABEL = {"haunting": "DOWN", "risen": "RISEN", "throwback": "THROWBACK THURSDAY", "ofrenda": "OFRENDA"}


def _e(text):
    return html.escape(str(text or ""), quote=False)


def _font(name):
    """The font file inlined: a page built from a string has no origin, so file:// fonts would not load."""
    data = base64.b64encode((FONTS / name).read_bytes()).decode()
    return f"url(data:font/ttf;base64,{data})"


def page_html(spec):
    kind = spec.get("kind", "haunting")
    ink = INK.get(kind, INK["haunting"])
    poem = _e(spec.get("poem", "")).replace("\n", "<br>")
    quote = _e(spec.get("quote", ""))
    return f"""<!doctype html><meta charset="utf-8"><style>
@font-face{{font-family:SG;src:{_font('SpaceGrotesk-700.ttf')};font-weight:700}}
@font-face{{font-family:SG;src:{_font('SpaceGrotesk-500.ttf')};font-weight:500}}
@font-face{{font-family:IS;src:{_font('InstrumentSerif-400.ttf')};font-weight:400}}
*{{margin:0;box-sizing:border-box}} html,body{{width:{SIZE}px;height:{SIZE}px;overflow:hidden}}
body{{background:#12102A;color:#F5F7FF;font-family:SG;position:relative}}
.sky{{position:absolute;inset:0;background:
  radial-gradient(circle at 95% 9%, rgba(245,247,255,.9) 0 1px, transparent 2px),
  radial-gradient(circle at 90% 26%, rgba(245,247,255,.7) 0 1px, transparent 2px),
  radial-gradient(circle at 97% 46%, rgba(245,247,255,.6) 0 1px, transparent 2px),
  radial-gradient(circle at 4% 52%, rgba(245,247,255,.5) 0 1px, transparent 2px),
  radial-gradient(circle at 93% 66%, rgba(245,247,255,.6) 0 1px, transparent 2px),
  radial-gradient(circle at 60% 96%, rgba(245,247,255,.5) 0 1px, transparent 2px),
  radial-gradient(circle at 30% 97%, rgba(245,247,255,.4) 0 1px, transparent 2px),
  radial-gradient(ellipse at 50% 110%, rgba(91,80,180,.35), transparent 60%)}}
.tag{{position:absolute;left:80px;top:76px;right:80px;display:flex;gap:14px;align-items:center;
  font:500 21px SG;letter-spacing:.12em;color:{ink};text-transform:uppercase;white-space:nowrap;overflow:hidden}}
.dot{{flex:none;width:14px;height:14px;border-radius:50%;background:{ink};box-shadow:0 0 18px {ink}}}
h1{{position:absolute;left:80px;top:124px;width:920px;font:700 68px/1.04 SG;letter-spacing:-0.04em}}
.sub{{position:absolute;left:80px;top:212px;width:920px;font:500 26px/1.2 SG;color:#B9B6D6}}
.poem{{position:absolute;left:80px;top:268px;width:920px;height:520px;font:400 60px/1.22 IS;overflow:hidden;white-space:nowrap}}
.quote{{position:absolute;left:80px;bottom:196px;width:920px;font:400 29px/1.3 IS;color:#B9B6D6;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}
.ghost{{position:absolute;left:80px;bottom:80px;width:66px;height:84px;background:#F5F7FF;border-radius:33px 33px 8px 8px}}
.ghost i{{position:absolute;top:30px;width:11px;height:15px;border-radius:50%;background:#12102A}}
.ghost i.l{{left:17px}} .ghost i.r{{left:38px}}
.ghost b{{position:absolute;bottom:-8px;width:18px;height:16px;border-radius:50%;background:#12102A}}
.ghost b:nth-of-type(1){{left:2px}} .ghost b:nth-of-type(2){{left:24px}} .ghost b:nth-of-type(3){{left:46px}}
.lamp{{position:absolute;left:150px;bottom:84px;width:26px;height:26px;border-radius:50%;background:{ink};box-shadow:0 0 22px {ink}}}
.src{{position:absolute;right:80px;bottom:92px;max-width:700px;font:500 20px SG;letter-spacing:.1em;color:#8E8BB0;
  text-transform:uppercase;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}}
</style><div class="sky"></div>
<div class="tag" id="tag"><div class="dot"></div><span>{_e(spec.get("tag", "The Downtime Ghost"))}</span></div>
<h1>{_e(spec.get("headline", ""))}</h1>
<div class="sub">{_e(spec.get("sub", ""))}</div>
<div class="poem" id="poem">{poem}</div>
<div class="quote">{"&#8220;" + quote + "&#8221;" if quote else ""}</div>
<div class="ghost"><i class="l"></i><i class="r"></i><b></b><b></b><b></b></div><div class="lamp"></div>
<div class="src">{_e(spec.get("source", ""))}</div>"""


FIT_JS = """() => {
  const t = document.getElementById('tag');                                      // the top line never clips
  let ts = 21;
  while (ts > 13 && t.scrollWidth > t.clientWidth) { ts -= 1; t.style.fontSize = ts + 'px'; }
  const p = document.getElementById('poem');
  const over = () => p.scrollHeight > p.clientHeight || p.scrollWidth > p.clientWidth;
  let size = 60;
  while (size > 34 && over()) { size -= 2; p.style.fontSize = size + 'px'; }   // first: no line may wrap
  if (over()) { p.style.whiteSpace = 'normal'; }                                 // then: wrap, and keep shrinking
  while (size > 22 && over()) { size -= 2; p.style.fontSize = size + 'px'; }
  return size;
}"""


def render(spec, out=None):
    """JPEG bytes of the card (and the file at `out` when given). The verse shrinks until it fits."""
    import os
    from playwright.sync_api import sync_playwright
    launch = {"executable_path": os.environ["CHROMIUM_PATH"]} if os.environ.get("CHROMIUM_PATH") else {}
    with sync_playwright() as p:
        browser = p.chromium.launch(**launch)
        try:
            page = browser.new_page(viewport={"width": SIZE, "height": SIZE})
            page.set_content(page_html(spec))
            page.evaluate("document.fonts.ready.then(() => true)")
            page.evaluate(FIT_JS)
            page.wait_for_timeout(50)
            data = page.screenshot(type="jpeg", quality=88)
        finally:
            browser.close()
    if out:
        Path(out).write_bytes(data)
    return data


def alt_text(spec):
    """Say what is shown, then the words on it (the pattern the other accounts use)."""
    kind = spec.get("kind", "haunting")
    lamp = {"haunting": "a red lamp", "risen": "a green lamp", "throwback": "an amber candle", "ofrenda": "an amber candle"}.get(kind, "a lamp")
    parts = [f"Dark card with a small white ghost and {lamp}. {spec.get('tag', '')}. {spec.get('headline', '')}."]
    if spec.get("sub"):
        parts.append(str(spec["sub"]) + ".")
    if spec.get("poem"):
        parts.append("Verse: " + " / ".join(l.strip() for l in str(spec["poem"]).splitlines() if l.strip()) + ".")
    if spec.get("quote"):
        parts.append(f"Quote: “{spec['quote']}”.")
    if spec.get("source"):
        parts.append(str(spec["source"]).rstrip(".") + ".")
    return " ".join(parts)[:1990]


if __name__ == "__main__":
    if len(sys.argv) == 3:
        render(json.loads(Path(sys.argv[1]).read_text()), sys.argv[2])
        print("wrote", sys.argv[2])
    else:
        print(__doc__)
