from typing import List


def gen_sf(
    benchmark: str,
    benchmark_sf: int | float | None = None,
    multi_threaded_mode: bool = False,
) -> tuple[List[float], int | float]:
    if benchmark == "tpch":
        verify_sf_list: List[float] = [1, 2]

        if benchmark_sf is None:
            if multi_threaded_mode:
                max_scale_factor = 50
            else:
                max_scale_factor = 20
        else:
            max_scale_factor = benchmark_sf

    elif benchmark == "ceb":
        verify_sf_list: List[float] = [
            0.25,
            0.5,
        ]  # just two different scales to make sure that the code works well with different data.
        if benchmark_sf is None:
            if multi_threaded_mode:
                max_scale_factor = 5
            else:
                max_scale_factor = 2
        else:
            max_scale_factor = benchmark_sf
    else:
        raise ValueError(f"Unknown benchmark {benchmark}")

    return verify_sf_list, max_scale_factor
