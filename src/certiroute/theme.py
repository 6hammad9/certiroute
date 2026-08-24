"""The visual system: type, colour, spacing, and an inline icon set.

The reference class for this product is operational software - the tools a
dispatcher already has open - not a marketing page. Typewolf's survey of
well-regarded design work shows the same convention repeatedly: a neo-grotesque
carrying the interface (Apercu, Basis Grotesque, Graphik, GT America) paired
with a monospace for anything numeric. Those faces are commercial, so this uses
their closest well-made equivalents on Google Fonts.

* Instrument Sans - display and headings. Slightly warm grotesque, close in
  character to Apercu, and reads as chosen rather than defaulted-to.
* Inter - interface text. Still unmatched below 14px, with true tabular figures.
* JetBrains Mono - times, temperatures, degree-hours. Numbers a dispatcher
  compares down a column must sit in the same place on every row.

Colour is deliberately neutral-dominant. The palette carries meaning rather
than decoration: mint is the recommended path, orange is heat. Icons are inline
SVG on a 24px grid at 1.75 stroke, never emoji, because an emoji renders as a
different picture on every operating system and cannot inherit text colour.
"""

from __future__ import annotations

from html import escape

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2"
    "?family=Instrument+Sans:wght@400;500;600;700"
    "&family=Inter:wght@400;500;600;700"
    "&family=JetBrains+Mono:wght@400;500;600&display=swap');"
)

# Lucide-style geometry: 24px grid, 1.75 stroke, round caps and joins. Paths
# only - the wrapper supplies the svg element so size and colour stay uniform.
_ICON_PATHS: dict[str, str] = {
    "sunrise": (
        '<path d="M12 2v6M4.9 10.9 3.5 9.5M19.1 10.9l1.4-1.4"/>'
        '<path d="M2 18h20M6 18a6 6 0 0 1 12 0M8 22h8"/>'
    ),
    "thermometer": (
        '<path d="M14 14.76V4.5a2.5 2.5 0 0 0-5 0v10.26a4.5 4.5 0 1 0 5 0Z"/>'
    ),
    "clock": '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3.5 2"/>',
    "pin": (
        '<path d="M20 10c0 5.5-8 12-8 12s-8-6.5-8-12a8 8 0 0 1 16 0Z"/>'
        '<circle cx="12" cy="10" r="3"/>'
    ),
    "route": (
        '<circle cx="6" cy="19" r="3"/><circle cx="18" cy="5" r="3"/>'
        '<path d="M9 19h5a4 4 0 0 0 0-8h-4a4 4 0 0 1 0-8h5"/>'
    ),
    "shield": (
        '<path d="M12 22s8-3.5 8-10V5l-8-3-8 3v7c0 6.5 8 10 8 10Z"/>'
        '<path d="m9 12 2 2 4-4"/>'
    ),
    "gauge": (
        '<path d="M12 21a9 9 0 1 1 9-9"/><path d="m12 13 4-4"/>'
        '<circle cx="12" cy="13" r="1.5"/>'
    ),
    "check": '<path d="m4.5 12.5 5 5 10-11"/>',
    "arrow-right": '<path d="M4 12h15"/><path d="m13 6 6 6-6 6"/>',
    "alert": (
        '<path d="M10.3 3.9 1.9 18a2 2 0 0 0 1.7 3h16.8a2 2 0 0 0 1.7-3L13.7 3.9'
        'a2 2 0 0 0-3.4 0Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>'
    ),
    "layers": (
        '<path d="m12 2 9 5-9 5-9-5 9-5Z"/><path d="m3 12 9 5 9-5"/>'
        '<path d="m3 17 9 5 9-5"/>'
    ),
    "calendar": (
        '<rect x="3" y="5" width="18" height="16" rx="2"/>'
        '<path d="M3 10h18M8 3v4M16 3v4"/>'
    ),
    "download": (
        '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 21h16"/>'
    ),
    "map": (
        '<path d="m2 6 6.5-3 7 3L22 3v15l-6.5 3-7-3L2 21V6Z"/>'
        '<path d="M8.5 3v15M15.5 6v15"/>'
    ),
}


