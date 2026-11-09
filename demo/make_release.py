#!/usr/bin/env python3
"""Write the demo press release with an embargo that lifts N seconds from now (default 70).

Usage: python3 demo/make_release.py [--seconds 70] [--out press/acme-series-b.md]
Same text every run; only the release time moves. No network.
"""
import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

BODY = """\
Acme Robotics raises $40 million Series B to put warehouse robots on every dock

AUSTIN, Texas. Acme Robotics said on Thursday that it closed a Series B round led by Northwind Capital, \
bringing total funding to $62 million. The company will use the money to hire 120 engineers and to ship its \
Dockhand robot to 300 warehouses before the end of next year.

"Every dock in the country still loads trucks by hand," said Mira Okafor, chief executive of Acme Robotics. \
"This round lets us fix that in two years, not ten."

Dockhand lifts 25 kilograms, works two full shifts on one charge and cut loading time by 38% in pilot sites.

-30-

About Acme Robotics
Acme Robotics builds loading robots for warehouses. Media contact: press@acme.example
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seconds", type=int, default=70)
    ap.add_argument("--out", default="press/acme-series-b.md")
    a = ap.parse_args()
    myt = timezone(timedelta(hours=8))
    until = (datetime.now(myt) + timedelta(seconds=a.seconds)).replace(microsecond=0)
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(f"EMBARGOED UNTIL {until.isoformat()}\n\n{BODY}", encoding="utf-8")
    print(f"wrote {out}: embargo lifts {until.isoformat()} (in {a.seconds} s)")


if __name__ == "__main__":
    main()
