"""Render saved retrieval measurements as PNG charts for the Markdown guide.

This optional documentation tool reads the existing sweep JSON; it never opens
Chroma or calls a model. Run with matplotlib supplied by uv, without adding a
plotting dependency to the application environment.
"""

import argparse
import json
from pathlib import Path
import textwrap

import matplotlib

# File rendering must also work on servers without an interactive display.
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.ticker import PercentFormatter, StrMethodFormatter

RESULT_COUNTS = (5, 8, 10, 20)
SERIES = (
    ("vector", "Vector", "#2563a6", "o"),
    ("hybrid-c20", "Híbrido · 20 candidatos", "#ca681c", "s"),
    ("hybrid-c40", "Híbrido · 40 candidatos", "#15816e", "^"),
    ("hybrid-c80", "Híbrido · 80 candidatos", "#8951a5", "D"),
)


def configuration(report: dict, series: str, k: int) -> dict:
    """Select an existing configuration; missing measurements fail explicitly."""
    key = f"vector-k{k}" if series == "vector" else f"hybrid-k{k}-{series.split('-')[1]}"
    return report["configurations"][key]


def line_chart(report: dict, metric: str, *, title: str, ylabel: str):
    """Plot all four measured series at actual k values without interpolation claims."""
    figure, axis = plt.subplots(figsize=(10, 5.4), layout="constrained")
    for series, label, color, marker in SERIES:
        values = [configuration(report, series, k)["summary"][metric] for k in RESULT_COUNTS]
        axis.plot(RESULT_COUNTS, values, label=label, color=color, marker=marker, linewidth=2)
    axis.set(title=title, xlabel="Fragmentos finales recuperados (k)", ylabel=ylabel,
             xticks=RESULT_COUNTS)
    axis.grid(alpha=0.2)
    axis.legend(loc="best", fontsize=9)
    return figure, axis


def save(figure, output: Path, name: str):
    """Export a sharp, opaque PNG that ordinary Markdown viewers can display."""
    figure.savefig(output / name, dpi=180, facecolor="white")
    plt.close(figure)


def plot_sweep(report: dict, output: Path):
    """Create five complementary views from the saved, unchanged judgments.

    Aggregate charts use all repetitions. The per-question chart uses round one;
    that distinction matters if later experiments show relevance variability.
    """
    output.mkdir(parents=True, exist_ok=True)
    plt.rcParams.update({"font.size": 11, "axes.spines.top": False, "axes.spines.right": False})
    figure, axis = line_chart(report, "mean_recall_at_k", title="Más fragmentos mejoran la cobertura",
                              ylabel="Recall medio de fuentes revisadas")
    axis.set_ylim(0, 1.04)
    axis.yaxis.set_major_formatter(PercentFormatter(1))
    save(figure, output, "recall-by-k.png")

    figure, axis = line_chart(report, "mean_context_characters", title="El contexto crece al recuperar más fuentes",
                              ylabel="Caracteres medios del JSON de fuentes")
    axis.set_ylim(bottom=0)
    axis.yaxis.set_major_formatter(StrMethodFormatter("{x:,.0f}"))
    save(figure, output, "context-by-k.png")

    figure, axis = plt.subplots(figsize=(10, 4.4), layout="constrained")
    differences = [[100 * (configuration(report, series, k)["summary"]["mean_recall_at_k"]
                           - configuration(report, "vector", k)["summary"]["mean_recall_at_k"])
                    for k in RESULT_COUNTS] for series, _, _, _ in SERIES[1:]]
    # A shared symmetric scale makes positive/negative changes comparable.
    image = axis.imshow(differences, cmap="RdBu", vmin=-10, vmax=10, aspect="auto")
    axis.set(xticks=range(4), xticklabels=RESULT_COUNTS, yticks=range(3),
             yticklabels=("20 candidatos", "40 candidatos", "80 candidatos"),
             xlabel="Fragmentos finales recuperados (k)",
             title="Híbrido frente a vector: diferencia de recall al mismo k")
    for row, values in enumerate(differences):
        for column, value in enumerate(values):
            axis.text(column, row, f"{value:+.2f} pp", ha="center", va="center", color="white" if abs(value) > 5 else "#17212d")
    figure.colorbar(image, ax=axis, label="Puntos porcentuales (positivo favorece al híbrido)")
    save(figure, output, "candidate-impact.png")

    figure, axes = plt.subplots(1, 2, figsize=(12, 4.8), layout="constrained", sharey=True)
    for axis, metric, title in zip(axes, ("mean_latency_ms", "p95_latency_ms"),
                                   ("Media observada", "p95 observado")):
        for series, label, color, marker in SERIES:
            values = [configuration(report, series, k)["summary"][metric] for k in RESULT_COUNTS]
            axis.plot(RESULT_COUNTS, values, label=label, color=color, marker=marker, linewidth=2)
        axis.set(title=title, xlabel="Fragmentos finales (k)", xticks=RESULT_COUNTS)
        axis.grid(alpha=0.2)
    # Set the shared scale after plotting both panels, so p95 peaks cannot clip.
    peak_latency = max(configuration(report, series, k)["summary"]["p95_latency_ms"]
                       for series, _, _, _ in SERIES for k in RESULT_COUNTS)
    axes[0].set_ylim(0, peak_latency * 1.1)
    axes[0].set_ylabel("Milisegundos, incluyendo embeddings y red")
    axes[0].legend(fontsize=8)
    figure.suptitle("Latencia de búsqueda · construcción inicial de BM25 excluida")
    save(figure, output, "latency-by-k.png")

    figure, axes = plt.subplots(1, 2, figsize=(13, 9.5), layout="constrained", sharey=True)
    questions = configuration(report, "vector", 5)["first_run"]["queries"]
    labels = [textwrap.fill(question["query"], width=40) for question in questions]
    for axis, series, title in zip(axes, ("vector", "hybrid-c20"), ("Vector", "Híbrido · 20 candidatos")):
        values = [[configuration(report, series, k)["first_run"]["queries"][index]["reviewed_source_recall_at_k"]
                   for k in RESULT_COUNTS] for index in range(len(questions))]
        axis.imshow(values, cmap="Blues", vmin=0, vmax=1, aspect="auto")
        axis.set(title=title, xticks=range(4), xticklabels=RESULT_COUNTS, xlabel="Fragmentos finales (k)",
                 yticks=range(len(labels)))
        for row, scores in enumerate(values):
            for column, value in enumerate(scores):
                axis.text(column, row, f"{value:.0%}", ha="center", va="center",
                          color="white" if value > 0.6 else "#17212d", fontsize=10)
    axes[0].set_yticklabels(labels, fontsize=9)
    figure.suptitle("Qué preguntas mejoran · recall por pregunta de la primera ronda")
    save(figure, output, "recall-by-question.png")


def main():
    """Select a saved sweep and destination; replace only the five named images."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("data/retrieval/parameter-sweep.json"))
    parser.add_argument("--output", type=Path, default=Path("docs/experiments/images/retrieval"))
    args = parser.parse_args()
    plot_sweep(json.loads(args.input.read_text(encoding="utf-8")), args.output)
    print(f"Saved five charts to {args.output}")


if __name__ == "__main__":
    main()
