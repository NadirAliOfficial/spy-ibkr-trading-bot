import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches
from matplotlib.patches import FancyBboxPatch

ET_OFFSET = -4  # UTC-4 (EDT)

# (utc_time, candle_open_display, side, fill_price, note, ref_open_for_offset)
# candle_open_display=None → show "—" in Candle Open column (mid-candle or reverse)
# ref_open_for_offset    → use this for offset calc even when display is None
# "Reverse" in note      → never show offset regardless
rows = [
    #            utc        open    side    fill    note                          ref_open
    ("17:36:43", 744.08, "BUY",  744.09, "Y LONG entry #1",              744.08),
    ("17:37:01", 744.07, None,   None,   "Held LONG (position from 13:36)", None),
    ("17:38:00", 744.30, "BUY",  744.32, "Y LONG entry #1",              744.30),
    ("17:39:01", 744.38, None,   None,   "Held LONG",                     None),
    ("17:39:10", None,   "SELL", 744.29, "STP3 exit",                     None),
    ("17:39:29", 744.38, "BUY",  744.40, "Y LONG re-entry #1",           744.38),
    ("17:40:01", 744.46, None,   None,   "Held LONG",                     None),
    ("17:40:17", None,   "SELL", 744.43, "STP3 exit",                     None),
    ("17:40:26", None,   "SELL", 744.38, "Reverse → SHORT",               None),
    ("17:41:00", 744.39, None,   None,   "Held SHORT",                    None),
    ("17:41:03", None,   "BUY",  744.43, "STP3 exit",                     None),
    ("17:41:11", None,   "BUY",  744.47, "Reverse → LONG",                None),
    ("17:41:39", None,   "SELL", 744.34, "STP3 exit",                     None),
    ("17:41:49", None,   "SELL", 744.35, "Reverse → SHORT",               None),
    ("17:42:00", 744.37, None,   None,   "Held SHORT",                    None),
    ("17:42:01", None,   "BUY",  744.41, "STP3 exit",                     None),
    ("17:42:02", None,   "BUY",  744.44, "Reverse → LONG",                None),
    ("17:43:00", 744.37, "BUY",  744.40, "Y LONG entry #1",              744.37),
    ("17:43:19", None,   "SELL", 744.36, "Z SHORT entry #2",             744.37),  # same 13:43 candle
    ("17:44:00", 744.33, "SELL", 744.32, "Z SHORT entry #1",             744.33),
    ("17:44:16", None,   "BUY",  744.40, "Reverse → LONG",                None),
    ("17:45:00", 744.36, "SELL", 744.35, "Z SHORT entry #1",             744.36),
    ("17:45:45", None,   "SELL", 744.35, "Z SHORT entry #2",             744.36),  # same 13:45 candle
    ("17:46:00", 744.46, "BUY",  744.47, "Reverse → LONG",                None),
    ("17:46:13", None,   "SELL", 744.37, "Reverse → SHORT",               None),
    ("17:47:00", 744.55, "BUY",  744.55, "Reverse → LONG",                None),
    ("17:47:55", 744.55, "BUY",  744.57, "Y LONG entry #1",              744.55),
    ("17:49:01", 744.66, "SELL", 744.63, "STP3 exit",                    744.66),
    ("17:49:07", None,   "BUY",  744.68, "Y LONG entry #1",             744.66),  # same 13:49 candle
    ("17:50:00", 744.78, None,   None,   "Held LONG",                     None),
]

def utc_to_et(t):
    h, m, s = t.split(":")
    return f"{int(h)+ET_OFFSET:02d}:{m}:{s} ET"

def show_offset(ref_open, fill, note):
    if "Reverse" in note:
        return None
    if ref_open and fill:
        return fill - ref_open
    return None

fig, ax = plt.subplots(figsize=(13, 14))
ax.set_facecolor("#0d1117")
fig.patch.set_facecolor("#0d1117")
ax.axis("off")

ax.text(0.5, 0.97, "SPY Bot — Minute Candle Open vs Fill Price",
        transform=ax.transAxes, fontsize=15, fontweight="bold",
        color="white", ha="center", va="top", fontfamily="monospace")
ax.text(0.5, 0.94, "Paper Account DU6846499 · Session 13:36–13:50 ET · 22 Jun 2026",
        transform=ax.transAxes, fontsize=9, color="#8b949e",
        ha="center", va="top", fontfamily="monospace")

col_labels = ["Time (ET)", "Candle Open", "Side", "Fill Price", "Offset", "Note"]
col_x = [0.01, 0.16, 0.28, 0.37, 0.48, 0.57]

