"""Plot browser measurements without substituting synthetic performance data."""

import csv
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "artifacts" / "chat-perf-2026-10-28"
with (OUT / "raw" / "scroll.csv").open() as stream:
    rows = list(csv.DictReader(stream))

for metric, label in [
    ("messageElements", "Message area elements"),
    ("mounted", "Mounted message bubbles"),
    ("heapMiB", "Approximate JS heap (MiB)"),
]:
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), layout="constrained")
    for axis, size in zip(axes.flat, ["50", "100", "500", "1000"]):
        for port, color in [("5173", "#b45309"), ("5175", "#0369a1")]:
            subset = [
                row for row in rows
                if row["size"] == size and row["port"] == port
            ]
            axis.plot(
                [int(row["index"]) for row in subset],
                [float(row[metric]) for row in subset],
                label=port,
                color=color,
                linewidth=1.5,
            )
        axis.set_title(f"{size} group")
        axis.set_xlabel("Serial checkpoint (two round trips)")
        axis.set_ylabel(label)
        axis.grid(alpha=0.2)
        axis.legend()
    fig.savefig(OUT / f"{metric}.png", dpi=160)
    plt.close(fig)

fig, axes = plt.subplots(1, 3, figsize=(15, 4), layout="constrained")
for axis, metric, label in zip(
    axes,
    ["messageElements", "mounted", "heapMiB"],
    ["Message elements", "Mounted bubbles", "JS heap (MiB)"],
):
    for size in ["50", "100", "500", "1000"]:
        subset = [
            row for row in rows
            if row["port"] == "5175" and row["size"] == size
        ]
        axis.plot(
            [int(row["requestedPages"]) for row in subset],
            [float(row[metric]) for row in subset],
            label=size,
            linewidth=1.2,
        )
    axis.set_xlabel("Completed history JSON responses (5175)")
    axis.set_ylabel(label)
    axis.grid(alpha=0.2)
    axis.legend(title="Group")
fig.savefig(OUT / "paging-dom-memory.png", dpi=160)
plt.close(fig)
