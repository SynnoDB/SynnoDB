Parallel rendering idea for `metrics_live.py`

- Keep terminal frame composition sequential.
- Pre-render only the live plot images in parallel.
- Use `ProcessPoolExecutor`, not threads.
- Build the list of unique `history_index` values for the turns being rendered.
- Render plot images for those indices ahead of time.
- Cache them in a mapping like `{history_index: image}`.
- Reuse that cache during the normal sequential `render_frames(...)` pass.

Why this is the sensible scope:

- The terminal buffer is append-only and stateful across turns.
- The live plot for a given turn depends only on the history/config for that turn.
- Matplotlib is a better fit for multiprocessing than multithreading.

Implementation notes:

- Add an optional CLI arg like `--plot-workers N`.
- Keep the existing sequential path as the default.
- Avoid parallelizing the whole screen render loop unless there is a stronger reason later.
