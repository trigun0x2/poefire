"""Fuzzes the Client.txt line parser: any line must parse to an event or None, never raise.

Run: uv run --group fuzz python fuzz/fuzz_parser.py -max_total_time=60
"""

import sys

import atheris

with atheris.instrument_imports():
    from poefire.parser import parse_line

# real line shapes, so mutations reach the deeper branches
SEEDS = [
    "2026/11/01 12:00:00 123 abc [INFO Client 1] : You have entered Strand.",
    '2026/11/01 12:00:00 123 abc [DEBUG Client 1] Generating level 83 area "MapWorldsStrand" '
    "with seed 1",
    "2026/11/01 12:00:00 123 abc [INFO Client 1] : Speedy has been slain.",
    "2026/11/01 12:00:00 123 abc [INFO Client 1] : Speedy (Deadeye) is now level 74",
    "2026/11/01 12:00:00 123 abc [INFO Client 1] @From Someone: hi",
    "2026/11/01 12:00:00 123 abc [INFO Client 1] : You have been permanently weakened by "
    "Kitava's merciless affliction. You now have a total of -60% to all Resistances.",
]


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    if fdp.ConsumeBool():
        seed = SEEDS[fdp.ConsumeIntInRange(0, len(SEEDS) - 1)]
        cut = fdp.ConsumeIntInRange(0, len(seed))
        line = seed[:cut] + fdp.ConsumeUnicodeNoSurrogates(64) + seed[cut:]
    else:
        line = fdp.ConsumeUnicodeNoSurrogates(512)
    parse_line(line)


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