def icon(name: str, *, size: int = 18, extra_class: str = "") -> str:
    """One inline SVG that inherits the surrounding text colour."""

    paths = _ICON_PATHS.get(name)
    if paths is None:
        raise KeyError(f"unknown icon {name!r}")
    classes = f"icon {extra_class}".strip()
    return (
        f'<svg class="{escape(classes)}" width="{size}" height="{size}" '
        'viewBox="0 0 24 24" fill="none" stroke="currentColor" '
        'stroke-width="1.75" stroke-linecap="round" stroke-linejoin="round" '
        f'aria-hidden="true">{paths}</svg>'
    )


# Some Streamlit versions strip @import from injected style blocks, so the
# faces are also requested with a link element. Both are harmless together, and
# every stack still names a system fallback so the app never waits on a font.
FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    "?family=Instrument+Sans:wght@400;500;600;700"
    "&family=Inter:wght@400;500;600;700"
    '&family=JetBrains+Mono:wght@400;500;600&display=swap">'
)

def as_markup(*blocks: str) -> str:
    """Join HTML for injection, with every blank line removed.

    CommonMark ends a raw-HTML block at the first blank line. A stylesheet
    written with blank lines between its sections therefore stops being a
    stylesheet at the first section break, and every rule after it is rendered
    onto the page as paragraph text. That is not a subtle failure - the whole
    design system appears as visible source above the interface.

    Blank lines are stripped here so the CSS can still be authored with the
    spacing that makes it readable.
    """

    joined = "\n".join(blocks)
    return "\n".join(line for line in joined.splitlines() if line.strip())


