#!/usr/bin/env python3
"""xTool Omni log viewer.

Usage:
    python3 omni_log_viewer.py                      # default log folder + this Mac's Studio logs
    python3 omni_log_viewer.py FOLDER_OR_FILES...   # any mix of folders, .gz exports and .zip files
    options: -o report.html   --no-local   --no-open   --json data.json

Every .gz / .zip in the log folder (and its sub-folders) is read: xTool Studio
log exports from the printer, and zips of xTool Studio's logs folder from any
computer. Overlapping exports are merged and duplicates discarded, so older
exports keep contributing history that has rotated out of newer ones.
"""

import argparse
import json
import os
import sys
import webbrowser

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import omni_parser  # noqa: E402
import studio_prices  # noqa: E402

# The same page renders the report here and in the browser-only version.
TEMPLATE = os.path.join(HERE, 'index.html')

# Where you drop log exports. Change this if you move the folder.
DEFAULT_LOG_DIR = os.path.expanduser('~/Library/CloudStorage/Dropbox/CoolBeans Design LLC/Omni O1 Logs')
# xTool Studio's own live log folder on this Mac (included unless --no-local).
LOCAL_STUDIO_LOGS = os.path.expanduser('~/Library/Application Support/xTool Studio/logs')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('inputs', nargs='*', help=f'folders or files (default: {DEFAULT_LOG_DIR})')
    ap.add_argument('-o', '--output', default=os.path.join(HERE, 'omni_report.html'))
    ap.add_argument('--no-local', action='store_true', help="don't read this computer's xTool Studio logs or ink prices")
    ap.add_argument('--json', help='also write the parsed data as JSON')
    ap.add_argument('--no-open', action='store_true', help="don't open the report in a browser")
    args = ap.parse_args()

    inputs = args.inputs or [DEFAULT_LOG_DIR]
    if not args.no_local and os.path.isdir(LOCAL_STUDIO_LOGS):
        inputs.append(LOCAL_STUDIO_LOGS)

    sources = omni_parser.load_sources(inputs)
    if not sources:
        sys.exit(f'No .gz or .zip log files found in: {", ".join(inputs)}')
    data = omni_parser.analyze(sources)
    data['sources'] = [s['file'] for s in data['stats']['sources']]
    # ink prices as set in xTool Studio on this computer (for the cost columns)
    data['prices'] = None if args.no_local else studio_prices.read_prices()

    payload = json.dumps(data, separators=(',', ':'), default=str).replace('</', '<\\/')
    with open(TEMPLATE, encoding='utf-8') as f:
        html = f.read().replace('/*__DATA__*/null', payload)
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(html)
    if args.json:
        with open(args.json, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=1, default=str)

    st = data['stats']
    print(f'Read {len(sources)} source(s):')
    for s in st['sources']:
        kind = []
        if s['device']:
            kind.append(f"printer logs to {s['deviceLast']}")
        if s['desktopDays']:
            kind.append(f"Studio logs {s['desktopDays'][0]} … {s['desktopDays'][-1]}")
        print(f"  {s['file']}: {', '.join(kind) or s.get('error') or 'no recognised logs'}")
    print(f"Duplicates discarded: {st['record']['duplicates']} printer events, "
          f"{st['appController']['duplicates']} controller log records, {st['desktopDropped']} Studio day-logs.")
    real = [j for j in data['jobs'] if not j['isTest']]
    print(f"Result: {len(real)} print jobs, "
          f"{sum(1 for e in data['events'] if e['kind'] == 'clean start')} cleans, {len(data['events'])} printer events.")
    print(f'Report written to {os.path.abspath(args.output)}')
    if not args.no_open:
        webbrowser.open('file://' + os.path.abspath(args.output))


if __name__ == '__main__':
    main()
