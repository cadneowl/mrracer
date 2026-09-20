"""Small inline-SVG charts, for showing a run's trajectory rather than its total.

A total says a review cost four dollars; only a shape says the last six requests
each took a minute, or that the context doubled halfway through and never came
back down. These are the pictures the panel draws beside the numbers.

Inline SVG and nothing else: no chart library, no client-side rendering, no
second request for data. A chart here is a string the server already has when
it answers, which is what lets the panel refresh one every few seconds through
htmx like any other fragment — and what lets it work with JavaScript switched
off entirely.

Colours are the dashboard's own CSS variables, so a chart follows the theme
instead of pinning its own palette, and every series is drawn in a different one
so a pipeline's steps can share an axis. Labels are escaped: a series is named
after a skill, and a skill is named in config.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from markupsafe import Markup, escape

# One per series, in order. Five is enough for any pipeline radar will run; a
# sixth series starts the cycle again rather than going uncoloured.
_COLOURS = ("var(--green)", "var(--blue)", "var(--amber)", "var(--red)", "var(--grey)")

# The drawing area, in viewBox units. The SVG scales to whatever width the panel
# gives it, so these are proportions rather than pixels.
_WIDTH = 620
_HEIGHT = 150
_LEFT = 54       # room for the y-axis labels
_RIGHT = 10
_TOP = 12
_BOTTOM = 26     # room for the x-axis labels

# Past this many points on one line, the dots stop being drawn and the line
# alone carries the shape. They are what the markup is mostly made of — a
# circle per request, per series, per chart — and a four-step pipeline at the
# sample cap came to 600 KB of SVG, re-sent every few seconds to every open
# panel while the run works. By then they have stopped being individually
# readable anyway: at a point every two pixels, a row of dots is a thick line.
_MAX_DOTS = 150


@dataclass(frozen=True)
class Series:
    """One line or set of bars: a label, and points in (x, y)."""

    label: str
    points: tuple[tuple[float, float], ...]


def _nice_top(value: float) -> float:
    """A round number at or above the highest point, for the top gridline.

    Charts that end exactly on their tallest point read as if the data were cut
    off, and an axis labelled 6.83 reads as noise. This is the 1/2/5 ladder
    every plotting library uses, written out because importing one to draw four
    lines is not a trade worth making.
    """
    if value <= 0:
        return 1.0
    magnitude = 10 ** (len(str(int(value))) - 1)
    for step in (1, 2, 2.5, 5, 10):
        top = step * magnitude
        if top >= value:
            return float(top)
    return float(10 * magnitude)


def _format_seconds(value: float) -> str:
    """An x-axis label: seconds while a run is short, minutes once it is not."""
    if value < 90:
        return f"{value:.0f}s"
    return f"{value / 60:.0f}m"


def chart(
    series: list[Series],
    *,
    title: str,
    caption: str = "",
    y_format: Callable[[float], str] = lambda v: f"{v:g}",
    kind: str = "line",
    reference: tuple[float, str] | None = None,
) -> Markup:
    """One chart: series over a shared x axis, with a y axis and a caption.

    ``kind`` is ``line`` for something continuous (a wait, a context size) or
    ``bars`` for a count per request. ``reference`` draws one dashed line across
    the plot — a mean, or the context window a run is filling up.

    A series with a single point still draws: the first request of a run is
    exactly when someone is watching, and an empty frame that fills in later
    looks broken.
    """
    drawn = [s for s in series if s.points]
    if not drawn:
        return Markup("")

    xs = [x for s in drawn for x, _ in s.points]
    ys = [y for s in drawn for _, y in s.points]
    x_min, x_max = min(xs), max(xs)
    x_span = (x_max - x_min) or 1.0
    y_top = _nice_top(max([*ys, reference[0] if reference else 0]))

    plot_w = _WIDTH - _LEFT - _RIGHT
    plot_h = _HEIGHT - _TOP - _BOTTOM

    def px(x: float) -> float:
        return _LEFT + (x - x_min) / x_span * plot_w

    def py(y: float) -> float:
        return _TOP + plot_h - (min(y, y_top) / y_top) * plot_h

    parts = [
        # No `preserveAspectRatio="none"`: stretching the viewBox to whatever
        # box the panel gives it squashes the axis labels and turns every
        # request's dot into an ellipse. The stylesheet gives the box this
        # same ratio instead, so there is nothing left to stretch.
        f'<svg class="chart" viewBox="0 0 {_WIDTH} {_HEIGHT}" role="img" '
        f'aria-label="{escape(title)}">',
        f"<title>{escape(title)}</title>",
    ]

    # Gridlines, bottom to top, each with the value it stands for.
    for fraction in (0.0, 0.5, 1.0):
        y = _TOP + plot_h - fraction * plot_h
        parts.append(
            f'<line class="chart-grid" x1="{_LEFT}" y1="{y:.1f}" '
            f'x2="{_WIDTH - _RIGHT}" y2="{y:.1f}" />'
        )
        parts.append(
            f'<text class="chart-axis" x="{_LEFT - 6}" y="{y + 3:.1f}" '
            f'text-anchor="end">{escape(y_format(y_top * fraction))}</text>'
        )

    if reference is not None:
        value, label = reference
        y = py(value)
        parts.append(
            f'<line class="chart-ref" x1="{_LEFT}" y1="{y:.1f}" '
            f'x2="{_WIDTH - _RIGHT}" y2="{y:.1f}" />'
        )
        parts.append(
            f'<text class="chart-axis chart-ref-label" x="{_WIDTH - _RIGHT}" '
            f'y="{y - 4:.1f}" text-anchor="end">{escape(label)}</text>'
        )

    for index, one in enumerate(drawn):
        colour = _COLOURS[index % len(_COLOURS)]
        points = sorted(one.points)
        if kind == "bars":
            # Narrow enough that a busy run stays readable, and never wider than
            # the gap between two requests. Several series share the slot side
            # by side rather than on top of each other: two steps of a pipeline
            # that answered at the same moment would otherwise be one bar, and
            # the taller one would be the only one anybody saw.
            slot = max(3.0, min(12.0, plot_w / max(len(points), 1) * 0.6))
            width = max(1.5, slot / len(drawn))
            offset = (index - (len(drawn) - 1) / 2) * width
            for x, y in points:
                height = max(0.0, _TOP + plot_h - py(y))
                parts.append(
                    f'<rect x="{px(x) + offset - width / 2:.1f}" y="{py(y):.1f}" '
                    f'width="{width:.1f}" height="{height:.1f}" fill="{colour}" '
                    f'opacity="0.8" />'
                )
        else:
            path = " ".join(f"{px(x):.1f},{py(y):.1f}" for x, y in points)
            parts.append(
                f'<polyline fill="none" stroke="{colour}" stroke-width="1.6" '
                f'stroke-linejoin="round" points="{path}" />'
            )
            # A dot per request: the line says the trend, the dots say how many
            # requests it took and where each one landed. Only while they can
            # still be told apart (see `_MAX_DOTS`).
            if len(points) <= _MAX_DOTS:
                radius = 2.2 if len(points) <= 60 else 1.2
                for x, y in points:
                    parts.append(
                        f'<circle cx="{px(x):.1f}" cy="{py(y):.1f}" r="{radius}" '
                        f'fill="{colour}" />'
                    )

    parts.append(
        f'<text class="chart-axis" x="{_LEFT}" y="{_HEIGHT - 8}">'
        f"{escape(_format_seconds(x_min))}</text>"
    )
    parts.append(
        f'<text class="chart-axis" x="{_WIDTH - _RIGHT}" y="{_HEIGHT - 8}" '
        f'text-anchor="end">{escape(_format_seconds(x_max))}</text>'
    )
    parts.append("</svg>")

    legend = ""
    if len(drawn) > 1:
        # Only a pipeline needs one: a single skill's chart is about itself.
        swatches = "".join(
            f'<span class="chart-key"><i style="background:'
            f'{_COLOURS[i % len(_COLOURS)]}"></i>{escape(s.label)}</span>'
            for i, s in enumerate(drawn)
        )
        legend = f'<div class="chart-legend">{swatches}</div>'

    return Markup(
        f'<figure class="chart-box"><figcaption class="chart-title">{escape(title)}'
        f'{f"<span>{escape(caption)}</span>" if caption else ""}</figcaption>'
        f'{"".join(parts)}{legend}</figure>'
    )