_CSS = f"""
<style>
{FONT_IMPORT}

:root {{
  /* Neutrals carry the interface; the palette carries meaning. */
  --ink:        #0C1116;
  --ink-2:      #333D47;
  --muted:      #5C6873;
  --faint:      #8A949E;
  --rule:       #E5E9EC;
  --rule-firm:  #CFD6DC;
  --canvas:     #F7F8FA;
  --surface:    #FFFFFF;

  --route:      #70FFD2;
  --route-ink:  #05372A;
  --route-soft: #E8FFF7;
  --route-line: #9DF4DC;

  --heat:       #FF9137;
  --heat-ink:   #8A3B00;
  --heat-soft:  #FFF3E9;
  --gold:       #FFCC4D;
  --caution:    #FFFC8C;

  --font-display: "Instrument Sans", "Inter", system-ui, sans-serif;
  --font-ui: "Inter", system-ui, -apple-system, "Segoe UI", sans-serif;
  --font-mono: "JetBrains Mono", ui-monospace, "SFMono-Regular", monospace;

  --r-sm: 8px; --r-md: 12px; --r-lg: 16px; --r-xl: 22px;
  --shadow: 0 1px 2px rgba(12,17,22,.04), 0 8px 24px -16px rgba(12,17,22,.18);
}}

html {{ color-scheme: light; }}
.stApp, [data-testid="stAppViewContainer"], .main {{
  background: var(--canvas) !important;
  color: var(--ink);
  font-family: var(--font-ui);
  font-feature-settings: "cv05" 1, "ss01" 1;
}}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ max-width: 1180px; padding-top: 1.5rem; padding-bottom: 4rem; }}

/* --- Type ------------------------------------------------------------- */

h1, h2, h3, h4 {{ font-family: var(--font-display); color: var(--ink); }}
h1 {{
  font-size: 2.6rem; font-weight: 700; letter-spacing: -.035em; line-height: 1.04;
}}
h2 {{
  font-size: 1.45rem; font-weight: 600; letter-spacing: -.022em;
  margin-top: 2.4rem;
}}
h3 {{
  font-size: 1.1rem; font-weight: 600; letter-spacing: -.016em;
  margin-top: 1.6rem;
}}
h4 {{ font-size: .95rem; font-weight: 600; letter-spacing: -.01em; }}
p, li, label, .stMarkdown {{ font-family: var(--font-ui); }}

.icon {{ flex: none; vertical-align: -.18em; }}

/* Numbers a dispatcher scans down a column must not shift between rows. */
.mono, .route-fact-value, .route-stop-time, .timing-label,
[data-testid="stMetricValue"] {{
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
  letter-spacing: -.02em;
}}

.eyebrow {{
  display: inline-flex; align-items: center; gap: .4rem;
  color: var(--heat-ink); font-size: .7rem; font-weight: 600;
  letter-spacing: .13em; text-transform: uppercase;
}}

/* The product name sits at label scale so the headline owns the page. */
.wordmark {{
  display: flex; align-items: center; gap: .55rem; flex-wrap: wrap;
  padding-bottom: 1.1rem; margin-bottom: 1.4rem;
  border-bottom: 1px solid var(--rule);
}}
.wordmark > span:first-of-type {{
  font-family: var(--font-display); font-size: 1.02rem; font-weight: 700;
  letter-spacing: -.02em; color: var(--ink);
}}
.wordmark > .icon {{ color: var(--route-ink); }}
.wordmark-tag {{
  display: inline-flex; align-items: center; gap: .35rem;
  margin-left: .35rem; padding-left: .75rem;
  border-left: 1px solid var(--rule);
  color: var(--faint); font-size: .74rem; font-weight: 500;
}}
.wordmark-tag .icon {{ color: var(--heat); }}

h1.hero-heading {{
  font-family: var(--font-display); color: var(--ink);
  font-size: 2.9rem; font-weight: 700; letter-spacing: -.038em;
  line-height: 1.02; margin: 0 0 1rem; max-width: 16ch;
}}
.hero-copy {{
  color: var(--muted); font-size: 1.02rem; line-height: 1.6;
  max-width: 62ch; margin: 0;
}}
.hero-proof {{
  display: flex; align-items: center; gap: 1.4rem; flex-wrap: wrap;
  margin-top: 1.3rem; padding-top: 1.1rem; border-top: 1px solid var(--rule);
  color: var(--muted); font-size: .8rem; font-weight: 500;
}}
.hero-proof span {{ display: inline-flex; align-items: center; gap: .45rem; }}
.hero-proof .heat {{ color: var(--heat-ink); }}
.hero-proof .heat .icon {{ color: var(--heat); }}

/* --- Steps ------------------------------------------------------------ */

.process-strip {{
  display: flex; align-items: center; gap: .9rem; flex-wrap: wrap;
  margin: 1.6rem 0 2rem; color: var(--muted);
}}
.process-step {{
  display: inline-flex; align-items: center; gap: .5rem;
  font-size: .8rem; font-weight: 500; color: var(--ink-2);
}}
.process-number {{
  display: inline-grid; place-items: center; width: 1.45rem; height: 1.45rem;
  border-radius: 50%; background: var(--surface); border: 1px solid var(--rule-firm);
  font-family: var(--font-mono); font-size: .7rem; font-weight: 500;
  color: var(--muted);
}}
.process-arrow {{ color: var(--rule-firm); display: inline-flex; }}

/* --- Surfaces --------------------------------------------------------- */

.picker-instruction, .journey-panel, .empty-state, .build-summary,
.timing-bars, .route-rail, .safety-note {{
  background: var(--surface); border: 1px solid var(--rule);
  border-radius: var(--r-lg);
}}

.picker-instruction {{ padding: 1rem 1.15rem; margin-bottom: 1rem; }}
.picker-instruction strong {{
  display: block; font-family: var(--font-display); font-size: .98rem;
  font-weight: 600; color: var(--ink); margin-bottom: .15rem;
}}
.picker-instruction span {{ color: var(--muted); font-size: .87rem; }}
.picker-instruction.ready {{
  border-color: var(--route-line); background: var(--route-soft);
}}

.build-summary {{
  padding: 1.05rem 1.2rem; margin: .9rem 0 1.1rem;
  color: var(--muted); font-size: .9rem; line-height: 1.55;
}}
.build-summary strong {{
  display: block; font-family: var(--font-display); color: var(--ink);
  font-size: 1rem; font-weight: 600; margin-bottom: .25rem;
}}

.workday-chip {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 1rem; flex-wrap: wrap; margin: .85rem 0 .4rem;
  padding: .6rem .9rem; border: 1px solid var(--rule);
  border-radius: var(--r-md); background: var(--surface);
  font-size: .82rem; color: var(--muted);
}}
.workday-chip strong {{ color: var(--ink); font-weight: 600; }}
.workday-chip span:last-child {{
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
  font-size: .78rem;
}}

.empty-state {{ padding: 2.2rem 1.6rem; text-align: center; }}
.empty-state h3 {{ margin: 0 0 .5rem; font-size: 1.15rem; }}
.empty-state p {{
  color: var(--muted); font-size: .92rem; line-height: 1.6;
  max-width: 54ch; margin: 0 auto;
}}

.safety-note {{
  padding: .85rem 1rem; margin-top: 1.4rem;
  color: var(--muted); font-size: .82rem; line-height: 1.55;
  border-left: 3px solid var(--gold);
}}
.safety-note strong {{ color: var(--ink); font-weight: 600; }}

/* --- The decision ----------------------------------------------------- */

.bento {{
  display: grid; grid-template-columns: repeat(6, minmax(0, 1fr));
  gap: 12px; margin: .5rem 0 1.8rem;
}}
.bento > * {{
  border: 1px solid var(--rule); border-radius: var(--r-lg);
  padding: 1.25rem 1.35rem; min-width: 0; background: var(--surface);
}}
.bento-hero {{
  grid-column: span 4; grid-row: span 3;
  display: flex; flex-direction: column; justify-content: center;
}}
.bento-tile {{
  grid-column: span 2; display: flex; flex-direction: column;
  justify-content: center;
}}
.decision-card {{
  background: var(--route); border-color: var(--route);
  box-shadow: var(--shadow);
}}
.decision-label {{
  display: inline-flex; align-items: center; gap: .4rem;
  color: var(--route-ink); font-size: .68rem; font-weight: 600;
  letter-spacing: .13em; text-transform: uppercase; opacity: .78;
}}
.decision-card h2 {{
  margin: .5rem 0 .45rem; font-size: 2.15rem; font-weight: 700;
  letter-spacing: -.035em; color: var(--route-ink); line-height: 1.05;
}}
.decision-card p {{
  margin: 0; color: #0A4234; font-size: .93rem; line-height: 1.6; max-width: 46ch;
}}
.decision-card strong {{ font-weight: 600; }}

.route-fact {{ background: var(--surface); border-radius: var(--r-lg); }}
.route-fact-label {{
  display: flex; align-items: center; gap: .4rem;
  color: var(--faint); font-size: .68rem; font-weight: 600;
  letter-spacing: .09em; text-transform: uppercase; margin-bottom: .45rem;
}}
.route-fact-value {{
  color: var(--ink); font-size: 1.15rem; font-weight: 500;
  overflow-wrap: anywhere; line-height: 1.25;
}}
.bento-tile.stop .route-fact-label .icon {{ color: var(--heat); }}
.bento-tile.time .route-fact-label .icon {{ color: var(--route-ink); }}
.bento-tile.status .route-fact-label .icon {{ color: var(--muted); }}
.route-summary {{
  display: grid; grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px; margin: 0 0 1.35rem;
}}

/* --- Start-time comparison -------------------------------------------- */

.timing-bars {{
  padding: 1.15rem 1.3rem; margin: .3rem 0 1.5rem;
  display: flex; flex-direction: column; gap: .5rem;
}}
.timing-row {{
  display: grid; grid-template-columns: 3.6rem 1fr 7.2rem;
  align-items: center; gap: .9rem;
}}
.timing-label {{ color: var(--muted); font-size: .84rem; font-weight: 500; }}
.timing-track {{
  background: var(--canvas); border-radius: 999px; height: 10px;
  overflow: hidden; border: 1px solid var(--rule);
}}
.timing-bar {{
  background: var(--rule-firm); height: 100%; border-radius: 999px;
  min-width: 4px;
}}
.timing-tag {{
  color: var(--faint); font-size: .76rem; font-weight: 500; text-align: right;
}}
.timing-row.picked .timing-label {{ color: var(--ink); font-weight: 600; }}
.timing-row.picked .timing-bar {{ background: var(--route); }}
.timing-row.picked .timing-tag {{ color: var(--route-ink); font-weight: 600; }}

/* --- Route hand-off --------------------------------------------------- */

.route-rail {{ position: relative; padding: 1.1rem 1.2rem; }}
.route-stop, .route-endpoint, .route-return {{
  display: grid; grid-template-columns: 2.4rem minmax(0, 1fr) auto;
  align-items: center; gap: .85rem; padding: .7rem 0;
  border-bottom: 1px solid var(--rule);
}}
.route-rail > *:last-child {{ border-bottom: 0; }}
.route-endpoint, .route-return {{ grid-template-columns: 2.4rem minmax(0, 1fr); }}
.route-stop-number {{
  display: grid; place-items: center; width: 2.15rem; height: 2.15rem;
  border-radius: 50%; background: var(--heat-soft); color: var(--heat-ink);
  border: 1px solid var(--gold);
  font-family: var(--font-mono); font-size: .92rem; font-weight: 600;
}}
.route-endpoint-node, .route-return-node {{
  width: .85rem; height: .85rem; border-radius: 50%;
  background: var(--route); border: 1px solid var(--route-line);
  margin-left: .65rem;
}}
.route-return-node {{ background: var(--surface); }}
.route-stop-kicker {{
  color: var(--faint); font-size: .66rem; font-weight: 600;
  letter-spacing: .09em; text-transform: uppercase;
}}
.route-stop-name {{
  font-family: var(--font-display); color: var(--ink); font-size: .97rem;
  font-weight: 600; letter-spacing: -.012em; margin-top: .08rem;
}}
.route-stop-task {{ color: var(--ink-2); font-size: .83rem; }}
.route-stop-travel {{ color: var(--muted); font-size: .78rem; margin-top: .12rem; }}
.route-stop-time {{
  color: var(--ink); font-size: .82rem; font-weight: 500; white-space: nowrap;
}}
.map-note {{
  color: var(--faint); font-size: .78rem; line-height: 1.5; margin-top: .6rem;
}}

/* --- Setup journey ---------------------------------------------------- */

.journey-panel {{ padding: 1.1rem 1.2rem; }}
.journey-eyebrow {{
  color: var(--faint); font-size: .68rem; font-weight: 600;
  letter-spacing: .11em; text-transform: uppercase; margin-bottom: .5rem;
}}
.journey-list {{ position: relative; }}
.journey-row {{
  display: grid; grid-template-columns: 1.4rem minmax(0, 1fr);
  gap: .8rem; align-items: start;
}}
.journey-copy {{ padding: .55rem 0; border-bottom: 1px solid var(--rule); }}
.journey-row:last-child .journey-copy {{ border-bottom: 0; }}
.journey-node {{
  width: .8rem; height: .8rem; border-radius: 50%; margin-top: .95rem;
  background: var(--heat); border: 1px solid var(--gold);
  display: grid; place-items: center;
  font-family: var(--font-mono); font-size: .58rem; color: var(--heat-ink);
}}
.journey-node.depot {{ background: var(--route); border-color: var(--route-line); }}
.journey-node.depot.pending, .journey-node.pending {{
  background: var(--surface); border: 1px dashed var(--rule-firm);
}}
.journey-node.return {{ background: var(--surface); border-color: var(--route-line); }}
.journey-kicker {{
  color: var(--faint); font-size: .64rem; font-weight: 600;
  letter-spacing: .09em; text-transform: uppercase;
}}
.journey-title {{
  font-family: var(--font-display); color: var(--ink); font-size: .92rem;
  font-weight: 600;
}}
.journey-meta {{ color: var(--muted); font-size: .8rem; margin-top: .08rem; }}

/* --- Streamlit widgets ------------------------------------------------ */

.stButton > button {{
  font-family: var(--font-ui); font-size: .9rem; font-weight: 600;
  border-radius: var(--r-md); border: 1px solid var(--rule-firm);
  padding: .62rem 1.1rem; transition: none;
}}
.stButton > button[kind="primary"] {{
  background: var(--ink); border-color: var(--ink); color: #FFFFFF;
}}
.stButton > button[kind="primary"]:hover:not(:disabled) {{
  background: #000; border-color: #000; color: #FFFFFF;
}}
.stButton > button[kind="primary"]:disabled {{ opacity: .38; }}
.stButton > button:not([kind="primary"]) {{
  background: var(--surface); color: var(--ink);
}}
.stButton > button:not([kind="primary"]):hover:not(:disabled) {{
  border-color: var(--ink); color: var(--ink);
}}
.stDownloadButton > button, .stLinkButton > a {{
  font-family: var(--font-ui); font-size: .88rem; font-weight: 500;
  border-radius: var(--r-md); border: 1px solid var(--rule-firm);
  background: var(--surface); color: var(--ink);
}}

[data-testid="stMetric"] {{
  background: var(--surface); border: 1px solid var(--rule);
  border-radius: var(--r-md); padding: .9rem 1rem;
}}
[data-testid="stMetricLabel"] {{
  color: var(--faint); font-size: .68rem !important; font-weight: 600;
  letter-spacing: .08em; text-transform: uppercase;
}}
[data-testid="stMetricValue"] {{ font-size: 1.4rem !important; font-weight: 500; }}

[data-testid="stExpander"] details {{
  border: 1px solid var(--rule); border-radius: var(--r-md);
  background: var(--surface);
}}
[data-testid="stExpander"] summary {{ font-size: .87rem; font-weight: 500; }}

.step-done {{
  display: flex; align-items: center; gap: .5rem;
  color: var(--ink-2); font-size: .87rem; padding: .12rem 0;
}}
.step-done .icon {{ color: var(--route-ink); }}

[data-testid="stCaptionContainer"], .stCaption {{
  color: var(--faint) !important; font-size: .79rem; line-height: 1.55;
}}

div[data-baseweb="input"] input, div[data-baseweb="select"] > div {{
  font-family: var(--font-ui); border-radius: var(--r-sm);
}}
[data-testid="stDataFrame"] {{
  border: 1px solid var(--rule); border-radius: var(--r-md); overflow: hidden;
}}
[data-testid="stAlert"] {{ border-radius: var(--r-md); font-size: .87rem; }}
hr {{ border-color: var(--rule); margin: 2.2rem 0; }}

/* --- Narrow screens --------------------------------------------------- */

@media (max-width: 760px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  h1 {{ font-size: 2rem; }}
  h1.hero-heading {{ font-size: 2.05rem; max-width: 100%; }}
  .wordmark-tag {{ margin-left: 0; padding-left: 0; border-left: 0; }}
  .process-arrow {{ display: none; }}
  .bento {{ grid-template-columns: 1fr; }}
  .bento-hero, .bento-tile {{ grid-column: span 1; grid-row: auto; }}
  .decision-card h2 {{ font-size: 1.75rem; }}
  .route-summary {{ grid-template-columns: 1fr; }}
  .timing-row {{ grid-template-columns: 3.2rem 1fr; }}
  .timing-tag {{ grid-column: 2; text-align: left; }}
  .route-stop {{ grid-template-columns: 2.4rem minmax(0, 1fr); }}
  .route-stop-time {{ grid-column: 2; }}
}}
</style>
"""

STYLESHEET = as_markup(FONT_LINKS, _CSS)

RESULT_MODE_STYLES = as_markup(
    """
<style>
.hero-heading, .hero-copy, .hero-proof, .process-strip { display: none; }
.wordmark { margin-bottom: 1rem; }
</style>
"""
)

__all__ = [
    "FONT_IMPORT",
    "FONT_LINKS",
    "RESULT_MODE_STYLES",
    "STYLESHEET",
    "as_markup",
    "icon",
]
