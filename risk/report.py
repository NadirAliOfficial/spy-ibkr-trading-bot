import smtplib
import logging
from collections import Counter
from email.mime.text import MIMEText

import config
from market import Candle, CandleRecord

logger = logging.getLogger(__name__)


def generate_report(sim_records: list[CandleRecord], candles: list[Candle]) -> str:
    lines = ["Sample Post Trade Report :", ""]

    if sim_records:
        scores = [r.sim_sl_hits for r in sim_records]
        counter = Counter(scores)
        total = len(scores)

        col1 = "Stop Loss Triggers Per Candle (Score)"
        col2 = "Occurrences"
        col3 = "Percentage"
        w1, w2, w3 = 40, 13, 11

        lines.append(f"{'─' * (w1 + w2 + w3 + 4)}")
        lines.append(f"{col1:<{w1}} {col2:>{w2}} {col3:>{w3}}")
        lines.append(f"{'─' * (w1 + w2 + w3 + 4)}")

        for score in sorted(counter):
            occ = counter[score]
            pct = f"{occ / total * 100:.2f}%"
            lines.append(f"{score:<{w1}} {occ:>{w2}} {pct:>{w3}}")

        lines.append(f"{'─' * (w1 + w2 + w3 + 4)}")
    else:
        lines.append("No simulated SL data recorded.")

    if candles:
        ranges = [abs(c.close - c.open) for c in candles if c.open > 0]
        mean = sum(ranges) / len(ranges) if ranges else 0.0
        lines.append(f"\nMean Open-Close per 1-minute candle = {mean:.2f}")

    return "\n".join(lines)


def save_report(report: str, path: str = "post_trade_report.txt"):
    with open(path, "w") as f:
        f.write(report)
    logger.info("Report saved to %s", path)


def email_report(report: str, subject: str = "SPY Bot Post-Trade Report"):
    to_addr = config.REPORT_EMAIL_TO
    from_addr = config.REPORT_EMAIL_FROM
    password = config.REPORT_EMAIL_PASS

    if not all([to_addr, from_addr, password]):
        logger.warning("Email not configured — skipping. Set REPORT_EMAIL_TO, REPORT_EMAIL_FROM, REPORT_EMAIL_PASS.")
        return

    msg = MIMEText(report)
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
            smtp.login(from_addr, password)
            smtp.sendmail(from_addr, to_addr, msg.as_string())
        logger.info("Report emailed to %s", to_addr)
    except Exception as e:
        logger.error("Failed to email report: %s", e)
