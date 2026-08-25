# CertiRoute — design brief

Everything a designer needs to redesign this interface, and the constraints an
implementation has to survive. Written from the working application, not from
an idea of it: every number and every string below is one the app actually
produces.

---

## 1. What the product decides

**One decision: what time an outdoor crew should start work today or tomorrow.**

A dispatcher marks a crew base and 2–9 work sites on a map. CertiRoute reads
street-level heat from the FortyGuard API, predicts the hours ahead, and returns
a start time — with the visit order already worked out and a calibrated
uncertainty interval attached.

It is an operations tool, not a marketing page. The reference class is the
software a dispatcher already has open: Linear, Retool, a fleet console. Dense,
quiet, legible at a glance, no delight for its own sake.

**Users:** dispatchers and supervisors at utilities, telecoms, construction and
municipal works. Often on a laptop in a depot office. Not designers, not
analysts. They want the answer and the reason for it, in that order.

**The one thing that must never be lost:** this is a heat-*safety* adjacent
tool that deliberately does **not** claim to certify safety. Every screen keeps
a visible boundary saying so. That constraint is not decoration and cannot be
designed away.

---

## 2. The three modes

The date control drives everything. One picker, three behaviours.

| Mode | When | Anchor | What the user gets |
| --- | --- | --- | --- |
| **Planning today** | date = today | today's whole-day reading | a start time for today, filtered to hours that have not passed |
| **Planning tomorrow** | date = tomorrow | today's reading | a start time for tomorrow, wider interval |
| **Reviewing a finished day** | date ≤ today − 2 | that day's reading | measured route, plus optional grading of the model |

Dates in between (yesterday) are **refused**: FortyGuard publishes hourly
history about two days behind. That refusal is a designed state, not an error.

---

## 3. Page structure as it stands

Single scrolling page. Sections are numbered `[01]`, `[02]`, `[03]` in a mono
face at label scale.

```
┌ HERO BAND (dark, full-width card, rounded 22px) ─────────────────┐
│  wordmark + tagline                                              │
│  ─────────────────────────────────────────────────────────────   │
│  headline (2.75rem)          │  9/9      days it chose the best  │
│  supporting paragraph        │           possible start          │
│  lede line (mint)            │  20%–59%  of the day's heat       │
│  ─────────────────────────   │           avoided                 │
│  proof row: 3 icon + label   │  0.77 °C  mean error against      │
│                              │           measured heat           │
└──────────────────────────────────────────────────────────────────┘

  LANDING SHOWCASE  (first visit only — disappears once a point is placed)
  kicker: PHOENIX, ARIZONA · 20 AUGUST 2026
  title:  A day CertiRoute had never seen
  note:   It read one measurement — 34.7 °C across the area — then sent
          the crew at 05:00 instead of 08:00. Both runs are below.
  ┌ animated playback (iframe, self-contained SVG) ────────────────┐
  │  clock 11:41        [Replay the day] [2×]                      │
  │  route with numbered stops, two crews moving                   │
  │  ┌ USUAL 08:00 ────────┐ ┌ RECOMMENDED 05:00 ──────────────┐   │
  │  │ 42.6 °C  working at │ │ done          home at 12:48     │   │
  │  │ ▓▓▓▓▓▓▓░░░░         │ │ ▓▓▓▓▓▓▓▓▓▓▓▓                    │   │
  │  └─────────────────────┘ └─────────────────────────────────┘   │
  └────────────────────────────────────────────────────────────────┘

  PROGRESS GUIDE  (① Place the crew base → ② Add work sites → ③ Plan the shift)
    states: done (mint tick) / active (black, bold) / pending (dimmed)
    each carries a hover explanation

[01] Build a route from the map
     Tap the crew base, then every place the crew has to be.
     • city selector
     • instruction card (changes with progress)
     • map (left, ~1.6fr) + "Your workday" journey panel (right, 1fr)
     • Undo last point / Start over
     • collapsed: "Advanced: import work orders or load the walkthrough"

[02] Plan today's shift   (or "Plan tomorrow's shift")
     • summary card: N work sites · Phoenix, Arizona
     • [Plan today's shift] primary button
     • caption warning the call takes 3–5 minutes
     • progress log while it runs

[03] Today's plan  (or "Tomorrow's plan" / "Reviewing a finished day")
     • view switch: Crew route | Planner details
     • (see §4)

  SAFETY BOUNDARY  (always, every state)
```

