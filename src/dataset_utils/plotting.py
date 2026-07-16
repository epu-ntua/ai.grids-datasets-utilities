from __future__ import annotations

import matplotlib.pyplot as plt


def bar_counts(counts: dict[str, int], title: str, top_n: int = 20) -> None:
    """
    Render a basic descending bar chart for count dictionaries.

    Intended for notebook diagnostics and quick sanity visuals.
    """
    items = sorted(counts.items(), key=lambda x: x[1], reverse=True)[:top_n]
    labels = [k for k, _ in items]
    vals = [v for _, v in items]

    plt.figure()
    plt.bar(range(len(vals)), vals)
    plt.xticks(range(len(vals)), labels, rotation=75, ha="right")
    plt.title(title)
    plt.tight_layout()
    plt.show()
