#!/usr/bin/env python3
"""Измерение скорости загрузки одного URL 10 запросами подряд."""

from __future__ import annotations

import argparse
import http.client
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional, Sequence
from urllib.parse import urlparse

__version__ = "1.0.0"

REQUEST_COUNT = 10
CHUNK_SIZE = 64 * 1024
DEFAULT_TIMEOUT = 30.0
USER_AGENT = "download-speed-test/1.0"

BYTES_PER_MB = 1_000_000
BITS_PER_BYTE = 8

EXIT_OK = 0
EXIT_MEASUREMENT_FAILED = 1
EXIT_USAGE = 2
EXIT_INTERRUPTED = 130


class DownloadError(Exception):
    pass


class MeasurementError(Exception):
    def __init__(self, index: int, total: int, completed: int, reason: str) -> None:
        super().__init__(f"ошибка запроса {index}/{total}: {reason}")
        self.index = index
        self.total = total
        self.completed = completed


def megabits_per_second(size_bytes: int, seconds: float) -> float:
    if seconds <= 0:
        return 0.0
    return size_bytes * BITS_PER_BYTE / seconds / 1_000_000


@dataclass(frozen=True)
class Sample:
    size_bytes: int
    seconds: float

    @property
    def mbps(self) -> float:
        return megabits_per_second(self.size_bytes, self.seconds)


@dataclass
class Result:
    url: str
    samples: List[Sample] = field(default_factory=list)

    @property
    def total_bytes(self) -> int:
        return sum(s.size_bytes for s in self.samples)

    @property
    def total_seconds(self) -> float:
        return sum(s.seconds for s in self.samples)

    @property
    def average_seconds(self) -> float:
        if not self.samples:
            return 0.0
        return self.total_seconds / len(self.samples)

    @property
    def mbps(self) -> float:
        return megabits_per_second(self.total_bytes, self.total_seconds)

    @property
    def megabytes_per_second(self) -> float:
        if self.total_seconds <= 0:
            return 0.0
        return self.total_bytes / BYTES_PER_MB / self.total_seconds


def _declared_length(response: Any) -> Optional[int]:
    raw = response.headers.get("Content-Length")
    if raw is None:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value >= 0 else None


def _describe_reason(reason: object, timeout: float) -> str:
    if isinstance(reason, (socket.timeout, TimeoutError)):
        return f"время ожидания истекло через {timeout:g} с"
    return f"ошибка соединения: {reason}"


def download_once(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
    timer: Callable[[], float] = time.perf_counter,
) -> Sample:
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
    )
    started = timer()
    received = 0
    expected: Optional[int] = None
    elapsed = 0.0
    try:
        with opener(request, timeout=timeout) as response:
            expected = _declared_length(response)
            while True:
                chunk = response.read(CHUNK_SIZE)
                if not chunk:
                    break
                received += len(chunk)
            elapsed = timer() - started
    except urllib.error.HTTPError as exc:
        exc.close()
        raise DownloadError(f"ошибка HTTP {exc.code}: {exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise DownloadError(_describe_reason(exc.reason, timeout)) from exc
    except (socket.timeout, TimeoutError):
        raise DownloadError(f"время ожидания истекло через {timeout:g} с без получения данных") from None
    except http.client.IncompleteRead as exc:
        raise DownloadError(
            f"соединение закрыто преждевременно: в последнем фрагменте получено "
            f"{len(exc.partial)} байт"
        ) from exc
    except (http.client.HTTPException, OSError) as exc:
        raise DownloadError(f"ошибка соединения: {exc}") from exc

    if expected is not None and received != expected:
        raise DownloadError(f"неполное тело: получено {received} из {expected} байт")
    return Sample(size_bytes=received, seconds=elapsed)


def run_measurement(
    url: str,
    timeout: float = DEFAULT_TIMEOUT,
    count: int = REQUEST_COUNT,
    *,
    download: Optional[Callable[[str, float], Sample]] = None,
    on_sample: Optional[Callable[[int, int, Sample], None]] = None,
) -> Result:
    download = download or download_once
    result = Result(url=url)
    for index in range(1, count + 1):
        try:
            sample = download(url, timeout)
        except DownloadError as exc:
            raise MeasurementError(index, count, len(result.samples), str(exc)) from exc
        result.samples.append(sample)
        if on_sample is not None:
            on_sample(index, count, sample)
    return result


def format_megabytes(size_bytes: int) -> str:
    return f"{size_bytes / BYTES_PER_MB:.2f} MB"


def format_sample(index: int, total: int, sample: Sample) -> str:
    width = len(str(total))
    return (
        f"[{index:>{width}}/{total}] {format_megabytes(sample.size_bytes)} "
        f"за {sample.seconds:.3f} с ({sample.mbps:.2f} Мбит/с)"
    )


def format_summary(result: Result) -> str:
    rows = [
        ("URL", result.url),
        ("Запросов", str(len(result.samples))),
        ("Среднее время запроса", f"{result.average_seconds:.3f} с"),
        ("Всего загружено", f"{format_megabytes(result.total_bytes)} ({result.total_bytes} байт)"),
        ("Скорость загрузки", f"{result.mbps:.2f} Мбит/с ({result.megabytes_per_second:.2f} МБ/с)"),
    ]
    width = max(len(label) for label, _ in rows)
    lines = ["", "Результат"]
    lines += [f"  {label:<{width}} : {value}" for label, value in rows]
    return "\n".join(lines)


def http_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise argparse.ArgumentTypeError(
            f"{value!r} — некорректный URL. Укажите адрес http:// или "
            "https:// (например, https://host/file.jpg)"
        )
    return value


def positive_float(value: str) -> float:
    try:
        number = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} — это не число") from None
    if not number > 0 or number == float("inf"):
        raise argparse.ArgumentTypeError(
            "тайм-аут должен быть положительным конечным числом секунд"
        )
    return number