---

## 4. The result — Crew route view

This is the screen that matters. Everything else exists to reach it.

### 4a. Decision card (bento, 6-col grid)

Hero tile spans 4 columns and 3 rows, mint background:

```
MOVE THE SHIFT
Start at 05:00
Beginning 3 hours earlier than your usual 08:00 cuts modelled heat
exposure by 25% for the same work. First stop is Phoenix City Hall.
```

Three stat tiles span 2 columns each:

| Label | Value | Icon |
| --- | --- | --- |
| Shift window | `05:00 – 13:44` | clock |
| Hottest working moment | `46.4 °C` | thermometer |
| Predicted within | `± 1.6 °C at 90%` | gauge |

The alternate state, when no earlier start helps: label **KEEP THE SHIFT**,
title **Start at 08:00 as usual**, and a body explaining that no earlier start
meaningfully reduces exposure.

### 4b. Watch the day

The same animated playback as the landing showcase, but for the user's own
plan. Two crews, same route, different start times. Plays automatically, has a
replay button and a 1×/2×/4× speed toggle.

### 4c. Why this start

Horizontal bars, one per candidate start. **Bar length = heat avoided against
the crew's usual start**, not absolute exposure — exposure never approaches
zero, so a zero-based bar makes every option look identical.

```
05:00  ████████████████████████  25% cooler   ← recommended (mint)
06:00  ████████████████████      22% cooler
07:00  ██████████                12% cooler
08:00  ▏                         your usual
```

Below it, two captions that appear conditionally:

- *"05:00, 06:00 already passed today, so they were not offered…"*
- *"4 site(s) cannot be visited before 08:30 because of their own access
  windows…"*

### 4d. Follow this route

- Two actions: **Open ordered stops in Google Maps**, **Download crew route (CSV)**
- Left (~1.45fr): route preview map with big numbered stops
- Right (1fr): a vertical rail — Crew base → numbered stops → Return to base

Each stop card carries: sequence number, kicker (*Start here* / *Next stop*),
site name, task, travel note, and a time range `05:00–05:55`.

---

## 5. The result — Planner details view

For dispatchers, reviewers and judges. Four metric cards, then evidence.

| Metric | Example | Help text explains |
| --- | --- | --- |
| Today's measured level | 34.7 °C | the only same-day signal the API returns |
| Held-out error | 0.77 °C | mean absolute error on days never trained on |
| Error at unseen sites | 0.82 °C | why one area model can serve arbitrary pins |
| Trained on | 20 days | dates and resolution |

Then:
- **Does the visit order matter today?** — usually an honest *no*, with the
  measured reason (site spread 0.32–2.32 °C vs diurnal swing 5.2–9.3 °C)
- **Predicted conditions at each stop** — a table of upper-bound temperatures
- The full safety boundary

---

## 6. States that must be designed, not treated as errors

These are frequent and normal. Each currently renders as a Streamlit
warning/info block, which is the weakest part of the interface.

1. **Reading not published yet** — before mid-morning, today's aggregate does
   not exist. *"FortyGuard has not published today's reading for this area yet."*
2. **Still computing** — the call takes 3–5 minutes and can outrun its budget.
   *"Press Plan again to pick the same reading back up — no second request is sent."*
3. **Day is spent** — planning at 19:00 when every start has passed.
   *"There is not enough of today left to run this shift."*
4. **Outside the trained area** — work sites beyond 60 km of trained ground.
5. **Untrained city** — only Phoenix, Houston and Miami have models.
6. **Hourly history not published** — reviewing yesterday.
7. **No route fits** — job windows and shift are infeasible.
8. **No API key** — button disabled with an explanation.

A designed empty/waiting state for #2 in particular would be worth a lot: four
minutes of silence currently looks like a hang.

---

## 7. Existing design system — keep or replace deliberately

### Colour (the palette is fixed; its use is not)

