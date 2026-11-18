"""Тесты для main.py. Внешняя сеть не используется.

В модульных тестах подменяются функции открытия соединения и таймеры.
Несколько интеграционных тестов запускают небольшой HTTP-сервер на 127.0.0.1,
чтобы проверить реальную работу urllib.
"""

import contextlib
import io
import socket
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

import main


# --------------------------------------------------------------------------- #
# Вспомогательные средства
# --------------------------------------------------------------------------- #
class FakeResponse:
    """Минимальная имитация объекта, возвращаемого urlopen()."""

    def __init__(self, body: bytes, headers=None):
        self._buffer = io.BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self.read_sizes = []

    def read(self, amt=-1):
        self.read_sizes.append(amt)
        return self._buffer.read(amt)

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def fake_timer(*values):
    iterator = iter(values)
    return lambda: next(iterator)


def sample(size=1_000_000, seconds=0.5):
    return main.Sample(size_bytes=size, seconds=seconds)


# --------------------------------------------------------------------------- #
# Проверка download_once
# --------------------------------------------------------------------------- #
class DownloadOnceTests(unittest.TestCase):
    def test_counts_every_byte_across_chunks(self):
        body = b"x" * 200_000  # несколько фрагментов
        response = FakeResponse(body)
        result = main.download_once(
            "http://h/f", opener=lambda req, timeout: response, timer=fake_timer(10.0, 12.0)
        )
        self.assertEqual(result.size_bytes, len(body))
        self.assertEqual(result.seconds, 2.0)

    def test_reads_in_large_chunks(self):
        # Защита от чтения по одному байту, которое исказило бы время измерений.
        response = FakeResponse(b"x" * 500_000)
        main.download_once("http://h/f", opener=lambda req, timeout: response)
        self.assertTrue(all(size == main.CHUNK_SIZE for size in response.read_sizes))
        self.assertLess(len(response.read_sizes), 20)

    def test_requests_uncompressed_body(self):
        seen = {}

        def opener(req, timeout):
            seen["accept_encoding"] = req.get_header("Accept-encoding")
            seen["timeout"] = timeout
            return FakeResponse(b"abc")

        main.download_once("http://h/f", timeout=7, opener=opener)
        self.assertEqual(seen["accept_encoding"], "identity")
        self.assertEqual(seen["timeout"], 7)

    def test_empty_body_is_a_valid_zero_byte_response(self):
        result = main.download_once("http://h/f", opener=lambda req, timeout: FakeResponse(b""))
        self.assertEqual(result.size_bytes, 0)
        self.assertEqual(result.mbps, 0.0)

    def test_missing_content_length_is_accepted(self):
        response = FakeResponse(b"x" * 10, headers={})
        result = main.download_once("http://h/f", opener=lambda req, timeout: response)
        self.assertEqual(result.size_bytes, 10)

    def test_short_body_is_an_error(self):
        response = FakeResponse(b"x" * 50, headers={"Content-Length": "100"})
        with self.assertRaisesRegex(main.DownloadError, "неполное тело"):
            main.download_once("http://h/f", opener=lambda req, timeout: response)

    def test_http_error_status(self):
        def opener(req, timeout):
            raise urllib.error.HTTPError(req.full_url, 404, "Not Found", {}, io.BytesIO(b""))

        with self.assertRaisesRegex(main.DownloadError, "HTTP 404"):
            main.download_once("http://h/f", opener=opener)

    def test_connection_error(self):
        def opener(req, timeout):
            raise urllib.error.URLError(ConnectionRefusedError("refused"))

        with self.assertRaisesRegex(main.DownloadError, "ошибка соединения"):
            main.download_once("http://h/f", opener=opener)

    def test_timeout_while_connecting(self):
        def opener(req, timeout):
            raise urllib.error.URLError(socket.timeout("timed out"))

        with self.assertRaisesRegex(main.DownloadError, "время ожидания истекло через 5 с"):
            main.download_once("http://h/f", timeout=5, opener=opener)

    def test_timeout_while_reading_body(self):
        class Stalling(FakeResponse):
            def read(self, amt=-1):
                raise socket.timeout("timed out")

        with self.assertRaisesRegex(main.DownloadError, "время ожидания истекло"):
            main.download_once("http://h/f", opener=lambda req, timeout: Stalling(b"abc"))


