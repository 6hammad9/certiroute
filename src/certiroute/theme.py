"""The visual system: an industrial blueprint, drawn in tokens.

The product is a planning instrument for depot dispatchers, so the interface is
built like a technical drawing rather than a consumer card layout: square
corners, hairline rules, transparent panels, and registration marks at the
corners of anything that matters. Barlow Condensed carries the headings, Barlow
the prose, IBM Plex Mono every number a dispatcher compares down a column.

Colour is almost entirely neutral. One blue-grey accent marks the recommended
path and the live state; nothing else is coloured, so when something is, it
means something. Both ramps run on one shared lightness scale, so the same step
of any role matches the others in visual value.

Icons are inline SVG on a 24px grid at 1.75 stroke, inheriting text colour.
Never emoji: an emoji renders as a different picture on every operating system
and cannot take a colour.
"""

from __future__ import annotations

from html import escape

FONT_IMPORT = (
    "@import url('https://fonts.googleapis.com/css2"
    "?family=Barlow:wght@400;500;600;700"
    "&family=Barlow+Condensed:wght@400;500;600;700"
    "&family=IBM+Plex+Mono:wght@400;500;600&display=swap');"
)

FONT_LINKS = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" href="https://fonts.googleapis.com/css2'
    "?family=Barlow:wght@400;500;600;700"
    "&family=Barlow+Condensed:wght@400;500;600;700"
    '&family=IBM+Plex+Mono:wght@400;500;600&display=swap">'
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
    "download": '<path d="M12 3v12"/><path d="m7 11 5 5 5-5"/><path d="M4 21h16"/>',
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


def corners() -> str:
    """The four registration marks that make a panel read as a drawing."""

    return (
        '<i class="corner tl"></i><i class="corner tr"></i>'
        '<i class="corner bl"></i><i class="corner br"></i>'
    )


def as_markup(*blocks: str) -> str:
    """Join HTML for injection, with every blank line removed.

    CommonMark ends a raw-HTML block at the first blank line. A stylesheet
    written with blank lines between its sections therefore stops being a
    stylesheet at the first section break, and every rule after it is rendered
    onto the page as paragraph text - the whole design system appears as
    visible source above the interface.

    Blank lines are stripped here so the CSS can still be authored with the
    spacing that makes it readable.
    """

    joined = "\n".join(blocks)
    return "\n".join(line for line in joined.splitlines() if line.strip())


