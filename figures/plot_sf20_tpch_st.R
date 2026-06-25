#!/usr/bin/env Rscript

suppressPackageStartupMessages(library(ggplot2))

args <- commandArgs(trailingOnly = TRUE)
cmd_args <- commandArgs(FALSE)
file_arg <- grep("^--file=", cmd_args, value = TRUE)
script_dir <- if (length(file_arg) > 0) {
  dirname(normalizePath(sub("^--file=", "", file_arg[[1]])))
} else {
  getwd()
}

default_log <- file.path(
  script_dir,
  "20260625_135533_lnpe8wed_tpch_st_base_q1-8_openai-unsloth-MiniMax-M3_ssd_bstorage.log"
)
log_path <- if (length(args) >= 1) args[[1]] else default_log
out_prefix <- if (length(args) >= 2) {
  args[[2]]
} else {
  file.path(script_dir, "sf20_tpch_st_speedup")
}

strip_ansi <- function(x) gsub("\\033\\[[0-9;]*m", "", x)

lines <- strip_ansi(readLines(log_path, warn = FALSE))
lines <- lines[grepl("tools.validate.run_and_check_queries", lines, fixed = TRUE)]
pattern <- paste0(
  "Q([0-9]+) \\(BFFExecSettings\\(scale_factor=20,.*\\)\\): ",
  "([0-9.]+)ms \\(Bespoke\\), ([0-9.]+)ms \\(DuckDB\\)"
)
matches <- regmatches(lines, regexec(pattern, lines))
matches <- matches[lengths(matches) > 0]

if (length(matches) == 0) {
  stop("No SF20 query runtime lines found in: ", log_path)
}

raw <- do.call(rbind, lapply(matches, function(x) {
  data.frame(
    query = as.integer(x[[2]]),
    bespoke_ms = as.numeric(x[[3]]),
    duckdb_ms = as.numeric(x[[4]])
  )
}))

agg <- aggregate(cbind(bespoke_ms, duckdb_ms) ~ query, raw, median)
counts <- aggregate(bespoke_ms ~ query, raw, length)
names(counts)[2] <- "repetitions"
agg <- merge(agg, counts, by = "query")
agg$speedup <- agg$duckdb_ms / agg$bespoke_ms
agg <- agg[agg$query != 3, ]
agg <- agg[order(agg$query), ]
query_levels <- paste0("Q", c(1, 2, 4:8))
agg$query_label <- factor(paste0("Q", agg$query), levels = query_levels)

long <- rbind(
  data.frame(query_label = agg$query_label, system = "DuckDB", speedup = 1),
  data.frame(query_label = agg$query_label, system = "Bespoke", speedup = agg$speedup)
)
long$system <- factor(long$system, levels = c("Bespoke", "DuckDB"))
long$label <- ifelse(long$system == "Bespoke", sprintf("%.2fx", long$speedup), "")

expected_queries <- c(1, 2, 4:8)
missing_queries <- setdiff(expected_queries, agg$query)
subtitle <- "SF20 median speedup; DuckDB baseline = 1x"
if (length(missing_queries) > 0) {
  subtitle <- paste0(
    subtitle,
    "; missing in log: ",
    paste0("Q", missing_queries, collapse = ", ")
  )
}

plot <- ggplot(long, aes(x = query_label, y = speedup, fill = system)) +
  geom_hline(yintercept = 1, color = "#444444", linewidth = 0.45, linetype = "dashed") +
  geom_col(position = position_dodge(width = 0.72), width = 0.62) +
  geom_text(
    aes(label = label),
    position = position_dodge(width = 0.72),
    vjust = -0.35,
    size = 3.3,
    color = "#222222"
  ) +
  scale_x_discrete(drop = FALSE) +
  scale_y_continuous(
    limits = c(0, max(long$speedup, na.rm = TRUE) * 1.18),
    expand = expansion(mult = c(0, 0.02))
  ) +
  scale_fill_manual(values = c("Bespoke" = "#2563eb", "DuckDB" = "#a3a3a3")) +
  labs(
    title = "TPC-H SF20 Speedup",
    subtitle = subtitle,
    x = NULL,
    y = "Speedup",
    fill = NULL
  ) +
  theme_minimal(base_size = 14) +
  theme(
    plot.title = element_text(face = "bold", size = 17),
    plot.subtitle = element_text(color = "#555555"),
    legend.position = "top",
    axis.line = element_line(color = "#222222", linewidth = 0.45),
    axis.ticks = element_line(color = "#222222", linewidth = 0.35),
    axis.ticks.length = unit(3, "pt"),
    panel.grid.major.x = element_blank(),
    panel.grid.minor = element_blank(),
    axis.title = element_text(face = "bold")
  )

speedup_csv <- data.frame(
  query = agg$query,
  duckdb_speedup = 1,
  bespoke_speedup = agg$speedup,
  repetitions = agg$repetitions
)
write.csv(speedup_csv, paste0(out_prefix, ".csv"), row.names = FALSE)
ggsave(paste0(out_prefix, ".png"), plot, width = 8, height = 4.8, dpi = 180)
ggsave(paste0(out_prefix, ".pdf"), plot, width = 8, height = 4.8)

message("Wrote: ", paste0(out_prefix, ".csv"))
message("Wrote: ", paste0(out_prefix, ".png"))
message("Wrote: ", paste0(out_prefix, ".pdf"))
if (length(missing_queries) > 0) {
  message("Missing SF20 queries in log: ", paste0("Q", missing_queries, collapse = ", "))
}
