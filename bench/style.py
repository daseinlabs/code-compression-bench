"""Visual identity for the benchmark figures — the daseinlabs.ai look.

A single place that owns the palette, the matplotlib rcParams, and the gradient
helpers every figure pulls from. The brief: modern, minimalist, tech-forward;
a light canvas, a clean sans-serif, subtle HORIZON GRADIENT accents, and a
recurring embedding-vector motif. Crisp, high-contrast, launch-quality — never
default-matplotlib.

Nothing here is proprietary: it's a colour list, a few rcParams, and some
matplotlib gradient/patch utilities. Import `apply_style()` once at the top of a
figure module, then build with `PALETTE`, `arm_color()`, and the gradient
helpers below.

This module degrades gracefully: if matplotlib is not installed the colour
constants and `hex_to_rgb`/`mix` still import and work, so callers that only
need the palette (e.g. an HTML report) don't pull in matplotlib.
"""

from __future__ import annotations

from typing import Optional

# ── brand palette ────────────────────────────────────────────────────────────
# A light, airy canvas with a deep-ink foreground and a horizon gradient that
# runs from a warm dawn (amber/coral) up through the brand indigo into a cool
# cyan — the "horizon" the brand leans on. Accents are saturated but never neon.

INK = "#0B1020"        # near-black foreground (text, axes, strong marks)
CANVAS = "#FBFCFE"     # the light canvas (figure + axes background)
PANEL = "#FFFFFF"      # card / panel fill
MUTED = "#6B7280"      # secondary text, gridlines-as-text
GRID = "#E6E9F0"       # hairline gridlines
HAIRLINE = "#D7DCE6"   # subtle separators / spines

# Horizon gradient control stops (dawn -> indigo -> cyan), low to high.
HORIZON = ["#FF8A5B", "#F25C8A", "#6C4CF1", "#3B82F6", "#22D3EE"]

# Primary brand accents (the indigo->cyan core of the horizon).
INDIGO = "#6C4CF1"
VIOLET = "#8B5CF6"
BLUE = "#3B82F6"
CYAN = "#22D3EE"
CORAL = "#F25C8A"
AMBER = "#FF8A5B"
MINT = "#34D399"       # positive deltas / "newly solves"
ROSE = "#F43F5E"       # negative deltas / "newly fails"

# A categorical cycle for arms. Dasein gets the signature indigo; the rest are
# distinct, harmonious hues. `arm_color()` keeps a stable mapping by name.
PALETTE: list[str] = [INDIGO, CYAN, AMBER, MINT, VIOLET, BLUE, CORAL, "#94A3B8"]

# Stable per-arm colour assignment. The hero arm (Dasein) is pinned to the brand
# indigo; known peers get fixed hues so the leaderboard reads the same every run.
_ARM_COLORS: dict[str, str] = {
    "dasein": INDIGO,
    "baseline": "#94A3B8",   # neutral slate — the control
    "bear": AMBER,
    "woz": VIOLET,
    "edgee": CYAN,
    "rtk": MINT,
    "headroom": BLUE,
}


def arm_color(name: str, fallback_index: int = 0) -> str:
    """Stable colour for an arm. Known arms get a pinned hue; unknown arms cycle
    deterministically through PALETTE by `fallback_index`."""
    key = (name or "").lower()
    if key in _ARM_COLORS:
        return _ARM_COLORS[key]
    return PALETTE[fallback_index % len(PALETTE)]


# ── colour math (stdlib-only; safe to import without matplotlib) ──────────────
def hex_to_rgb(h: str) -> tuple[float, float, float]:
    """'#RRGGBB' -> (r, g, b) in 0..1."""
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255.0 for i in (0, 2, 4))  # type: ignore[return-value]


def rgb_to_hex(rgb: tuple[float, float, float]) -> str:
    return "#" + "".join(f"{max(0, min(255, round(c * 255))):02X}" for c in rgb)


def mix(c1: str, c2: str, t: float) -> str:
    """Linear blend of two hex colours; t=0 -> c1, t=1 -> c2."""
    a, b = hex_to_rgb(c1), hex_to_rgb(c2)
    return rgb_to_hex(tuple(a[i] + (b[i] - a[i]) * t for i in range(3)))  # type: ignore[arg-type]


def lighten(c: str, t: float) -> str:
    """Blend toward white by t in [0,1]."""
    return mix(c, "#FFFFFF", t)


def darken(c: str, t: float) -> str:
    """Blend toward ink by t in [0,1]."""
    return mix(c, INK, t)


def horizon_css(angle: str = "90deg", stops: Optional[list[str]] = None) -> str:
    """CSS linear-gradient string of the horizon palette (for the HTML report)."""
    cols = stops or HORIZON
    n = len(cols)
    parts = [f"{c} {round(100 * i / (n - 1))}%" for i, c in enumerate(cols)]
    return f"linear-gradient({angle}, " + ", ".join(parts) + ")"