# --------------------------------------------------------------------------- #
# Расчёты
# --------------------------------------------------------------------------- #
class CalculationTests(unittest.TestCase):
    def test_summary_values(self):
        result = main.Result("http://h/f", [sample(1_000_000, 0.5) for _ in range(10)])
        self.assertEqual(result.total_bytes, 10_000_000)
        self.assertAlmostEqual(result.total_seconds, 5.0)
        self.assertAlmostEqual(result.average_seconds, 0.5)
        self.assertAlmostEqual(result.mbps, 16.0)  # 10 МБ * 8 / 5 с
        self.assertAlmostEqual(result.megabytes_per_second, 2.0)

    def test_speed_is_total_over_total_not_mean_of_speeds(self):
        # 1 МБ за 1 с и 9 МБ за 1 с: всего 10 МБ / 2 с = 40 Мбит/с.
        result = main.Result("u", [sample(1_000_000, 1.0), sample(9_000_000, 1.0)])
        self.assertAlmostEqual(result.mbps, 40.0)

    def test_zero_time_does_not_divide_by_zero(self):
        self.assertEqual(main.megabits_per_second(1000, 0), 0.0)
        self.assertEqual(main.Result("u", [sample(1000, 0.0)]).megabytes_per_second, 0.0)

    def test_summary_text_contains_units(self):
        result = main.Result("http://h/f", [sample(1_000_000, 0.5) for _ in range(10)])
        text = main.format_summary(result)
        self.assertIn("Среднее время запроса : 0.500 с", text)
        self.assertIn("10.00 MB (10000000 байт)", text)
        self.assertIn("16.00 Мбит/с (2.00 МБ/с)", text)