class RussianArgumentParser(argparse.ArgumentParser):
    def format_usage(self) -> str:
        return super().format_usage().replace("usage:", "Использование:")

    def format_help(self) -> str:
        help_text = super().format_help()
        return (
            help_text.replace("usage:", "Использование:")
            .replace("positional arguments:", "Позиционные аргументы:")
            .replace("options:", "Параметры:")
            .replace("optional arguments:", "Параметры:")
        )

    def error(self, message: str) -> None:
        if message.startswith("the following arguments are required:"):
            message = "не указаны обязательные аргументы:" + message.split(":", 1)[1]
        elif message.startswith("unrecognized arguments:"):
            message = "неизвестные аргументы:" + message.split(":", 1)[1]
        elif message.startswith("argument "):
            message = "аргумент " + message[len("argument ") :]
        print(f"{self.prog}: ошибка: {message}", file=sys.stderr)
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    parser = RussianArgumentParser(
        prog="main.py",
        add_help=False,
        description=(
            f"Загружает URL {REQUEST_COUNT} раз подряд и выводит среднее время "
            "запроса, общий объём данных и скорость загрузки."
        ),
    )
    parser.add_argument("-h", "--help", action="help", help="показать это сообщение и выйти")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="показать версию программы и выйти",
    )
    parser.add_argument(
        "url", type=http_url, metavar="URL", help="адрес большого файла по протоколу HTTP или HTTPS"
    )
    parser.add_argument(
        "--timeout",
        type=positive_float,
        default=DEFAULT_TIMEOUT,
        metavar="СЕКУНДЫ",
        help=(
            "тайм-аут подключения и максимальное ожидание данных при остановке передачи "
            f"(по умолчанию: {DEFAULT_TIMEOUT:g}); не ограничивает общее время загрузки"
        ),
    )
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            reconfigure(encoding="utf-8", errors="backslashreplace")

    args = parse_args(argv)
    print(f"Загрузка {args.url}: {REQUEST_COUNT} запросов подряд...")
    try:
        result = run_measurement(
            args.url,
            args.timeout,
            on_sample=lambda i, n, s: print(format_sample(i, n, s), flush=True),
        )
    except MeasurementError as exc:
        print(f"ошибка: {exc}", file=sys.stderr)
        print(
            f"Измерение не завершено: выполнено запросов — {exc.completed} из {exc.total}; "
            "итог не сформирован.",
            file=sys.stderr,
        )
        return EXIT_MEASUREMENT_FAILED
    except KeyboardInterrupt:
        print("\nВыполнение прервано.", file=sys.stderr)
        return EXIT_INTERRUPTED

    print(format_summary(result))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