header_y = 0.89
row_h = 0.029
n_rows = len(rows)

rect = FancyBboxPatch((0.0, header_y - 0.005), 1.0, row_h + 0.005,
                      boxstyle="square,pad=0", transform=ax.transAxes,
                      color="#161b22", zorder=1)
ax.add_patch(rect)

for i, label in enumerate(col_labels):
    ax.text(col_x[i], header_y + 0.014, label,
            transform=ax.transAxes, fontsize=8.5, fontweight="bold",
            color="#58a6ff", va="center", fontfamily="monospace")

for r_idx, (utc, c_open, side, fill, note, ref_open) in enumerate(rows):
    y = header_y - (r_idx + 1) * row_h - 0.002
    bg = "#161b22" if r_idx % 2 == 0 else "#0d1117"
    rect = FancyBboxPatch((0.0, y - 0.003), 1.0, row_h,
                          boxstyle="square,pad=0", transform=ax.transAxes,
                          color=bg, zorder=1)
    ax.add_patch(rect)

    ax.text(col_x[0], y + 0.009, utc_to_et(utc), transform=ax.transAxes,
            fontsize=7.8, color="#c9d1d9", va="center", fontfamily="monospace")

    if c_open:
        ax.text(col_x[1], y + 0.009, f"{c_open:.2f}", transform=ax.transAxes,
                fontsize=7.8, color="#e6edf3", va="center", fontfamily="monospace", fontweight="bold")
    else:
        ax.text(col_x[1], y + 0.009, "—", transform=ax.transAxes,
                fontsize=7.8, color="#484f58", va="center", fontfamily="monospace")

    sc = "#3fb950" if side == "BUY" else "#f85149" if side == "SELL" else "#484f58"
    ax.text(col_x[2], y + 0.009, side or "—", transform=ax.transAxes,
            fontsize=7.8, color=sc, va="center", fontfamily="monospace", fontweight="bold")

    if fill:
        ax.text(col_x[3], y + 0.009, f"{fill:.2f}", transform=ax.transAxes,
                fontsize=7.8, color="#e6edf3", va="center", fontfamily="monospace")
    else:
        ax.text(col_x[3], y + 0.009, "—", transform=ax.transAxes,
                fontsize=7.8, color="#484f58", va="center", fontfamily="monospace")

    offset = show_offset(ref_open, fill, note)
    if offset is not None:
        oc = "#3fb950" if offset >= 0 else "#f85149"
        ax.text(col_x[4], y + 0.009, f"{offset:+.2f}", transform=ax.transAxes,
                fontsize=7.8, color=oc, va="center", fontfamily="monospace")
    else:
        ax.text(col_x[4], y + 0.009, "—", transform=ax.transAxes,
                fontsize=7.8, color="#484f58", va="center", fontfamily="monospace")

    nc = "#8b949e"
    if "Y LONG entry" in note:
        nc = "#58a6ff"
    elif "Z SHORT entry" in note:
        nc = "#79c0ff"
    elif "STP3" in note:
        nc = "#d29922"
    elif "Reverse" in note:
        nc = "#bc8cff"
    ax.text(col_x[5], y + 0.009, note, transform=ax.transAxes,
            fontsize=7.5, color=nc, va="center", fontfamily="monospace")

patches = [
    mpatches.Patch(color="#58a6ff", label="Y LONG entry (BUY STP @ Open+0.01)"),
    mpatches.Patch(color="#79c0ff", label="Z SHORT entry (SELL STP @ Open-0.01)"),
    mpatches.Patch(color="#d29922", label="STP3 protective exit"),
    mpatches.Patch(color="#bc8cff", label="Reverse position flip"),
    mpatches.Patch(color="#8b949e", label="Candle held / no new action"),
]
ax.legend(handles=patches, loc="upper left", bbox_to_anchor=(0.01, 0.055),
          facecolor="#161b22", edgecolor="#30363d", labelcolor="white",
          fontsize=8, framealpha=1)

ax.text(0.99, 0.01, "Leg=181 · Total=362 · Margin $1,116/share · Delayed data (paper)",
        transform=ax.transAxes, fontsize=7.5, color="#484f58",
        ha="right", va="bottom", fontfamily="monospace")

plt.tight_layout(pad=0.3)
plt.savefig("/Users/nadirali/ibkr/ugoabr-spy-bot/session_fills.png",
            dpi=150, bbox_inches="tight", facecolor="#0d1117")
print("Saved session_fills.png")