# --------------------------------------------------------------------------- #
# Проверка run_measurement
# --------------------------------------------------------------------------- #
class RunMeasurementTests(unittest.TestCase):
    def test_exactly_ten_sequential_requests(self):
        in_flight = 0
        order = []

        def download(url, timeout):
            nonlocal in_flight
            in_flight += 1
            self.assertEqual(in_flight, 1, "a request started before the previous one ended")
            order.append(len(order) + 1)
            in_flight -= 1
            return sample()

        result = main.run_measurement("http://h/f", download=download)
        self.assertEqual(order, list(range(1, 11)))
        self.assertEqual(len(result.samples), 10)

    def test_progress_callback_receives_each_sample(self):
        seen = []
        main.run_measurement(
            "u", download=lambda u, t: sample(), on_sample=lambda i, n, s: seen.append((i, n))
        )
        self.assertEqual(seen, [(i, 10) for i in range(1, 11)])

    def test_fail_fast_stops_at_first_error(self):
        calls = []

        def download(url, timeout):
            calls.append(1)
            if len(calls) == 4:
                raise main.DownloadError("HTTP 500 Server Error")
            return sample()

        with self.assertRaises(main.MeasurementError) as ctx:
            main.run_measurement("u", download=download)
        self.assertEqual(len(calls), 4)
        self.assertEqual((ctx.exception.index, ctx.exception.completed), (4, 3))
        self.assertIn("ошибка запроса 4/10: HTTP", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Интерфейс командной строки
# --------------------------------------------------------------------------- #
class CliTests(unittest.TestCase):
    def parse_expecting_usage_error(self, argv):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as ctx:
            main.parse_args(argv)
        self.assertEqual(ctx.exception.code, main.EXIT_USAGE)

    def test_missing_url(self):
        self.parse_expecting_usage_error([])

    def test_invalid_urls(self):
        for bad in (
            "not a url",
            "example.com/file",
            "ftp://host/file",
            "file:///etc/passwd",
            "http://",
        ):
            with self.subTest(url=bad):
                self.parse_expecting_usage_error([bad])

    def test_invalid_timeouts(self):
        for bad in ("0", "-3", "abc", "inf", "nan"):
            with self.subTest(timeout=bad):
                self.parse_expecting_usage_error(["http://h/f", "--timeout", bad])

    def test_invalid_argument_message_is_in_russian(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            main.parse_args(["http://h/f", "--timeout", "nan"])
        self.assertIn("ошибка:", err.getvalue())
        self.assertIn("аргумент --timeout", err.getvalue())
        self.assertIn("тайм-аут должен быть", err.getvalue())

    def test_version_flag(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main.parse_args(["--version"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn(main.__version__, out.getvalue())

    def test_defaults_and_overrides(self):
        args = main.parse_args(["https://h/f.jpg"])
        self.assertEqual((args.url, args.timeout), ("https://h/f.jpg", main.DEFAULT_TIMEOUT))
        self.assertEqual(main.parse_args(["http://h/f", "--timeout", "2.5"]).timeout, 2.5)

    def test_help_is_in_russian(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as ctx:
            main.parse_args(["--help"])
        self.assertEqual(ctx.exception.code, 0)
        self.assertIn("Позиционные аргументы:", out.getvalue())
        self.assertIn("Параметры:", out.getvalue())
        self.assertIn("Загружает URL", out.getvalue())
        self.assertIn("показать это сообщение и выйти", out.getvalue())
        self.assertNotIn("show this help message", out.getvalue())
        self.assertNotIn("optional arguments:", out.getvalue())

    @staticmethod
    def run_cli(argv, download):
        """Запускает main.main() с подменённой загрузкой и возвращает код и вывод."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(main, "download_once", download))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = main.main(argv)
        return code, out.getvalue(), err.getvalue()

    def test_success_prints_summary_and_exits_zero(self):
        code, out, err = self.run_cli(["http://h/f"], lambda url, timeout: sample())
        self.assertEqual(code, 0)
        self.assertIn("Скорость загрузки", out)
        self.assertEqual(err, "")

    def test_failure_prints_no_summary_and_exits_one(self):
        calls = []

        def failing(url, timeout):
            calls.append(1)
            if len(calls) == 3:
                raise main.DownloadError("время ожидания истекло через 30 с")
            return sample()

        code, out, err = self.run_cli(["http://h/f"], failing)
        self.assertEqual(code, main.EXIT_MEASUREMENT_FAILED)
        self.assertNotIn("Скорость загрузки", out)
        self.assertIn("ошибка запроса 3/10", err)
        self.assertIn("выполнено запросов — 2 из 10", err)


# --------------------------------------------------------------------------- #
# Интеграция: реальный urllib и локальный HTTP-сервер
# --------------------------------------------------------------------------- #
class _Handler(BaseHTTPRequestHandler):
    BODY = b"x" * 300_000

    def do_GET(self):
        if self.path == "/ok":
            self.send_response(200)
            self.send_header("Content-Length", str(len(self.BODY)))
            self.end_headers()
            self.wfile.write(self.BODY)
        elif self.path == "/empty":
            self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif self.path == "/truncated":
            self.send_response(200)
            self.send_header("Content-Length", "1000")
            self.end_headers()
            self.wfile.write(b"y" * 500)  # после этого соединение закрывается
        else:
            self.send_error(404)

    def log_message(self, *args):  # keep test output clean
        pass


class LocalServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        # Отключаем прокси из окружения, чтобы обращаться к локальному серверу напрямую.
        cls.opener = urllib.request.build_opener(urllib.request.ProxyHandler({})).open

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def fetch(self, path):
        return main.download_once(self.base + path, timeout=5, opener=self.opener)

    def test_downloads_full_body(self):
        result = self.fetch("/ok")
        self.assertEqual(result.size_bytes, 300_000)
        self.assertGreater(result.seconds, 0)

    def test_follows_redirect(self):
        self.assertEqual(self.fetch("/redirect").size_bytes, 300_000)

    def test_empty_body(self):
        self.assertEqual(self.fetch("/empty").size_bytes, 0)

    def test_404_is_failure(self):
        with self.assertRaisesRegex(main.DownloadError, "HTTP 404"):
            self.fetch("/missing")

    def test_connection_closed_before_full_body_is_failure(self):
        with self.assertRaisesRegex(main.DownloadError, "неполное тело: получено 500 из 1000"):
            self.fetch("/truncated")

    def test_refused_connection_is_failure(self):
        with socket.socket() as probe:  # занимаем свободный порт, затем закрываем сокет
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        with self.assertRaises(main.DownloadError):
            main.download_once(f"http://127.0.0.1:{port}/", timeout=2, opener=self.opener)

    def test_full_run_of_ten_requests(self):
        result = main.run_measurement(
            self.base + "/ok", 5, download=lambda u, t: main.download_once(u, t, opener=self.opener)
        )
        self.assertEqual(len(result.samples), 10)
        self.assertEqual(result.total_bytes, 3_000_000)


if __name__ == "__main__":
    unittest.main()
