"""
Post-trade analysis report.

Outputs:
  1. Stop Loss Triggers per candle, sorted descending by score.
     Mode (most frequent score) shown first.
     Percentage of each score shown.

  2. Mean |Open - Close| across all 1-min candles (direction irrelevant).
"""
from collections import Counter
from sim_stop_loss import CandleRecord
from candle import Candle


def generate_report(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    lines = []
    lines.append("=" * 52)
    lines.append("POST-TRADE ANALYSIS")
    lines.append("=" * 52)

    # ---- Simulated SL hits per candle ----
    if sim_records:
        scores = [r.sim_sl_hits for r in sim_records]
        counter = Counter(scores)
        total = len(scores)
        mode_score = counter.most_common(1)[0][0]

        lines.append("\nSimulated Stop Loss Triggers Per Candle")
        lines.append(f"{'Score':<22} {'Occurrences':>12} {'Percentage':>12}")
        lines.append("-" * 52)

        # Sort descending by score, mode first
        sorted_scores = sorted(counter.keys(), reverse=True)
        # Put mode at top if not already highest
        if mode_score in sorted_scores and sorted_scores[0] != mode_score:
            sorted_scores.remove(mode_score)
            sorted_scores.insert(0, mode_score)

        for score in sorted_scores:
            occ = counter[score]
            pct = (occ / total) * 100
            marker = " *" if score == mode_score else ""
            lines.append(f"{score:<22} {occ:>12} {pct:>11.2f}%{marker}")

        lines.append(f"\n  Mode (most frequent) = {mode_score}")
        lines.append(f"  Total candles tracked = {total}")
    else:
        lines.append("\nNo simulated SL data recorded.")

    # ---- Mean |Open - Close| ----
    lines.append("")
    if candles:
        ranges = [abs(c.close - c.open) for c in candles if c.open > 0 and c.close > 0]
        if ranges:
            mean_range = sum(ranges) / len(ranges)
            lines.append(f"Mean |Open-Close| per 1-min candle = {mean_range:.4f}")
        else:
            lines.append("Mean |Open-Close|: no candle data")
    else:
        lines.append("Mean |Open-Close|: no candle data")

    lines.append("=" * 52)
    return "\n".join(lines)


def save_report(report: str, path: str = "post_trade_report.txt"):
    with open(path, "w") as f:
        f.write(report)
    print(f"Report saved to {path}")
