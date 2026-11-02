"""Plot saved delivery results and separate judgments without external API calls.

Supply matplotlib through uv --with; the application does not depend on it.
The report is immutable input. Only the three named PNG files are replaced.
"""

import argparse
import hashlib
import json
from pathlib import Path

import matplotlib

matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.ticker import FuncFormatter
import numpy as np

from evals.delivery import summarize_report

COLORS = {'pass': '#18745b', 'partial': '#b77916', 'fail': '#b83d47', 'na': '#e7ebef'}


def load_results(report_path, judgments_path):
    """Reject mismatched annotations and incomplete/duplicate case coverage."""
    raw = report_path.read_bytes()
    report = json.loads(raw)
    judgments = json.loads(judgments_path.read_text())
    if judgments['report_sha256'] != hashlib.sha256(raw).hexdigest():
        raise ValueError('Judgments belong to a different report')
    bank_ids = [case['id'] for case in report['bank']['cases']]
    sample_ids = [sample['case_id'] for sample in report['samples']]
    judgment_ids = [case['case_id'] for case in judgments['cases']]
    if any(len(ids) != len(set(ids)) or set(ids) != set(bank_ids)
           for ids in (bank_ids, sample_ids, judgment_ids)):
        raise ValueError('Bank, results and judgments must cover the same unique cases')
    summary = summarize_report(report)
    metrics = {case['case_id']: case for case in summary['cases']}
    labels = {case['case_id']: case for case in judgments['cases']}
    rows = [dict(case, metrics=metrics[case['id']], judgment=labels[case['id']])
            for case in report['bank']['cases']]
    return rows, summary


def save(figure, output, name):
    """Render with consistent typography and close the figure after export."""
    figure.savefig(output / name, dpi=170, facecolor='white', bbox_inches='tight')
    plt.close(figure)


def plot_results(rows, summary, output):
    """Show outcome dimensions, case wall time and reported generation tokens."""
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({'font.size': 10, 'axes.spines.top': False,
                         'axes.spines.right': False, 'axes.titleweight': 'bold'})
    labels = [row['id'] + (' *' if row.get('setup') else '') for row in rows]
    codes = []
    for row in rows:
        metric, judgment = row['metrics'], row['judgment']
        provenance = metric['citation_provenance_valid']
        support = judgment['evidence_support']
        codes.append([
            0 if metric['status'] == 'ok' else 2,
            3 if provenance is None else (0 if provenance else 2),
            {'supported': 0, 'catalogue_supported': 0, 'partial': 1,
             'not_applicable': 3}[support],
            0 if judgment['task_success'] else 2,
        ])
    figure, axis = plt.subplots(figsize=(11, 9))
    axis.imshow(codes, cmap=ListedColormap(list(COLORS.values())), vmin=0, vmax=3,
                aspect='auto', interpolation='nearest')
    axis.set_xticks(range(4), ['Execution', 'Citation provenance', 'Evidence support', 'Task criteria'])
    axis.xaxis.tick_top()
    axis.tick_params(axis='both', length=0, pad=9)
    axis.set_yticks(range(len(rows)), labels)
    for y, values in enumerate(codes):
        for x, value in enumerate(values):
            axis.text(x, y, ['OK', 'Partial', 'Fail', 'n/a'][value], ha='center', va='center',
                      color='#4b5563' if value == 3 else 'white', fontsize=9)
    axis.set_title('Execution, valid citations and task success are different checks', pad=43, loc='left')
    figure.text(.01, .015, 'Provenance is mechanical. Support and task criteria use exploratory agent labels.\n'
                'n/a is not a failure. Catalogue support uses saved tool exchanges. * Includes a setup turn.',
                fontsize=9, color='#475569')
    figure.tight_layout(rect=(0, .065, 1, 1))
    save(figure, output, 'outcomes-by-case.png')

    ordered = sorted(rows, key=lambda row: row['metrics']['elapsed_ms'], reverse=True)
    names = [row['id'] + (' *' if row.get('setup') else '') for row in ordered]
    seconds = [row['metrics']['elapsed_ms'] / 1000 for row in ordered]
    figure, axis = plt.subplots(figsize=(11, 8))
    colors = ['#4966a4' if row.get('setup') else '#8ba5ca' for row in ordered]
    bars = axis.barh(names, seconds, color=colors)
    axis.invert_yaxis()
    axis.bar_label(bars, labels=[f'{value:.2f}' for value in seconds], padding=5, fontsize=9)
    axis.axvline(summary['elapsed_ms']['p50'] / 1000, color='#18745b', linestyle='--',
                 label=f"Observed p50: {summary['elapsed_ms']['p50'] / 1000:.2f} s", zorder=0)
    axis.axvline(summary['elapsed_ms']['p95'] / 1000, color='#b77916', linestyle=':',
                 label=f"Observed p95: {summary['elapsed_ms']['p95'] / 1000:.2f} s", zorder=0)
    axis.set_xlim(0, max(seconds) * 1.14)
    axis.set_xlabel('Seconds per case, including setup when present')
    axis.set_title('Observed latency by case', loc='left', pad=15)
    axis.legend(loc='lower right', frameon=False)
    axis.set_axisbelow(True)
    axis.grid(axis='x', alpha=.15)
    figure.text(.01, .015, 'One execution per case; three cases ran concurrently. '
                '* Two turns in one case. These are not HTTP load-test percentiles.', fontsize=9, color='#475569')
    figure.tight_layout(rect=(0, .045, 1, 1))
    save(figure, output, 'latency-by-case.png')

    ordered = sorted(rows, key=lambda row: row['metrics']['input_tokens'] + row['metrics']['output_tokens'],
                     reverse=True)
    names = [row['id'] + (' *' if row.get('setup') else '') for row in ordered]
    incoming = np.array([row['metrics']['input_tokens'] for row in ordered])
    outgoing = np.array([row['metrics']['output_tokens'] for row in ordered])
    totals = incoming + outgoing
    figure, axis = plt.subplots(figsize=(11, 8))
    axis.barh(names, incoming, color='#4966a4', label='Input tokens')
    axis.barh(names, outgoing, left=incoming, color='#dd9c42', label='Output tokens')
    axis.invert_yaxis()
    for i, value in enumerate(totals):
        axis.text(value + max(totals) * .012, i, f'{value:,}', va='center', fontsize=9)
    axis.set_xlim(0, max(totals) * 1.2)
    axis.xaxis.set_major_formatter(FuncFormatter(lambda value, _: f'{value / 1000:,.0f}k'))
    axis.set_xlabel('Provider-reported generation tokens; linear scale')
    axis.set_title('Token usage is concentrated in catalogue cases', loc='left', pad=15)
    axis.legend(loc='lower right', frameon=False)
    axis.set_axisbelow(True)
    axis.grid(axis='x', alpha=.15)
    figure.text(.01, .015, 'Input and output summed once from model calls. Embeddings excluded; no monetary estimate.\n'
                '* Includes setup. Tokens describe request volume, not task quality.', fontsize=9, color='#475569')
    figure.tight_layout(rect=(0, .065, 1, 1))
    save(figure, output, 'tokens-by-case.png')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, default=Path('data/delivery/run-2026-11-02.json'))
    parser.add_argument('--judgments', type=Path, default=Path('data/delivery/judgments.json'))
    parser.add_argument('--output', type=Path, default=Path('docs/experiments/images/delivery'))
    args = parser.parse_args()
    rows, summary = load_results(args.report, args.judgments)
    plot_results(rows, summary, args.output)
    print(f'Saved three delivery charts to {args.output}')


if __name__ == '__main__':
    main()
