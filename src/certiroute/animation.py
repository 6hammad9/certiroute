"""Play the day back, so the decision can be watched rather than read.

A table saying one shift avoids 24% of exposure is true and forgettable. The
same fact, watched, is not: two crews leave the same base on the same route,
and one is home before the heat arrives while the other is still out in it.

That race is the product's whole argument, which is why this is not
decoration. The recommended run is drawn in the route colour and the usual
run in the heat colour, both moving in real proportion to the clock, with each
crew's current temperature climbing beside it.

The output is one self-contained HTML document - inline SVG driven by
requestAnimationFrame, no external libraries, no 3D, nothing fetched. It runs
inside a Streamlit component iframe, offline, on any machine that will be
demonstrating it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import cos, radians

from certiroute.domain import GeoPoint
from certiroute.optimization import SchedulePlan, TemperatureProfile

# Matches the palette in theme.py: the recommended path is mint, heat is
# orange. Nothing here introduces a colour the rest of the product does not use.
ROUTE_COLOR = "#5980a6"
ROUTE_INK = "#1d2d3d"
HEAT_COLOR = "#2b2b2d"
HEAT_INK = "#2b2b2d"
INK = "#1d1f20"
MUTED = "#5d5d60"
FAINT = "#7a7a7d"
RULE = "color-mix(in srgb, #1d1f20 16%, transparent)"
SURFACE = "#f2f2f3"
CANVAS = "#e9e9ea"

VIEW_WIDTH = 760
VIEW_HEIGHT = 330
PADDING = 118


@dataclass(frozen=True)
class PlaybackRun:
    """One crew's day: a schedule and the label the viewer sees."""

    label: str
    plan: SchedulePlan
    color: str
    recommended: bool = False