# ── matplotlib glue (only touched when matplotlib is available) ───────────────
def apply_style() -> None:
    """Install the brand rcParams. Safe no-op if matplotlib is missing."""
    try:
        import matplotlib as mpl
        from cycler import cycler
    except Exception:
        return

    mpl.rcParams.update({
        # canvas
        "figure.facecolor": CANVAS,
        "axes.facecolor": CANVAS,
        "savefig.facecolor": CANVAS,
        "savefig.edgecolor": "none",
        "savefig.bbox": "tight",
        "savefig.dpi": 200,
        "figure.dpi": 130,
        # type — clean humanist sans, with safe fallbacks across platforms
        "font.family": "sans-serif",
        "font.sans-serif": [
            "Inter", "Helvetica Neue", "Segoe UI", "Arial",
            "DejaVu Sans", "sans-serif",
        ],
        "font.size": 11,
        "axes.titlesize": 13,
        "axes.titleweight": "bold",
        "axes.titlepad": 12,
        "axes.labelsize": 11,
        "axes.labelcolor": INK,
        "axes.labelweight": "medium",
        "text.color": INK,
        "xtick.color": MUTED,
        "ytick.color": MUTED,
        "xtick.labelsize": 10,
        "ytick.labelsize": 10,
        # spare frame: drop top/right spines, hairline the rest
        "axes.edgecolor": HAIRLINE,
        "axes.linewidth": 1.0,
        "axes.spines.top": False,
        "axes.spines.right": False,
        # restrained grid
        "axes.grid": True,
        "axes.grid.axis": "y",
        "grid.color": GRID,
        "grid.linewidth": 1.0,
        "grid.alpha": 1.0,
        "axes.axisbelow": True,
        # legend
        "legend.frameon": False,
        "legend.fontsize": 10,
        # marks
        "lines.linewidth": 2.4,
        "lines.solid_capstyle": "round",
        "patch.linewidth": 0,
        "axes.prop_cycle": cycler(color=PALETTE),
    })


def gradient_cmap(stops: Optional[list[str]] = None, name: str = "horizon"):
    """A matplotlib LinearSegmentedColormap over the given hex stops (default the
    horizon palette). Raises if matplotlib is unavailable."""
    from matplotlib.colors import LinearSegmentedColormap

    cols = stops or HORIZON
    return LinearSegmentedColormap.from_list(name, cols, N=256)


def gradient_fill_bar(ax, x: float, height: float, width: float,
                      c_lo: str, c_hi: str, *,
                      bottom: float = 0.0, vertical: bool = True,
                      radius: float = 0.0, zorder: int = 3):
    """Draw a single bar filled with a vertical gradient (c_lo at the base,
    c_hi at the top) by painting a clipped imshow gradient behind a bar-shaped
    clip path. Returns the AxesImage. matplotlib required.

    This is what gives the bars their "lit-from-below" horizon look instead of a
    flat fill.
    """
    import numpy as np
    from matplotlib.patches import FancyBboxPatch, Rectangle
    from matplotlib.colors import LinearSegmentedColormap

    cmap = LinearSegmentedColormap.from_list("bar", [c_lo, c_hi], N=256)
    x0, x1 = x - width / 2, x + width / 2
    y0, y1 = bottom, bottom + height

    if vertical:
        grad = np.linspace(0, 1, 256).reshape(-1, 1)  # bottom->top
    else:
        grad = np.linspace(0, 1, 256).reshape(1, -1)

    im = ax.imshow(grad, extent=(x0, x1, y0, y1), origin="lower",
                   aspect="auto", cmap=cmap, zorder=zorder, interpolation="bilinear")

    if radius and radius > 0:
        clip = FancyBboxPatch((x0, y0), width, height,
                              boxstyle=f"round,pad=0,rounding_size={radius}",
                              transform=ax.transData)
    else:
        clip = Rectangle((x0, y0), width, height, transform=ax.transData)
    im.set_clip_path(clip)
    return im


def style_axes(ax, *, ygrid: bool = True, xgrid: bool = False) -> None:
    """Final pass on an axes: hairline spines, grid only where asked, ticks out
    of the way. Call after plotting."""
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(HAIRLINE)
        ax.spines[side].set_linewidth(1.0)
    ax.tick_params(length=0)
    ax.set_axisbelow(True)
    if ygrid:
        ax.grid(True, axis="y", color=GRID, linewidth=1.0)
    else:
        ax.grid(False, axis="y")
    if xgrid:
        ax.grid(True, axis="x", color=GRID, linewidth=1.0)
    else:
        ax.grid(False, axis="x")


def vector_motif(ax, *, n: int = 7, color: str = INDIGO, alpha: float = 0.10,
                 seed: int = 7) -> None:
    """The embedding-vector motif: a faint sheaf of short arrows fanning from the
    lower-left, evoking vectors in a latent space. Purely decorative; drawn in
    axes (0..1) coordinates behind the data. matplotlib required."""
    import numpy as np
    from matplotlib.patches import FancyArrowPatch

    rng = np.random.default_rng(seed)
    ox, oy = 0.02, 0.04
    for i in range(n):
        ang = (np.pi / 2.4) * (i / max(n - 1, 1)) + rng.uniform(-0.04, 0.04)
        ln = rng.uniform(0.12, 0.26)
        dx, dy = ln * np.cos(ang), ln * np.sin(ang)
        arr = FancyArrowPatch(
            (ox, oy), (ox + dx, oy + dy),
            transform=ax.transAxes, mutation_scale=10,
            arrowstyle="-|>", color=color, alpha=alpha, lw=1.6, zorder=0,
        )
        ax.add_patch(arr)
