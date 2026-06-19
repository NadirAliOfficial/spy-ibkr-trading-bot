import logging
from collections import Counter

import resend

import config
from market import Candle, CandleRecord

logger = logging.getLogger(__name__)


def _build_data(sim_records: list[CandleRecord], candles: list[Candle]):
    rows = []
    mean = None

    if sim_records:
        scores = [r.sim_sl_hits for r in sim_records]
        counter = Counter(scores)
        total = len(scores)
        for score in sorted(counter):
            occ = counter[score]
            pct = f"{occ / total * 100:.2f}%"
            rows.append((score, occ, pct))

    if candles:
        ranges = [abs(c.close - c.open) for c in candles if c.open > 0]
        mean = sum(ranges) / len(ranges) if ranges else 0.0

    return rows, mean


def generate_report(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    rows, mean = _build_data(sim_records, candles)

    lines = ["Sample Post Trade Report :", ""]

    if rows:
        w1, w2, w3 = 40, 13, 11
        sep = "─" * (w1 + w2 + w3 + 4)
        lines += [
            sep,
            f"{'Stop Loss Triggers Per Candle (Score)':<{w1}} {'Occurrences':>{w2}} {'Percentage':>{w3}}",
            sep,
        ]
        for score, occ, pct in rows:
            lines.append(f"{score:<{w1}} {occ:>{w2}} {pct:>{w3}}")
        lines.append(sep)
    else:
        lines.append("No simulated SL data recorded.")

    if mean is not None:
        lines.append(f"\nMean Open-Close per 1-minute candle = {mean:.2f}")

    return "\n".join(lines)


def _generate_html(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    rows, mean = _build_data(sim_records, candles)

    th = "border:1px solid #ccc;padding:10px 16px;text-align:right;font-weight:normal;"
    td = "border:1px solid #ccc;padding:10px 16px;text-align:right;"
    td_label = "border:1px solid #ccc;padding:10px 16px;text-align:left;font-weight:bold;"

    table_rows = ""
    for score, occ, pct in rows:
        table_rows += f"<tr><td style='{td}'>{score}</td><td style='{td}'>{occ}</td><td style='{td}'>{pct}</td></tr>"

    mean_line = (
        f"<p style='font-size:15px;margin-top:24px;'>"
        f"<strong>Mean</strong> Open-Close per 1-minute candle = {mean:.2f}</p>"
        if mean is not None else ""
    )

    no_data = "" if rows else "<p>No simulated SL data recorded.</p>"

    return f"""
<div style="font-family:Arial,sans-serif;font-size:14px;color:#111;max-width:540px;margin:0 auto;">
  <p style="font-size:16px;margin-bottom:24px;">Sample Post Trade Report :</p>
  {no_data}
  {"" if not rows else f'''
  <table style="border-collapse:collapse;width:100%;">
    <thead>
      <tr>
        <th style="{td_label}">Stop Loss Triggers<br>Per Candle (Score)</th>
        <th style="{th}">Occurrences</th>
        <th style="{th}">Percentage</th>
      </tr>
    </thead>
    <tbody>{table_rows}</tbody>
  </table>'''}
  {mean_line}
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