```
--ink        #0C1116     near-black, cool
--ink-2      #333D47     secondary text
--muted      #5C6873     body
--faint      #8A949E     labels
--rule       #E5E9EC     hairlines
--rule-firm  #CFD6DC     stronger borders
--canvas     #F7F8FA     page
--surface    #FFFFFF     cards

--route      #70FFD2     recommended path, crew base, cool
--route-ink  #05372A     text on mint
--heat       #FF9137     heat, numbered work stops
--heat-ink   #8A3B00     text on orange
--gold       #FFCC4D     timing and evidence emphasis
--caution    #FFFC8C     safety boundaries
```

Neutral-dominant. The four accents carry meaning: **mint = the recommended
path**, **orange = heat**. They are light, so they work as fills and markers
with dark ink on top, never as small text on white.

### Type

- **Instrument Sans** — display and headings
- **Inter** — interface text
- **JetBrains Mono** — every number a user compares down a column (times,
  temperatures, degree-hours), with tabular figures

Chosen from the convention in Typewolf's survey of well-regarded work: a
neo-grotesque carrying the interface, a mono for anything numeric.

### Icons

Inline SVG on a 24px grid, 1.75 stroke, round caps, `currentColor`. **No
emoji** — an emoji renders differently on every OS and cannot take a colour.
Current set: sunrise, thermometer, clock, pin, route, shield, gauge, check,
arrow-right, alert, layers, calendar, download, map.

### Spacing and shape

Radii 8 / 12 / 16 / 22px. One shadow, used sparingly.
Content column max-width 1180px.

---

## 8. Implementation constraints — please design within these

The app is **Streamlit**. This is not negotiable before the deadline, and it
imposes real limits:

1. **Every interaction is a full server round trip.** Measured: ~600 ms from
   click to re-render. Designs that assume instant feedback will feel broken.
   The map cannot accept a click during that window.
2. **The map is a Folium iframe.** Page CSS cannot reach inside it. Anything
   drawn on the map has to travel inside the map's own document.
3. **The playback is a separate iframe** with a fixed height set in Python.
   It is self-contained SVG + JS — no external libraries, nothing fetched, so
   it runs offline on a demo machine.
4. **Custom HTML is injected as markdown.** A blank line ends a raw-HTML block
   and dumps the rest onto the page as text. All injected markup passes through
   a helper that strips blank lines.
5. **Streamlit widgets** (buttons, date input, expanders, dataframes, metrics)
   are restyled through `data-testid` selectors. They can be restyled but not
   restructured.
6. **Google Fonts is the only external asset host** in use.

Given all that: **layout, hierarchy, colour, type, spacing, iconography, copy
and the designed states are all fair game.** Novel interaction patterns that
need sub-100 ms feedback are not.

---

## 9. Real content for mockups — please use these, not lorem

```
Headline      Start the shift before the heat does.
Sub           Outdoor crews work the hottest hours by default, because the
              shift was set long before anyone knew what the day would do.
Lede          CertiRoute reads today's street-level heat and tells you what
              time to begin — with the visit order already worked out.
Proof row     Today's real measurements · Calibrated interval · No synthetic fallback

Decision      MOVE THE SHIFT / Start at 05:00
              Beginning 3 hours earlier than your usual 08:00 cuts modelled
              heat exposure by 25% for the same work.

Stats         9/9 days it chose the best possible start
              20%–59% of the day's heat avoided
              0.77 °C mean error against measured heat

Sites         Phoenix City Hall · Chase Field · Eastlake Park ·
              Sky Harbor Terminal 4 · S'edav Va'aki Museum · Papago Park

Times         05:00–05:55 · 05:57–07:02 · 08:30–09:35 · 09:45–11:05
Temperatures  32.1 °C · 33.6 °C · 37.5 °C · 39.4 °C · 40.8 °C · 41.3 °C

Safety        Planning aid — not safety clearance. Confirm live site conditions
              and follow your heat-safety policy before dispatch.
```

---

## 10. What I would most like a designer to solve

In priority order:

1. **The waiting state.** Three to five minutes of nothing is the single worst
   moment in the product.
2. **The result hierarchy.** Decision, then playback, then bars, then route —
   four heavy blocks in a row. Which earns the top, and what collapses?
3. **The eight refusal states**, so they read as considered answers rather than
   Streamlit's default warning boxes.
4. **The map setup step**, which is where a first-time user spends the longest
   and currently gets the least design attention.
5. **Density.** A dispatcher will use this daily. The landing pitch should get
   out of the way faster than it currently does.
