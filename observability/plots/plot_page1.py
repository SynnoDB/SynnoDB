import matplotlib.pyplot as plt
import numpy as np


def plot_page1(
    benchmark_metrics_by_mode, selected_benchmarks, show_umbra_total_runtime
):
    # Combined grouped plot for multiple benchmarks in two rows: ST (top), MT (bottom).
    mode_order = [
        mode
        for mode in ["st", "mt"]
        if all(
            mode in benchmark_metrics_by_mode.get(b, {}) for b in selected_benchmarks
        )
    ]
    if not mode_order:
        raise ValueError(
            "No shared st/mt data available across selected benchmarks for combined runtime plot."
        )

    nrows = len(mode_order)
    fig_height = 3.1 if nrows == 1 else 5.8
    fig, axes = plt.subplots(nrows, 1, figsize=(6.8, fig_height), squeeze=False)

    for row_idx, mode in enumerate(mode_order):
        ax_main = axes[row_idx, 0]
        is_bottom_row = row_idx == nrows - 1

        benchmark_order = selected_benchmarks
        benchmark_labels = [
            benchmark_metrics_by_mode[b][mode]["label"] for b in benchmark_order
        ]
        duckdb_vals = np.array(
            [
                benchmark_metrics_by_mode[b][mode]["total_duckdb_time"]
                for b in benchmark_order
            ]
        )
        bespoke_vals = np.array(
            [
                benchmark_metrics_by_mode[b][mode]["total_bespoke_time"]
                for b in benchmark_order
            ]
        )

        include_umbra_global = show_umbra_total_runtime and all(
            benchmark_metrics_by_mode[b][mode]["total_umbra_time"] is not None
            for b in benchmark_order
        )
        if include_umbra_global:
            umbra_vals = np.array(
                [
                    benchmark_metrics_by_mode[b][mode]["total_umbra_time"]
                    for b in benchmark_order
                ]
            )
        else:
            umbra_vals = None
            if show_umbra_total_runtime:
                print(
                    f"[{mode.upper()}] Note: Umbra total runtime missing for at least one selected benchmark; plotting DuckDB vs Bespoke only."
                )

        if include_umbra_global:
            group_gap = 1.5
        else:
            group_gap = 2.0

        group_centers = np.arange(len(benchmark_order)) * group_gap

        if include_umbra_global:
            bar_width = 0.44
            duck_x = group_centers - bar_width
            umbra_x = group_centers
            bespoke_x = group_centers + bar_width
        else:
            bar_width = 0.62
            bar_offset = 0.43
            duck_x = group_centers - bar_offset
            bespoke_x = group_centers + bar_offset

        bars_duck = ax_main.bar(
            duck_x,
            duckdb_vals,
            width=bar_width,
            color="#E8E8E8",
            edgecolor="#333333",
            linewidth=2.0,
            alpha=1.0,
        )

        if include_umbra_global:
            bars_umbra = ax_main.bar(
                umbra_x,
                umbra_vals,
                width=bar_width,
                color="#7FB069",
                edgecolor="#4F6F41",
                linewidth=2.0,
                alpha=0.95,
            )
        else:
            bars_umbra = []

        bars_bespoke = ax_main.bar(
            bespoke_x,
            bespoke_vals,
            width=bar_width,
            color="#2E86AB",
            edgecolor="#1a4d6f",
            linewidth=2.0,
            alpha=0.95,
        )

        if include_umbra_global:
            y_max = max(np.max(duckdb_vals), np.max(umbra_vals), np.max(bespoke_vals))
        else:
            y_max = max(np.max(duckdb_vals), np.max(bespoke_vals))

        xtick_positions = []
        xtick_labels = []
        for i, benchmark_name in enumerate(benchmark_order):
            benchmark_label = benchmark_metrics_by_mode[benchmark_name][mode]["label"]
            xtick_positions.append(duck_x[i])
            xtick_labels.append("DuckDB")
            if include_umbra_global:
                xtick_positions.append(umbra_x[i])
                xtick_labels.append("Umbra")
            xtick_positions.append(bespoke_x[i])
            xtick_labels.append(f"Bespoke\n{benchmark_label}")

        ax_main.set_xticks(xtick_positions)
        # Only show the system-name tick labels on the bottom row.
        ax_main.set_xticklabels(
            xtick_labels if is_bottom_row else [""] * len(xtick_positions),
            fontsize=11.0,
        )

        for i, benchmark_label in enumerate(benchmark_labels):
            sf = benchmark_metrics_by_mode[benchmark_order[i]][mode]["target_sf"]
            # Only label the benchmark below the bottom row's x-axis.
            if is_bottom_row:
                ax_main.text(
                    group_centers[i],
                    -0.22,
                    f"{benchmark_label}",
                    transform=ax_main.get_xaxis_transform(),
                    ha="center",
                    va="top",
                    fontsize=12,
                    fontweight="bold",
                )
            # Scale factor, centered above each group in a light font.
            ax_main.text(
                group_centers[i],
                y_max * 1.22,
                f"{benchmark_label} (SF {sf})",
                ha="center",
                va="center",
                fontsize=11.5,
                # fontweight="light",
                color="#4D4D4D",
            )

        for i, benchmark_name in enumerate(benchmark_order):
            duck_h = duckdb_vals[i]
            bespoke_h = bespoke_vals[i]
            speedup_total = benchmark_metrics_by_mode[benchmark_name][mode][
                "overall_speedup_total"
            ]

            speedup_x = bespoke_x[i] - (bar_width / 2) + 0.05
            ax_main.annotate(
                "",
                xy=(speedup_x, bespoke_h + y_max * 0.01),
                xytext=(speedup_x, duck_h + y_max * 0.015),
                arrowprops=dict(arrowstyle="<->", color="#FF6B6B", lw=2.4),
            )
            ax_main.text(
                speedup_x + 0.05,
                (duck_h + bespoke_h) / 2,
                f"{speedup_total:.2f}x",
                ha="left",
                va="center",
                fontsize=13,
                fontweight="bold",
                color="#FF6B6B",
            )

        for bars in (bars_duck, bars_umbra, bars_bespoke):
            for bar in bars:
                h = bar.get_height()
                ax_main.text(
                    bar.get_x() + bar.get_width() / 2,
                    h + y_max * 0.01,
                    f"{h:.1f}s",
                    ha="center",
                    va="bottom",
                    fontsize=12,
                )

        # Mode label placed inside the plot instead of a title,
        # centered between the per-group SF annotations.
        mode_full = {"st": "Single-\nThreaded", "mt": "Multi-\nThreaded"}
        ax_main.text(
            float(np.mean(group_centers)),
            y_max * 1.22,
            mode_full.get(mode, mode.upper()),
            ha="center",
            va="center",
            fontsize=14,
            fontweight="bold",
            linespacing=0.95,
        )
        mode_ylabels = {"st": "Single-Threaded", "mt": "Multi-Threaded"}
        # In mathtext, wrap the hyphen in braces so it renders as a plain
        # hyphen (not a spaced minus sign).
        bold_word = mode_ylabels.get(mode, mode.upper()).replace("-", "{-}")
        ax_main.set_ylabel(
            r"$\mathbf{" + bold_word + r"}$" + "\n(total runtime in s)",
            fontsize=12,
        )
        ax_main.set_ylim(0, y_max * 1.35)
        ax_main.grid(axis="y", alpha=0.3, linestyle="-", linewidth=0.5)
        ax_main.set_axisbelow(True)

    plt.subplots_adjust(hspace=0.1, bottom=0.15, top=0.95)
    plt.savefig(
        "figures/journal_speedup_combined_tpch_ceb.pdf",
        dpi=300,
        bbox_inches="tight",
        facecolor="white",
    )

    plt.show()
    print("\n✓ Plot saved as 'journal_speedup_combined_tpch_ceb.pdf'")

    return fig
