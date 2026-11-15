"""Generate an offline, self-contained replay from actual curb_parking results."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from curb_parking import scenarios, simulate


def render(data: dict) -> str:
    # The names/IDs are data, never executable HTML/script content.
    raw = json.dumps(data, ensure_ascii=False, allow_nan=False, separators=(',', ':'))
    raw = raw.replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    template = Path(__file__).with_name('curb_replay_template.html').read_text()
    return template.replace('__SIMULATION_DATA__', raw)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=Path('work/curb-parking-qa/replay.html'))
    args = parser.parse_args()
    data = {name: simulate(*scenario) for name, scenario in scenarios().items()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(data))
    args.output.with_suffix('.json').write_text(json.dumps(data, ensure_ascii=False, allow_nan=False, indent=2))
    print(args.output.resolve())


if __name__ == '__main__':
    main()
