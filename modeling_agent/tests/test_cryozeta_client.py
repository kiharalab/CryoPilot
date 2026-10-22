from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from cryozeta_client import CryoZetaClient, CryoZetaError
from tools import _wait_for_cryozeta_success


class FakeResponse:
    def __init__(self, payload=None, *, ok=True, status_code=200, content=b""):
        self._payload = payload
        self.ok = ok
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self):
        self.cookies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url.endswith("/users/loginpage"):
            self.cookies["csrftoken"] = "test-csrf"
            return FakeResponse({})
        if url.endswith("/users/api/me"):
            return FakeResponse(
                {"ok": True, "authenticated": True, "username": "test-user"}
            )
        if "/job/api/status/" in url:
            return FakeResponse(
                {
                    "ok": True,
                    "status": "Success",
                    "message": "Finished",
                    "result_ready": True,
                    "result_url": "/media/result.cif",
                }
            )
        if "/job/api/result/" in url:
            return FakeResponse({"ok": True, "result_url": "/media/result.cif"})
        if url.endswith("/media/result.cif"):
            return FakeResponse(content=b"data_test\n#\n")
        raise AssertionError(f"Unexpected GET {url}")

    def post(self, url, **kwargs):
        files = kwargs.get("files") or {}
        captured_files = {
            name: (value[0], value[1].read()) for name, value in files.items()
        }
        recorded = dict(kwargs)
        recorded["files"] = captured_files
        self.calls.append(("POST", url, recorded))
        if url.endswith("/users/api/login"):
            return FakeResponse({"ok": True, "username": "test-user"})
        if url.endswith("/job/api/create"):
            return FakeResponse({"ok": True, "job_id": "job-123", "message": "Queued"})
        raise AssertionError(f"Unexpected POST {url}")


class CryoZetaClientTests(unittest.TestCase):
    def setUp(self):
        self.session = FakeSession()
        self.client = CryoZetaClient(session=self.session)

    def test_login_uses_csrf_cookie_and_confirms_session(self):
        user = self.client.login("test-user", "test-password")

        self.assertEqual(user, {"authenticated": True, "username": "test-user"})
        login_call = next(
            call
            for call in self.session.calls
            if call[0] == "POST" and call[1].endswith("/users/api/login")
        )
        self.assertEqual(login_call[2]["headers"]["X-CSRFToken"], "test-csrf")
        self.assertFalse(login_call[2]["json"]["remember_me"])

    def test_submit_uses_plugin_compatible_multipart_fields(self):
        self.client.login("test-user", "test-password")
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            density_map = root / "map.mrc"
            fasta = root / "protein.fasta"
            density_map.write_bytes(b"MAP")
            fasta.write_text(">chain_A\nACDE\n", encoding="utf-8")

            result = self.client.submit_job(
                map_path=density_map,
                protein_fasta=fasta,
                contour_level=0.56,
                resolution=2.2,
                job_note="integration test",
            )

        self.assertEqual(result["job_id"], "job-123")
        submit_call = next(
            call
            for call in self.session.calls
            if call[0] == "POST" and call[1].endswith("/job/api/create")
        )
        self.assertEqual(
            set(submit_call[2]["files"]), {"cryo_em_map", "fasta(protein)"}
        )
        self.assertEqual(submit_call[2]["data"]["internalalgoname"], "CryoZeta")
        self.assertEqual(submit_call[2]["data"]["contour_level"], "0.5600")
        self.assertEqual(submit_call[2]["data"]["resolution"], "2.200")
        self.assertEqual(submit_call[2]["headers"]["X-CSRFToken"], "test-csrf")

    def test_status_metadata_and_download(self):
        self.client.login("test-user", "test-password")
        status = self.client.job_status("job-123")
        metadata = self.client.result_metadata("job-123")

        with tempfile.TemporaryDirectory() as temporary_directory:
            output = Path(temporary_directory) / "result.cif"
            download = self.client.download_result("job-123", output)
            self.assertEqual(output.read_bytes(), b"data_test\n#\n")

        self.assertTrue(status["result_ready"])
        self.assertEqual(
            metadata["result_url"], "https://em.kiharalab.org/media/result.cif"
        )
        self.assertEqual(download["bytes"], 12)

    def test_rejects_invalid_job_id(self):
        with self.assertRaises(CryoZetaError):
            self.client.job_status("../../other-user-job")


class FakeWaitingClient:
    def __init__(self, statuses):
        self.statuses = iter(statuses)
        self.login_count = 0

    def login_from_environment(self):
        self.login_count += 1

    def job_status(self, job_id):
        status = dict(next(self.statuses))
        status.setdefault("job_id", job_id)
        return status

    def result_metadata(self, job_id):
        return {"ok": True, "job_id": job_id, "result_url": "/result.cif"}


class CryoZetaWaitToolTests(unittest.IsolatedAsyncioTestCase):
    async def test_waits_and_refreshes_login_until_success(self):
        client = FakeWaitingClient(
            [
                {"status": "running", "result_ready": False},
                {"status": "Success", "result_ready": True},
            ]
        )
        sleep_calls = []

        async def fake_sleep(seconds):
            sleep_calls.append(seconds)

        result = await _wait_for_cryozeta_success(
            client,
            "job-123",
            poll_interval_seconds=30,
            sleeper=fake_sleep,
        )

        self.assertEqual(sleep_calls, [30.0])
        self.assertEqual(client.login_count, 1)
        self.assertEqual(result["status"], "Success")
        self.assertEqual(result["poll_count"], 2)
        self.assertEqual(result["result"]["result_url"], "/result.cif")

    async def test_terminal_failure_stops_waiting(self):
        client = FakeWaitingClient(
            [{"status": "Failed", "message": "server failure", "result_ready": False}]
        )

        with self.assertRaisesRegex(CryoZetaError, "server failure"):
            await _wait_for_cryozeta_success(client, "job-123")


if __name__ == "__main__":
    unittest.main()
