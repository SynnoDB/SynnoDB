import time


class RuntimeTracker:
    def __init__(self):
        self.skipped_time = 0  # time that was skipped due to cache hits. This is used to calculate the total time that would have been taken if there were no cache hits.
        self.wait_time = 0  # time spent waiting for user input - exclude this from total time calculation since it's not part of the agent's runtime

    def start(self):
        self.start_time = time.perf_counter()

    def add_skipped_time(self, time_to_add):
        self.skipped_time += time_to_add

    def add_wait_time(self, time_to_add):
        self.wait_time += time_to_add

    def retrieve_total_time(self) -> float:
        # returns in seconds
        return (
            time.perf_counter() - self.start_time + self.skipped_time - self.wait_time
        )