def _project(
    points: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Flatten latitude/longitude onto the drawing surface.

    Longitude is scaled by cos(latitude) so the shape of a route is not
    stretched sideways; at metro distances that is accurate enough to be
    honest about which stops are near each other.
    """

    if not points:
        raise ValueError("at least one point is required")
    mean_latitude = sum(latitude for latitude, _ in points) / len(points)
    scale = max(cos(radians(mean_latitude)), 1e-6)
    flat = [(longitude * scale, latitude) for latitude, longitude in points]

    xs = [x for x, _ in flat]
    ys = [y for _, y in flat]
    span_x = max(max(xs) - min(xs), 1e-9)
    span_y = max(max(ys) - min(ys), 1e-9)
    usable_x = VIEW_WIDTH - 2 * PADDING
    usable_y = VIEW_HEIGHT - 2 * PADDING
    ratio = min(usable_x / span_x, usable_y / span_y)

    offset_x = (VIEW_WIDTH - span_x * ratio) / 2
    offset_y = (VIEW_HEIGHT - span_y * ratio) / 2
    return [
        (
            offset_x + (x - min(xs)) * ratio,
            # Screen y grows downward while latitude grows upward.
            VIEW_HEIGHT - offset_y - (y - min(ys)) * ratio,
        )
        for x, y in flat
    ]


def _temperature_series(
    profiles: Mapping[str, TemperatureProfile], job_id: str
) -> dict[int, float]:
    profile = profiles.get(job_id)
    if profile is None:
        return {}
    return {point.minute_of_day: point.temperature_c for point in profile.points}


def build_playback_payload(
    runs: Sequence[PlaybackRun],
    profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    threshold_c: float = 35.0,
) -> dict:
    """Everything the animation needs, resolved before it reaches the browser."""

    if not runs:
        raise ValueError("at least one run is required")
    reference = runs[0].plan
    if not reference.stops:
        raise ValueError("a run must contain at least one stop")

    coordinates = [(depot.latitude, depot.longitude)] + [
        (stop.latitude, stop.longitude) for stop in reference.stops
    ]
    projected = _project(coordinates)
    depot_point = projected[0]
    by_job = {
        stop.job_id: projected[index + 1] for index, stop in enumerate(reference.stops)
    }

    payload_runs = []
    for run in runs:
        legs = []
        for stop in run.plan.stops:
            x, y = by_job.get(stop.job_id, depot_point)
            legs.append(
                {
                    "seq": stop.sequence,
                    "name": stop.job_name.split(" — ")[0],
                    "x": round(x, 2),
                    "y": round(y, 2),
                    "arrive": stop.arrival_minute,
                    "start": stop.start_minute,
                    "finish": stop.finish_minute,
                    "temp": round(stop.temperature_c, 1),
                    "peak": round(stop.peak_temperature_c, 1),
                }
            )
        payload_runs.append(
            {
                "label": run.label,
                "color": run.color,
                "recommended": run.recommended,
                "stops": legs,
                # When the crew leaves base: the first arrival less the
                # travel it took to get there.
                "depart": (
                    run.plan.stops[0].arrival_minute
                    - run.plan.stops[0].inbound_travel_minutes
                ),
                "finish": run.plan.route_finish_minute,
                "hot": round(run.plan.minutes_above_planning_threshold),
                "exposure": round(run.plan.total_raw_exposure_units, 1),
            }
        )

    series = _temperature_series(profiles, reference.stops[0].job_id)
    starts = [run["depart"] for run in payload_runs]
    ends = [run["finish"] for run in payload_runs]
    return {
        "depot": {"x": round(depot_point[0], 2), "y": round(depot_point[1], 2)},
        # A fixed drawing box, always. Text and markers are sized in these
        # units, so a box that shrinks to fit a tightly grouped route would
        # render them enormous, and one taller than the frame would be cut off
        # at the bottom. _project already spreads any route across this box.
        "view": {"x": 0, "y": 0, "w": VIEW_WIDTH, "h": VIEW_HEIGHT},
        "runs": payload_runs,
        "curve": {str(minute): round(value, 1) for minute, value in series.items()},
        "from": min(starts),
        "to": max(ends),
        "threshold": threshold_c,
        "width": VIEW_WIDTH,
        "height": VIEW_HEIGHT,
    }


# Streamlit's content column at its 1180px maximum, less block and iframe
# padding. The stage scales to this width, so its drawn height follows from
# the fitted view and the frame can be sized to fit rather than guessed at.
STAGE_WIDTH_PX = 984
CHROME_HEIGHT_PX = 172


def playback_height(payload: dict, *, stage_width: int = STAGE_WIDTH_PX) -> int:
    """Exactly the height the stage and its cards occupy.

    The stage keeps its aspect at any width, so this follows from the drawing
    box rather than being guessed - no band of blank white underneath, and no
    route running off the bottom edge.
    """

    view = payload.get("view") or {"w": VIEW_WIDTH, "h": VIEW_HEIGHT}
    stage = stage_width * (float(view["h"]) / float(view["w"]))
    return int(round(stage)) + CHROME_HEIGHT_PX


def route_playback_html_from_payload(payload: dict) -> str:
    """Render a payload resolved earlier, such as the committed showcase."""

    return _TEMPLATE.replace("__PAYLOAD__", json.dumps(payload))


def route_playback_html(
    runs: Sequence[PlaybackRun],
    profiles: Mapping[str, TemperatureProfile],
    *,
    depot: GeoPoint,
    threshold_c: float = 35.0,
) -> str:
    """One self-contained document that animates the day."""

    payload = build_playback_payload(
        runs, profiles, depot=depot, threshold_c=threshold_c
    )
    data = json.dumps(payload)
    return _TEMPLATE.replace("__PAYLOAD__", data)


_TEMPLATE = """
<!doctype html>
<meta charset="utf-8">
<style>
:root {
  --ink: __INK__; --muted: __MUTED__; --faint: __FAINT__; --rule: __RULE__;
  --surface: __SURFACE__; --canvas: __CANVAS__;
  --route: __ROUTE__; --route-ink: __ROUTE_INK__;
  --heat: __HEAT__; --heat-ink: __HEAT_INK__;
}
* { box-sizing: border-box; }
body {
  margin: 0; background: var(--surface); color: var(--ink);
  font-family: Barlow, system-ui, -apple-system, "Segoe UI", sans-serif;
  border: 1px solid var(--rule); overflow: hidden;
}
.wrap { padding: 14px 16px 16px; }
.head {
  display: flex; align-items: center; justify-content: space-between;
  gap: 12px; flex-wrap: wrap; margin-bottom: 10px;
}
.clock {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 1.5rem; font-weight: 600;
  letter-spacing: -.02em;
}
.controls { display: flex; gap: 8px; align-items: center; }
button {
  font: 600 .82rem/1 'Barlow Condensed', system-ui, sans-serif; cursor: pointer;
  border: 1px solid var(--rule); background: var(--surface); color: var(--ink);
  border-radius: 0; padding: .5rem .85rem;
}
button:hover { border-color: var(--ink); }
button.primary { background: var(--ink); border-color: var(--ink); color: #fff; }
.stage { background: color-mix(in srgb, var(--route) 4%, transparent); }
.cards { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; margin-top: 12px; }
.card {
  border: 1px solid var(--rule); border-radius: 0; padding: .7rem .85rem;
  background: var(--surface);
}
.card .label {
  font-size: .64rem; font-weight: 600; letter-spacing: .09em;
  text-transform: uppercase; color: var(--faint);
}
.card .row {
  display: flex; align-items: baseline; justify-content: space-between;
  gap: .5rem; margin-top: .3rem;
}
.card .temp {
  font-family: "IBM Plex Mono", ui-monospace, monospace;
  font-variant-numeric: tabular-nums; font-size: 1.25rem; font-weight: 600;
}
.card .state { font-size: .76rem; color: var(--muted); }
.dot { width: .6rem; height: .6rem; border-radius: 50%; display: inline-block; }
.track {
  height: 5px; background: var(--canvas); border-radius: 0;
  margin-top: .5rem; overflow: hidden;
}
.fill { height: 100%; border-radius: 0; width: 0%; }
.done { opacity: .55; }
</style>
<div class="wrap">
  <div class="head">
    <div class="clock" id="clock">--:--</div>
    <div class="controls">
      <button class="primary" id="play">Replay the day</button>
      <button id="speed">1&times;</button>
    </div>
  </div>
  <svg class="stage" id="stage" width="100%"
       preserveAspectRatio="xMidYMid meet"
       role="img" aria-label="The crew route, played through the day"></svg>
  <div class="cards" id="cards"></div>
</div>
<script>
const DATA = __PAYLOAD__;
const SVG = "http://www.w3.org/2000/svg";
const stage = document.getElementById("stage");
const clockEl = document.getElementById("clock");
const cardsEl = document.getElementById("cards");
const playEl = document.getElementById("play");
const speedEl = document.getElementById("speed");

const SPEEDS = [1, 2, 4];
let speedIndex = 1;
let minute = DATA.from;
let running = true;
let last = null;

function label(m) {
  const h = Math.floor(m / 60), r = Math.round(m % 60);
  return String(h).padStart(2, "0") + ":" + String(r).padStart(2, "0");
}
function make(tag, attrs) {
  const node = document.createElementNS(SVG, tag);
  for (const key in attrs) node.setAttribute(key, attrs[key]);
  return node;
}

// --- static scene ---------------------------------------------------------
stage.setAttribute(
  "viewBox",
  [DATA.view.x, DATA.view.y, DATA.view.w, DATA.view.h].join(" ")
);
const base = DATA.runs[0];
const path = [DATA.depot, ...base.stops, DATA.depot];
let d = "";
path.forEach((p, i) => { d += (i ? " L " : "M ") + p.x + " " + p.y; });
stage.appendChild(make("path", {
  d, fill: "none", stroke: "#CFD6DC", "stroke-width": 2,
  "stroke-linecap": "round", "stroke-dasharray": "5 7",
}));

base.stops.forEach((s) => {
  stage.appendChild(make("circle", {
    cx: s.x, cy: s.y, r: 15, fill: "#FFFFFF",
    stroke: "#CFD6DC", "stroke-width": 1.5,
  }));
  const t = make("text", {
    x: s.x, y: s.y + 4.5, "text-anchor": "middle",
    "font-size": 12, "font-weight": 600, fill: "#5C6873",
    "font-family": "IBM Plex Mono, ui-monospace, monospace",
  });
  t.textContent = s.seq;
  stage.appendChild(t);
  const leftHalf = s.x < DATA.view.x + DATA.view.w / 2;
  const n = make("text", {
    x: s.x + (leftHalf ? -22 : 22), y: s.y + 4,
    "text-anchor": leftHalf ? "end" : "start",
    "font-size": 10.5, "font-weight": 500, fill: "#8A949E",
  });
  n.textContent = s.name;
  stage.appendChild(n);
});

stage.appendChild(make("circle", {
  cx: DATA.depot.x, cy: DATA.depot.y, r: 13,
  fill: "__ROUTE__", stroke: "#FFFFFF", "stroke-width": 2.5,
}));
const depotText = make("text", {
  x: DATA.depot.x, y: DATA.depot.y + 29, "text-anchor": "middle",
  "font-size": 10, "font-weight": 700, fill: "__ROUTE_INK__",
  "letter-spacing": ".08em",
});
depotText.textContent = "BASE";
stage.appendChild(depotText);

// --- one moving crew per run ---------------------------------------------
const ordered = DATA.runs
  .map((run, index) => ({ run, index }))
  .sort((a, b) => Number(a.run.recommended) - Number(b.run.recommended));
const crews = ordered.map(({ run }) => {
  const trail = make("path", {
    d: "", fill: "none", stroke: run.color, "stroke-width": 3.5,
    "stroke-linecap": "round", opacity: .9,
  });
  stage.appendChild(trail);
  const halo = make("circle", { r: 13, fill: run.color, opacity: .22 });
  const dot = make("circle", {
    r: 6.5, fill: run.color, stroke: "#FFFFFF", "stroke-width": 2,
  });
  stage.appendChild(halo);
  stage.appendChild(dot);

  const card = document.createElement("div");
  card.className = "card";
  card.innerHTML =
    '<div class="label"><span class="dot" style="background:' + run.color +
    '"></span> ' + run.label + "</div>" +
    '<div class="row"><span class="temp">--</span>' +
    '<span class="state">waiting</span></div>' +
    '<div class="track"><div class="fill" style="background:' +
    run.color + '"></div></div>';
  cardsEl.appendChild(card);

  return {
    run, trail, halo, dot,
    temp: card.querySelector(".temp"),
    state: card.querySelector(".state"),
    fill: card.querySelector(".fill"),
    card,
  };
});

// Where a crew is at a given minute: parked at a stop while working, or
// moving along the leg toward the next one.
function positionAt(run, m) {
  const stops = run.stops;
  if (m <= run.depart) return { p: DATA.depot, state: "at base", temp: null };
  for (let i = 0; i < stops.length; i++) {
    const s = stops[i];
    if (m >= s.start && m <= s.finish) {
      return { p: s, state: "working at " + s.name, temp: s.temp };
    }
    const from = i === 0 ? DATA.depot : stops[i - 1];
    const leaveAt = i === 0 ? run.depart : stops[i - 1].finish;
    if (m < s.arrive) {
      const span = Math.max(s.arrive - leaveAt, 1);
      const k = Math.min(Math.max((m - leaveAt) / span, 0), 1);
      return {
        p: { x: from.x + (s.x - from.x) * k, y: from.y + (s.y - from.y) * k },
        state: "travelling to " + s.name,
        temp: i === 0 ? s.temp : stops[i - 1].temp,
      };
    }
  }
  const lastStop = stops[stops.length - 1];
  if (m >= run.finish) return { p: DATA.depot, state: "home", temp: null };
  const span = Math.max(run.finish - lastStop.finish, 1);
  const k = Math.min(Math.max((m - lastStop.finish) / span, 0), 1);
  return {
    p: {
      x: lastStop.x + (DATA.depot.x - lastStop.x) * k,
      y: lastStop.y + (DATA.depot.y - lastStop.y) * k,
    },
    state: "returning to base",
    temp: lastStop.temp,
  };
}

function draw(m) {
  clockEl.textContent = label(m);
  crews.forEach((crew) => {
    const { run } = crew;
    const at = positionAt(run, Math.min(m, run.finish));
    crew.dot.setAttribute("cx", at.p.x);
    crew.dot.setAttribute("cy", at.p.y);
    crew.halo.setAttribute("cx", at.p.x);
    crew.halo.setAttribute("cy", at.p.y);

    // Trail: the legs already completed, plus the part of this one done.
    let d = "M " + DATA.depot.x + " " + DATA.depot.y;
    if (m > run.depart) {
      run.stops.forEach((s) => {
        if (m >= s.arrive) d += " L " + s.x + " " + s.y;
      });
      d += " L " + at.p.x + " " + at.p.y;
    }
    crew.trail.setAttribute("d", d);

    const finished = m >= run.finish;
    crew.card.classList.toggle("done", finished);
    if (finished) {
      crew.state.textContent = "home at " + label(run.finish);
      crew.temp.textContent = "done";
      crew.temp.style.color = "__MUTED__";
      crew.fill.style.width = "100%";
    } else {
      crew.state.textContent = at.state;
      crew.temp.textContent =
        at.temp === null ? "--" : at.temp.toFixed(1) + " \\u00b0C";
      crew.temp.style.color =
        at.temp !== null && at.temp >= DATA.threshold ? "__HEAT_INK__" : "__INK__";
      const span = Math.max(run.finish - run.depart, 1);
      const k = Math.min(Math.max((m - run.depart) / span, 0), 1);
      crew.fill.style.width = (k * 100).toFixed(1) + "%";
    }
    crew.dot.setAttribute("opacity", finished ? .35 : 1);
    crew.halo.setAttribute("opacity", finished ? .08 : .22);
  });
}

function frame(now) {
  if (last === null) last = now;
  const elapsed = (now - last) / 1000;
  last = now;
  if (running) {
    // One real second is forty minutes of the working day at 1x.
    minute += elapsed * 40 * SPEEDS[speedIndex];
    if (minute >= DATA.to) { minute = DATA.to; running = false; }
    draw(minute);
  }
  requestAnimationFrame(frame);
}

playEl.addEventListener("click", () => {
  minute = DATA.from; running = true; last = null;
});
speedEl.addEventListener("click", () => {
  speedIndex = (speedIndex + 1) % SPEEDS.length;
  speedEl.textContent = SPEEDS[speedIndex] + "\\u00d7";
});
speedEl.textContent = SPEEDS[speedIndex] + "\\u00d7";
draw(minute);
requestAnimationFrame(frame);
</script>
"""

for _token, _value in (
    ("__INK__", INK),
    ("__MUTED__", MUTED),
    ("__FAINT__", FAINT),
    ("__RULE__", RULE),
    ("__SURFACE__", SURFACE),
    ("__CANVAS__", CANVAS),
    ("__ROUTE_INK__", ROUTE_INK),
    ("__ROUTE__", ROUTE_COLOR),
    ("__HEAT_INK__", HEAT_INK),
    ("__HEAT__", HEAT_COLOR),
):
    _TEMPLATE = _TEMPLATE.replace(_token, _value)


__all__ = [
    "PlaybackRun",
    "playback_height",
    "route_playback_html_from_payload",
    "build_playback_payload",
    "route_playback_html",
]
