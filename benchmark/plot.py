"""Render the dual-panel benchmark graph (drop rate + P99 latency).

Usage:
    python benchmark/plot.py
    python benchmark/plot.py --async-file path/results_async.json --sync-file path/results_sync.json
"""

import argparse
import json
import logging
from pathlib import Path

import matplotlib

matplotlib.use("Agg")

import matplotlib.pyplot as plt

logger = logging.getLogger("benchmark.plot")

SERIES_STYLE = {
    "async": {"label": "Async Gateway", "color": "blue", "marker": "o", "linestyle": "--"},
    "sync": {"label": "Synchronous Baseline", "color": "red", "marker": "s", "linestyle": "-"},
}


def load_results(path):
    path = Path(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("could not read %s: %s", path, exc)
        return None
    if not isinstance(data, dict) or not data.get("levels"):
        logger.warning("no levels found in %s", path)
        return None
    return data


def series_from(data):
    concurrency = []
    drop_rate = []
    p99_latency_s = []
    for level in data["levels"]:
        if not isinstance(level, dict) or "concurrency" not in level:
            continue
        concurrency.append(level["concurrency"])
        drop_rate.append(level.get("drop_rate", 0.0) * 100.0)
        p99_ms = level.get("p99_latency_ms")
        p99_latency_s.append(p99_ms / 1000.0 if p99_ms is not None else None)
    return concurrency, drop_rate, p99_latency_s


def plot_results(datasets, output):
    fig, (ax_drop, ax_latency) = plt.subplots(2, 1, sharex=True, figsize=(8.0, 16.0 / 3.0))

    for name, data in datasets.items():
        if data is None:
            continue
        style = SERIES_STYLE[name]
        concurrency, drop_rate, p99_latency_s = series_from(data)
        ax_drop.plot(
            concurrency, drop_rate,
            label=style["label"],
            color=style["color"], marker=style["marker"], linestyle=style["linestyle"],
        )
        points = [(c, v) for c, v in zip(concurrency, p99_latency_s) if v is not None]
        if points:
            xs, ys = zip(*points)
            ax_latency.plot(
                xs, ys,
                label=style["label"],
                color=style["color"], marker=style["marker"], linestyle=style["linestyle"],
            )

    ax_drop.set_title("Drop Rate vs Concurrent Requests")
    ax_drop.set_ylabel("Drop Rate (%)")
    ax_drop.set_ylim(0, 100)
    ax_drop.legend()

    ax_latency.set_title("P99 Latency vs Concurrent Requests")
    ax_latency.set_xlabel("Concurrent Requests")
    ax_latency.set_ylabel("P99 Latency (seconds)")
    ax_latency.legend()

    for ax in (ax_drop, ax_latency):
        ax.grid(visible=True, alpha=0.35)

    fig.tight_layout()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output, dpi=150)
    logger.info("wrote %s", output)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--async-file", default="benchmark/results_async.json")
    parser.add_argument("--sync-file", default="benchmark/results_sync.json")
    parser.add_argument("--output", default="benchmark/benchmark_results.png")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    datasets = {
        "async": load_results(args.async_file),
        "sync": load_results(args.sync_file),
    }
    if all(data is None for data in datasets.values()):
        raise SystemExit(
            "no benchmark results found; run the benchmark first "
            "(benchmark/run.py --mode async --output benchmark/results_async.json "
            "and --mode sync --output benchmark/results_sync.json)"
        )
    plot_results(datasets, args.output)


if __name__ == "__main__":
    main()
