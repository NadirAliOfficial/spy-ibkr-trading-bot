from collections import Counter
from market import Candle, CandleRecord


def generate_report(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    lines = ["=" * 52, "POST-TRADE ANALYSIS", "=" * 52]

    if sim_records:
        scores = [r.sim_sl_hits for r in sim_records]
        counter = Counter(scores)
        total = len(scores)
        mode = counter.most_common(1)[0][0]

        lines += [
            "",
            "Simulated Stop Loss Triggers Per Candle",
            f"{'Score':<22} {'Occurrences':>12} {'Percentage':>12}",
            "-" * 52,
        ]

        for score in sorted(counter, key=lambda s: (s != mode, -s)):
            occ = counter[score]
            tag = " *" if score == mode else ""
            lines.append(f"{score:<22} {occ:>12} {occ/total*100:>11.2f}%{tag}")

        lines += ["", f"  Mode = {mode}  |  Total candles = {total}"]
    else:
        lines.append("\nNo simulated SL data recorded.")

    if candles:
        ranges = [abs(c.close - c.open) for c in candles if c.open > 0]
        mean = sum(ranges) / len(ranges) if ranges else 0.0
        lines.append(f"\nMean |Open-Close| per 1-min candle = {mean:.4f}")

    lines.append("=" * 52)
    return "\n".join(lines)


def save_report(report: str, path: str = "post_trade_report.txt"):
    with open(path, "w") as f:
        f.write(report)
    print(f"Report saved to {path}")
