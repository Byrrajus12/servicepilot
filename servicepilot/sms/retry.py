from datetime import timedelta


def retry_delay(base: float, attempts: int) -> timedelta:
    return timedelta(seconds=min(300, base * 2 ** (attempts - 1)))