_CSS = f"""
<style>
{FONT_IMPORT}

:root {{
  --color-bg: #f2f2f3;
  --color-surface: #e9e9ea;
  --color-text: #1d1f20;
  --color-accent: #5980a6;
  --color-divider: color-mix(in srgb, #1d1f20 16%, transparent);

  /* Tonal ramps on one shared lightness scale, so the same step of any role
     matches the others in visual value. */
  --n100: #f5f5f8; --n200: #e7e7ea; --n300: #d4d4d7; --n400: #b7b7ba;
  --n500: #98989b; --n600: #7a7a7d; --n700: #5d5d60; --n800: #424244;
  --n900: #2b2b2d;

  --alert: #9c5a33; --alert-wash: #f6efe9; --alert-deep: #7d4526;

  --a100: #eef6ff; --a200: #d6ebff; --a300: #b5d9fd; --a400: #94bce3;
  --a500: #749dc4; --a600: #597ea3; --a700: #416180; --a800: #2c455d;
  --a900: #1d2d3d;

  --font-heading: "Barlow Condensed", system-ui, sans-serif;
  --font-body: "Barlow", system-ui, sans-serif;
  --font-mono: "IBM Plex Mono", ui-monospace, "SFMono-Regular", monospace;

  --space-1: 3.4px; --space-2: 6.8px; --space-3: 10.2px;
  --space-4: 13.6px; --space-6: 20.4px; --space-8: 27.2px;
}}

html {{ color-scheme: light; }}
.stApp, [data-testid="stAppViewContainer"], .main {{
  background: var(--color-bg) !important;
  color: var(--color-text);
  font-family: var(--font-body);
  font-size: 15px;
}}
[data-testid="stHeader"] {{ background: transparent; }}
.block-container {{ max-width: 1180px; padding-top: .6rem; padding-bottom: 3rem; }}

/* --- Type -------------------------------------------------------------- */

h1, h2, h3, h4, h5, h6 {{
  font-family: var(--font-heading); font-weight: 600; line-height: 1.12;
  letter-spacing: -.015em; color: var(--color-text); margin: 0 0 var(--space-2);
}}
h1 {{ font-size: 44px; line-height: 1.02; }}
h2 {{ font-size: 28px; margin-top: 0; }}
h3 {{ font-size: 24px; margin-top: 0; }}
h4 {{ font-size: 20px; }}
p, li, label, .stMarkdown {{ font-family: var(--font-body); }}
.icon {{ flex: none; vertical-align: -.18em; }}

/* Every number a dispatcher compares down a column sits in the same place. */
.mono, .stat-figure, .fact-v, .rail-window, .bar-time,
[data-testid="stMetricValue"] {{
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
}}
.kicker {{
  font-family: var(--font-mono); font-size: 9.5px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--n600);
}}

/* --- Blueprint frame --------------------------------------------------- */

.blueprint {{ position: relative; border: 1px solid var(--color-divider); }}
.blueprint > .corner {{
  position: absolute; width: 11px; height: 11px;
  color: color-mix(in srgb, var(--color-text) 55%, transparent);
}}
.blueprint > .corner::before, .blueprint > .corner::after {{
  content: ""; position: absolute; background: currentColor;
}}
.blueprint > .corner::before {{ left: 5px; top: 0; width: 1px; height: 100%; }}
.blueprint > .corner::after {{ top: 5px; left: 0; width: 100%; height: 1px; }}
.blueprint > .corner.tl {{ top: -6px; left: -6px; }}
.blueprint > .corner.tr {{ top: -6px; right: -6px; }}
.blueprint > .corner.bl {{ bottom: -6px; left: -6px; }}
.blueprint > .corner.br {{ bottom: -6px; right: -6px; }}

/* --- Masthead ---------------------------------------------------------- */

.masthead {{
  border-bottom: 1px solid var(--color-divider);
  padding: 2px 0 8px; margin-bottom: 2px;
}}
.masthead-row {{ display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }}
.wordmark {{
  font-family: var(--font-heading); font-weight: 600; font-size: 20px;
  letter-spacing: .06em; text-transform: uppercase; color: var(--color-text);
}}
.masthead-tag {{
  font-family: var(--font-mono); font-size: 10.5px; letter-spacing: .08em;
  text-transform: uppercase; color: var(--n600);
}}
.masthead-area {{ margin-left: auto; display: flex; align-items: baseline; gap: 8px; }}
.masthead-area .value {{ font-size: 13px; color: var(--color-text); }}
.safety-strip {{
  display: flex; align-items: center; gap: 8px; margin-top: 8px;
  font-size: 11.5px; color: var(--n700);
}}
.safety-strip::before {{
  content: ""; width: 6px; height: 6px; background: var(--color-accent);
  display: block; flex: none;
}}

/* --- Hero -------------------------------------------------------------- */

.hero-band {{
  display: grid; grid-template-columns: 1.35fr 1fr; gap: 40px;
  align-items: start; padding: 20px 0 22px;
  border-bottom: 1px solid var(--color-divider);
}}
h1.hero-heading {{ margin: 0 0 10px; max-width: 22ch; }}
.hero-copy {{
  font-size: 15px; line-height: 1.5; color: var(--n700);
  max-width: 52ch; margin: 0 0 12px;
}}
.hero-proof {{
  display: flex; gap: 18px; flex-wrap: wrap; font-family: var(--font-mono);
  font-size: 10px; letter-spacing: .1em; text-transform: uppercase;
  color: var(--n600);
}}
.hero-stats {{
  display: grid; grid-template-columns: repeat(3, 1fr);
  border: 1px solid var(--color-divider);
}}
.hero-stats > div {{
  padding: 12px 12px 14px;
  border-right: 1px solid var(--color-divider);
}}
.hero-stats > div:last-child {{ border-right: 0; }}
.stat-figure {{ font-size: 22px; letter-spacing: -.02em; }}
.stat-note {{
  font-size: 11.5px;
  line-height: 1.35;
  color: var(--n700);
  margin-top: 4px;
}}

.proof-head {{
  display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap;
  margin: 24px 0 2px;
}}
.proof-kicker {{
  display: inline-flex; align-items: center; gap: 6px;
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--n600);
}}
.proof-title {{ font-family: var(--font-heading); font-size: 26px; font-weight: 600; }}
.proof-note {{
  font-size: 13px; line-height: 1.5; color: var(--n700);
  max-width: 74ch; margin: 2px 0 12px;
}}

/* --- Section headers --------------------------------------------------- */

.section-head {{ display: flex; align-items: baseline; gap: 12px; margin: 26px 0 4px; }}
.section-index {{
  font-family: var(--font-mono); font-size: 12px; letter-spacing: .04em;
  color: var(--a700); border-bottom: 1px solid var(--a300); padding-bottom: 1px;
}}
.section-blurb {{
  font-size: 13.5px; color: var(--n700); max-width: 74ch; margin: 0 0 14px;
}}

/* --- Steps ------------------------------------------------------------- */

.steps {{
  display: grid; grid-template-columns: repeat(3, 1fr);
  border: 1px solid var(--color-divider); margin-bottom: 22px;
}}
.step {{
  padding: 12px 14px; border-right: 1px solid var(--color-divider);
  display: flex; gap: 10px; align-items: flex-start;
}}
.step:last-child {{ border-right: 0; }}
.step-index {{ font-family: var(--font-mono); font-size: 11px; color: var(--a700); }}
.step-title {{ font-family: var(--font-heading); font-size: 16px; }}
.step-note {{ font-size: 11.5px; color: var(--n600); }}
.step.done {{ background: color-mix(in srgb, var(--color-accent) 6%, transparent); }}
.step.pending {{ opacity: .55; }}
.step.pending .step-index {{ color: var(--n500); }}

/* --- Decision ---------------------------------------------------------- */

.decision {{
  display: grid; grid-template-columns: repeat(6, 1fr);
  border: 1px solid var(--color-divider);
}}
.decision-main {{
  grid-column: span 4; padding: 24px 26px 22px;
  background: var(--color-accent); color: var(--color-bg);
  border-right: 1px solid var(--color-divider);
}}
.decision-label {{
  font-family: var(--font-mono); font-size: 10.5px; letter-spacing: .16em;
  margin-bottom: 8px; opacity: .9; text-transform: uppercase;
}}
.decision-main h3 {{
  margin: 0 0 10px;
  font-size: 40px;
  line-height: 1;
  color: var(--color-bg);
}}
.decision-main p {{ margin: 0; font-size: 14.5px; line-height: 1.5; max-width: 46ch; }}
.decision-side {{ grid-column: span 2; display: flex; flex-direction: column; }}
.decision-cell {{
  padding: 14px 18px;
  border-bottom: 1px solid var(--color-divider);
  flex: 1;
}}
.decision-cell:last-child {{ border-bottom: 0; }}
.decision-cell .value {{
  font-family: var(--font-mono); font-size: 22px; font-variant-numeric: tabular-nums;
  margin-top: 3px;
}}

/* --- Why this start ---------------------------------------------------- */

.why {{ border: 1px solid var(--color-divider); border-top: 0; }}
.why > summary {{
  display: flex; align-items: center; gap: 14px; padding: 12px 18px;
  cursor: pointer; list-style: none; font-size: 13px; color: var(--n800);
}}
.why > summary::-webkit-details-marker {{ display: none; }}
.why > summary:hover {{
  background: color-mix(in srgb, var(--color-accent) 6%, transparent);
}}
.why-label {{
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--a700);
}}
.why-toggle {{
  margin-left: auto;
  font-family: var(--font-mono);
  font-size: 11px;
  color: var(--n600);
}}
.why-body {{ padding: 4px 18px 18px; border-top: 1px solid var(--color-divider); }}
.why-note {{
  font-size: 12.5px;
  color: var(--n700);
  margin: 12px 0 14px;
  max-width: 78ch;
}}
.bar-row {{
  display: grid; grid-template-columns: 62px 1fr 150px; gap: 12px;
  align-items: center; padding: 5px 0;
}}
.bar-time {{ font-size: 13px; color: var(--n800); }}
.bar-track {{
  height: 16px;
  background: color-mix(in srgb, var(--color-text) 5%, transparent);
}}
.bar-fill {{ height: 100%; background: var(--a300); }}
.bar-row.picked .bar-fill {{ background: var(--color-accent); }}
.bar-note {{ font-size: 12px; color: var(--n700); }}
.bar-row.picked .bar-note {{ color: var(--a800); font-weight: 600; }}
.bar-aside {{
  display: flex;
  gap: 8px;
  font-size: 12px;
  color: var(--n700);
  margin-top: 6px;
}}
.bar-aside .mark {{ font-family: var(--font-mono); color: var(--a700); }}

/* --- Route rail -------------------------------------------------------- */

.rail-node {{
  display: flex; gap: 12px; padding: 11px 12px;
  border: 1px solid var(--color-divider); border-top: 0;
}}
.rail-node:first-child {{ border-top: 1px solid var(--color-divider); }}
.rail-node.base {{
  border-color: var(--color-accent);
  background: color-mix(in srgb, var(--color-accent) 8%, transparent);
}}
.rail-index {{
  font-family: var(--font-mono);
  font-size: 13px;
  width: 20px;
  color: var(--n800);
}}
.rail-node.base .rail-index {{ color: var(--a800); font-size: 11px; }}
.rail-kicker {{
  font-family: var(--font-mono); font-size: 9.5px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--a700);
}}
.rail-node.base .rail-kicker {{ color: var(--a800); }}
.rail-name {{ font-size: 13.5px; line-height: 1.3; }}
.rail-note {{ font-size: 11.5px; color: var(--n600); }}
.rail-window {{
  font-size: 11.5px; white-space: nowrap; color: var(--n800); margin-left: auto;
}}

/* --- Refusal plates ---------------------------------------------------- */

.gate {{
  display: grid; grid-template-columns: 1.3fr 1fr;
  background: color-mix(in srgb, var(--color-accent) 5%, transparent);
}}
.gate-main {{ padding: 24px 26px; }}
.gate-tags {{ display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }}
.gate-code {{
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
  padding: 3px 7px; background: var(--n900); color: var(--n100);
}}
.gate-kind {{
  font-family: var(--font-mono); font-size: 10px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--n600);
}}
.gate-main h2 {{ margin: 0 0 10px; font-size: 32px; max-width: 24ch; }}
.gate-body {{
  font-size: 14px;
  line-height: 1.55;
  color: var(--n800);
  max-width: 58ch;
  margin: 0 0 14px;
}}
.gate-consequence {{
  font-size: 13px;
  line-height: 1.55;
  color: var(--n700);
  max-width: 58ch;
  margin: 0;
}}
.gate-facts {{ padding: 24px 26px; border-left: 1px solid var(--color-divider); }}
.fact {{
  display: flex; justify-content: space-between; gap: 12px; padding: 8px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent);
  font-size: 12.5px;
}}
.fact-k {{ color: var(--n700); }}
.fact-v {{ text-align: right; }}
.gate-reassure {{
  font-size: 11.5px;
  line-height: 1.5;
  color: var(--n600);
  margin: 14px 0 0;
}}

/* --- Heat limit verdict ------------------------------------------------ */

.limit {{ border: 1px solid var(--color-divider); margin: 0 0 18px; }}
.limit-head {{
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap;
  padding: 10px 20px; border-bottom: 1px solid var(--color-divider);
  font-family: var(--font-mono); font-size: 10.5px;
  letter-spacing: .12em; text-transform: uppercase; color: var(--n600);
}}
.limit-flag {{ padding: 3px 9px; color: var(--n100); background: var(--n900); }}
.limit-body {{ padding: 18px 20px 6px; }}
.limit-body h4 {{
  margin: 0 0 8px; font-family: var(--font-display); font-weight: 600;
  font-size: 26px; line-height: 1.1; letter-spacing: .01em;
}}
.limit-body p {{
  margin: 0; font-size: 14px; line-height: 1.55;
  max-width: 62ch; color: var(--n800);
}}
.limit-rows {{ padding: 14px 20px 18px; display: grid; gap: 7px; }}
.limit-row {{
  display: grid; grid-template-columns: 5.5em 1fr auto;
  gap: 12px; align-items: baseline;
  font-size: 12.5px; padding-bottom: 6px;
  border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent);
}}
.limit-row .lr-start {{ font-family: var(--font-mono); font-size: 12px; }}
.limit-row .lr-note {{ color: var(--n700); }}
.limit-row .lr-peak {{
  font-family: var(--font-mono); font-variant-numeric: tabular-nums;
}}
.limit-row.is-clear .lr-peak {{ color: var(--a700); }}
.limit-row.is-over .lr-peak {{ color: var(--alert-deep); }}
.limit-row.is-baseline .lr-start {{ font-weight: 600; }}

.limit.v-clear {{
  background: color-mix(in srgb, var(--color-accent) 4%, transparent);
}}
.limit.v-clear .limit-flag {{ background: var(--a700); }}
.limit.v-move {{
  background: color-mix(in srgb, var(--color-accent) 7%, transparent);
}}
.limit.v-move .limit-flag {{ background: var(--a800); }}
.limit.v-none {{ background: var(--alert-wash); border-color: var(--alert); }}
.limit.v-none .limit-flag {{ background: var(--alert-deep); }}
.limit.v-none .limit-body h4 {{ color: var(--alert-deep); }}

@media (max-width: 640px) {{
  .limit-row {{ grid-template-columns: 4.5em 1fr; }}
  .limit-row .lr-peak {{ grid-column: 2; }}
}}

/* --- Panels and strips ------------------------------------------------- */

.summary-strip {{
  display: grid; grid-template-columns: 1fr auto; gap: 22px; align-items: center;
  border: 1px solid var(--color-divider); padding: 16px 18px; margin-top: 12px;
}}
.summary-line {{
  font-family: var(--font-mono);
  font-size: 13px;
  font-variant-numeric: tabular-nums;
}}
.summary-note {{ font-size: 12px; color: var(--n600); margin-top: 4px; }}
.result-strip {{
  display: flex; align-items: center; gap: 18px; flex-wrap: wrap;
  margin-top: 12px; border: 1px solid var(--color-divider); padding: 10px 14px;
  font-size: 12px; color: var(--n600);
}}
.result-strip .mono {{ font-size: 11.5px; color: var(--color-text); }}
.instruction {{
  border: 1px solid var(--color-divider);
  padding: 14px 16px;
  margin-bottom: 12px;
}}
.instruction.ready {{
  border-color: var(--color-accent);
  background: color-mix(in srgb, var(--color-accent) 7%, transparent);
}}
.instruction strong {{
  display: block; font-family: var(--font-heading); font-size: 17px;
  font-weight: 600; margin-bottom: 2px;
}}
.instruction span {{ font-size: 12.5px; color: var(--n700); }}
.empty-state {{ border: 1px solid var(--color-divider); padding: 26px 22px; }}
.empty-state h3 {{ margin: 0 0 6px; }}
.empty-state p {{ font-size: 13px; color: var(--n700); margin: 0; max-width: 60ch; }}
.map-note {{ font-size: 11.5px; color: var(--n600); margin-top: 6px; }}
.step-done {{
  display: flex; align-items: center; gap: .5rem; color: var(--n800);
  font-size: 13px; padding: .1rem 0;
}}
.step-done .icon {{ color: var(--a700); }}

/* --- Setup journey ------------------------------------------------------ */

.journey-panel {{ border: 1px solid var(--color-divider); padding: 14px 16px; }}
.journey-eyebrow {{
  font-family: var(--font-mono); font-size: 9.5px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--n600); margin-bottom: 8px;
}}
.journey-list {{ display: flex; flex-direction: column; }}
.journey-row {{
  display: grid; grid-template-columns: 14px minmax(0, 1fr); gap: 10px;
  align-items: start;
}}
.journey-copy {{
  padding: 8px 0;
  border-bottom: 1px solid color-mix(in srgb, var(--color-text) 8%, transparent);
}}
.journey-row:last-child .journey-copy {{ border-bottom: 0; }}
.journey-node {{
  width: 8px; height: 8px; margin-top: 15px;
  background: var(--color-accent); border: 1px solid var(--a700);
}}
.journey-node.pending {{ background: transparent; border-style: dashed; }}
.journey-node.return {{ background: var(--color-bg); }}
.journey-kicker {{
  font-family: var(--font-mono); font-size: 9px; letter-spacing: .12em;
  text-transform: uppercase; color: var(--a700);
}}
.journey-title {{
  font-family: var(--font-heading); font-size: 16px; font-weight: 600;
}}
.journey-meta {{ font-size: 11.5px; color: var(--n600); }}

/* --- Small blocks ------------------------------------------------------- */

.build-summary {{
  border: 1px solid var(--color-divider); padding: 14px 16px; margin: 10px 0 12px;
  font-size: 12.5px; line-height: 1.5; color: var(--n700);
}}
.build-summary strong {{
  display: block; font-family: var(--font-mono); font-size: 13px;
  color: var(--color-text); font-weight: 500; margin-bottom: 4px;
}}
.workday-chip {{
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  flex-wrap: wrap; border: 1px solid var(--color-divider); padding: 8px 12px;
  margin: 10px 0 4px; font-size: 12px; color: var(--n700);
}}
.workday-chip strong {{
  font-family: var(--font-heading); font-size: 15px; font-weight: 600;
  color: var(--color-text);
}}
.workday-chip span:last-child {{
  font-family: var(--font-mono); font-size: 11.5px; font-variant-numeric: tabular-nums;
}}
.safety-note {{
  border-left: 2px solid var(--color-accent); padding: 8px 0 8px 12px;
  margin-top: 14px; font-size: 12px; line-height: 1.55; color: var(--n700);
}}
.safety-note strong {{ color: var(--color-text); font-weight: 600; }}

/* --- Colophon ---------------------------------------------------------- */

.colophon {{
  margin-top: 30px; border-top: 1px solid var(--color-divider);
  padding: 18px 0 10px; display: grid; grid-template-columns: 1fr 1fr; gap: 30px;
}}
.colophon p {{ font-size: 13px; line-height: 1.55; margin: 0; max-width: 60ch; }}
.colophon-rows {{
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 12px;
  color: var(--n700);
}}
.colophon-row {{
  display: flex; justify-content: space-between;
  border-bottom: 1px solid var(--color-divider); padding-bottom: 5px;
}}
.colophon-row:last-child {{ border-bottom: 0; }}

/* --- Streamlit widgets ------------------------------------------------- */

.stButton > button, .stDownloadButton > button, .stLinkButton > a {{
  font-family: var(--font-heading); font-weight: 600; font-size: 14px;
  border-radius: 0; border: 1px solid var(--color-divider);
  padding: 9px 16px; transition: none; color: var(--color-text);
  background: transparent;
}}
.stButton > button[kind="primary"] {{
  background: var(--color-accent); border-color: var(--color-accent);
  color: var(--color-bg);
}}
.stButton > button[kind="primary"]:hover:not(:disabled) {{
  background: var(--a600); border-color: var(--a600); color: var(--color-bg);
}}
.stButton > button[kind="primary"]:disabled {{ opacity: .45; }}
.stButton > button:not([kind="primary"]):hover:not(:disabled) {{
  background: color-mix(in srgb, var(--color-text) 7%, transparent);
  border-color: var(--color-divider); color: var(--color-text);
}}
[data-testid="stMetric"] {{
  background: transparent; border: 1px solid var(--color-divider);
  border-radius: 0; padding: 14px 16px;
}}
[data-testid="stMetricLabel"] {{
  font-family: var(--font-mono); color: var(--n600);
  font-size: 9.5px !important; letter-spacing: .12em; text-transform: uppercase;
}}
[data-testid="stMetricValue"] {{ font-size: 26px !important; font-weight: 400; }}
[data-testid="stExpander"] details {{
  border: 1px solid var(--color-divider); border-radius: 0; background: transparent;
}}
[data-testid="stExpander"] summary {{ font-size: 13px; font-family: var(--font-body); }}
[data-testid="stCaptionContainer"], .stCaption {{
  color: var(--n600) !important; font-size: 11.5px; line-height: 1.5;
}}
div[data-baseweb="input"] input, div[data-baseweb="select"] > div {{
  font-family: var(--font-body); border-radius: 0; background: var(--color-surface);
}}
[data-testid="stDataFrame"] {{
  border: 1px solid var(--color-divider);
  border-radius: 0;
}}
[data-testid="stAlert"] {{ border-radius: 0; font-size: 13px; }}
hr {{ border-color: var(--color-divider); margin: 1.6rem 0; }}
/* A map click costs a server round trip of roughly 600ms, during which the map
   cannot accept another. That wait is made obvious rather than mysterious. */
[data-testid="stStatusWidget"] {{
  background: var(--n900) !important; color: var(--n100) !important;
  border-radius: 0 !important; border: 0 !important;
}}
[data-testid="stStatusWidget"] * {{ color: var(--n100) !important; }}

/* --- Narrow screens ---------------------------------------------------- */

@media (max-width: 820px) {{
  .block-container {{ padding-left: 1rem; padding-right: 1rem; }}
  h1 {{ font-size: 34px; }}
  .hero-band, .colophon {{ grid-template-columns: 1fr; gap: 20px; }}
  .decision {{ grid-template-columns: 1fr; }}
  .decision-main, .decision-side {{ grid-column: span 1; }}
  .decision-main {{ border-right: 0; border-bottom: 1px solid var(--color-divider); }}
  .decision-main h3 {{ font-size: 30px; }}
  .gate {{ grid-template-columns: 1fr; }}
  .gate-facts {{ border-left: 0; border-top: 1px solid var(--color-divider); }}
  .steps {{ grid-template-columns: 1fr; }}
  .step {{ border-right: 0; border-bottom: 1px solid var(--color-divider); }}
  .hero-stats {{ grid-template-columns: 1fr; }}
  .hero-stats > div {{
    border-right: 0;
    border-bottom: 1px solid var(--color-divider);
  }}
  .bar-row {{ grid-template-columns: 54px 1fr; }}
  .bar-note {{ grid-column: 2; }}
  .summary-strip {{ grid-template-columns: 1fr; }}
}}
</style>
"""

STYLESHEET = as_markup(FONT_LINKS, _CSS)

RESULT_MODE_STYLES = as_markup(
    """
<style>
.hero-band { display: none; }
.masthead { margin-bottom: 0; }
</style>
"""
)

__all__ = [
    "FONT_IMPORT",
    "FONT_LINKS",
    "RESULT_MODE_STYLES",
    "STYLESHEET",
    "as_markup",
    "corners",
    "icon",
]
