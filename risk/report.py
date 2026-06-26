import logging
from collections import Counter, defaultdict
from datetime import datetime
from zoneinfo import ZoneInfo

import resend

import config
from market import Candle, CandleRecord

logger = logging.getLogger(__name__)
ET = ZoneInfo("America/New_York")


def _fmt_time(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=ET).strftime("%H:%M:%S")


def _build_data(sim_records: list[CandleRecord], candles: list[Candle]):
    rows = []
    mean = None
    total_oc_lt5 = None

    if sim_records:
        scores = [r.sim_sl_hits for r in sim_records]
        counter = Counter(scores)
        total = len(scores)

        # Times of occurrence per score: candle open time formatted as HH:MM:SS ET
        times_by_score: dict[int, list[str]] = defaultdict(list)
        for r in sim_records:
            times_by_score[r.sim_sl_hits].append(_fmt_time(r.minute_ts))

        for score in sorted(counter):
            occ = counter[score]
            pct = f"{occ / total * 100:.2f}%"
            times = ", ".join(times_by_score[score])
            rows.append((score, occ, times, pct))

        # Total open-close points from candles with < 5 stop loss triggers
        # Match by minute_ts: build a candle close lookup
        candle_close: dict[float, float] = {}
        for r in sim_records:
            if r.close_price > 0:
                candle_close[r.minute_ts] = r.close_price
            elif r.open_price > 0:
                candle_close[r.minute_ts] = r.open_price  # fallback: use open

        oc_sum = sum(
            abs(candle_close.get(r.minute_ts, r.open_price) - r.open_price)
            for r in sim_records if r.sim_sl_hits < 5
        )
        total_oc_lt5 = round(oc_sum, 2)

    if candles:
        ranges = [abs(c.close - c.open) for c in candles if c.open > 0]
        mean = sum(ranges) / len(ranges) if ranges else 0.0

    return rows, mean, total_oc_lt5


def generate_report(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    rows, mean, total_oc_lt5 = _build_data(sim_records, candles)

    lines = ["SPY Bot — Post-Trade Report", ""]

    if rows:
        w1, w2, w4, w3 = 8, 13, 36, 11
        sep = "─" * (w1 + w2 + w4 + w3 + 6)
        lines += [
            sep,
            f"{'Score':<{w1}} {'Occurrences':>{w2}} {'Times of Occurrence':<{w4}} {'Percentage':>{w3}}",
            sep,
        ]
        for score, occ, times, pct in rows:
            lines.append(f"{score:<{w1}} {occ:>{w2}} {times:<{w4}} {pct:>{w3}}")
        lines.append(sep)
    else:
        lines.append("No simulated SL data recorded.")

    if mean is not None:
        lines.append(f"\nMean Open-Close per 1-minute candle = {mean:.2f}")

    if total_oc_lt5 is not None:
        lines.append(f"Total Open-Close points from candles with < 5 Stop Loss Triggers = {total_oc_lt5}")

    return "\n".join(lines)


def _generate_html(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    rows, mean, total_oc_lt5 = _build_data(sim_records, candles)

    th = "border:1px solid #ccc;padding:8px 12px;text-align:right;font-weight:normal;"
    th_l = "border:1px solid #ccc;padding:8px 12px;text-align:left;font-weight:normal;"
    td = "border:1px solid #ccc;padding:8px 12px;text-align:right;"
    td_l = "border:1px solid #ccc;padding:8px 12px;text-align:left;"
    td_label = "border:1px solid #ccc;padding:8px 12px;text-align:left;font-weight:bold;"

    table_rows = ""
    for score, occ, times, pct in rows:
        table_rows += (
            f"<tr><td style='{td}'>{score}</td>"
            f"<td style='{td}'>{occ}</td>"
            f"<td style='{td_l}'>{times}</td>"
            f"<td style='{td}'>{pct}</td></tr>"
        )

    mean_line = (
        f"<p style='font-size:15px;margin-top:24px;'>"
        f"<strong>Mean</strong> Open-Close per 1-minute candle = {mean:.2f}</p>"
        if mean is not None else ""
    )

    total_line = (
        f"<p style='font-size:15px;margin-top:8px;'>"
        f"Total Open-Close points from candles with &lt; 5 Stop Loss Triggers = {total_oc_lt5}</p>"
        if total_oc_lt5 is not None else ""
    )

    no_data = "" if rows else "<p>No simulated SL data recorded.</p>"

    return f"""
<div style="font-family:Arial,sans-serif;font-size:14px;color:#111;max-width:720px;margin:0 auto;">
  <p style="font-size:16px;margin-bottom:24px;">SPY Bot — Post-Trade Report</p>
  {no_data}
  {"" if not rows else f'''
  <table style="border-collapse:collapse;width:100%;">
    <thead>
      <tr>
        <th style="{td_label}">Score</th>
        <th style="{th}">Occurrences</th>
        <th style="{th_l}">Times of Occurrence</th>
        <th style="{th}">Percentage</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>'''}
  {mean_line}
  {total_line}
</div>
"""


def save_report(report: str, path: str = "post_trade_report.txt"):
    with open(path, "w") as f:
        f.write(report)
    logger.info("Report saved to %s", path)


def email_report(sim_records: list[CandleRecord], candles: list[Candle],
                 subject: str = "SPY Bot — Post-Trade Report"):
    api_key = config.RESEND_API_KEY
    to_addr = config.REPORT_EMAIL_TO

    if not api_key or not to_addr:
        logger.warning("Email not configured — skipping. Set RESEND_API_KEY and REPORT_EMAIL_TO in .env")
        return

    resend.api_key = api_key
    try:
        resend.Emails.send({
            "from": "SPY Bot <onboarding@resend.dev>",
            "to": [to_addr],
            "subject": subject,
            "html": _generate_html(sim_records, candles),
            "text": generate_report(sim_records, candles),
        })
        logger.info("Report emailed to %s", to_addr)
    except Exception as e:
        logger.error("Failed to email report: %s", e)
