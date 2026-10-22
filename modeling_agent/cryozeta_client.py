from __future__ import annotations

import os
import re
from contextlib import ExitStack
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse

import requests

DEFAULT_BASE_URL = "https://em.kiharalab.org"
CLIENT_NAME = "CryoEM-Modeling-Agent"
CLIENT_VERSION = "0.1.0"
JOB_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]+$")


class CryoZetaError(RuntimeError):
    """A safe, user-facing error returned by the CryoZeta service client."""


class CryoZetaClient:
    def __init__(
        self,
        base_url: str = DEFAULT_BASE_URL,
        *,
        session: requests.Session | None = None,
        timeout: tuple[float, float] = (15.0, 300.0),
    ) -> None:
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise ValueError("CryoZeta base URL must be an absolute HTTPS URL.")
        self.base_url = base_url.rstrip("/")
        self.session = session or requests.Session()
        self.timeout = timeout
        self.csrf_token = ""

    @classmethod
    def from_environment(cls) -> CryoZetaClient:
        return cls(os.environ.get("CRYOZETA_BASE_URL", DEFAULT_BASE_URL))

    def login(self, username: str, password: str) -> dict[str, Any]:
        if not username.strip() or not password:
            raise CryoZetaError("CryoZeta username and password are required.")

        self._ensure_csrf_token()
        response = self.session.post(
            self._url("/users/api/login"),
            json={
                "username": username.strip(),
                "password": password,
                "remember_me": False,
            },
            headers=self._csrf_headers(),
            timeout=self.timeout,
        )
        payload = self._payload(response, "login")
        if not response.ok or not payload.get("ok"):
            raise CryoZetaError(
                self._message(
                    payload, f"CryoZeta login failed ({response.status_code})."
                )
            )
        return self.current_user()

    def login_from_environment(self) -> dict[str, Any]:
        username = os.environ.get("CRYOZETA_USERNAME", "")
        password = os.environ.get("CRYOZETA_PASSWORD", "")
        if not username.strip() or not password:
            raise CryoZetaError(
                "Set CRYOZETA_USERNAME and CRYOZETA_PASSWORD before using CryoZeta."
            )
        return self.login(username, password)

    def current_user(self) -> dict[str, Any]:
        response = self.session.get(
            self._url("/users/api/me"),
            timeout=self.timeout,
        )
        payload = self._payload(response, "session check")
        if not response.ok or not payload.get("ok") or not payload.get("authenticated"):
            raise CryoZetaError(
                self._message(payload, "CryoZeta session is not authenticated.")
            )
        return {
            "authenticated": True,
            "username": payload.get("username") or payload.get("email") or "",
        }

    def submit_job(
        self,
        *,
        map_path: Path,
        contour_level: float,
        resolution: float,
        protein_fasta: Path | None = None,
        dna_fasta: Path | None = None,
        rna_fasta: Path | None = None,
        job_note: str = "",
    ) -> dict[str, Any]:
        map_path = self._input_file(map_path, "Cryo-EM map")
        fasta_inputs = {
            "fasta(protein)": self._optional_input_file(protein_fasta, "protein FASTA"),
            "fasta(DNA)": self._optional_input_file(dna_fasta, "DNA FASTA"),
            "fasta(RNA)": self._optional_input_file(rna_fasta, "RNA FASTA"),
        }
        if not any(fasta_inputs.values()):
            raise CryoZetaError("Provide at least one protein, DNA, or RNA FASTA file.")
        if resolution <= 0:
            raise CryoZetaError("Map resolution must be greater than zero.")

        self._ensure_csrf_token()
        with ExitStack() as stack:
            files: dict[str, tuple[str, Any]] = {
                "cryo_em_map": (map_path.name, stack.enter_context(map_path.open("rb")))
            }
            for field_name, fasta_path in fasta_inputs.items():
                if fasta_path is not None:
                    files[field_name] = (
                        fasta_path.name,
                        stack.enter_context(fasta_path.open("rb")),
                    )

            response = self.session.post(
                self._url("/job/api/create"),
                data={
                    "internalalgoname": "CryoZeta",
                    "contour_level": f"{float(contour_level):.4f}",
                    "resolution": f"{float(resolution):.3f}",
                    "job_note": job_note.strip(),
                    "submission_source": "modeling_agent",
                    "client_name": CLIENT_NAME,
                    "client_version": CLIENT_VERSION,
                },
                files=files,
                headers=self._csrf_headers(),
                timeout=self.timeout,
            )

        payload = self._payload(response, "job submission")
        if not response.ok or not payload.get("ok"):
            message = self._message(
                payload, f"CryoZeta submission failed ({response.status_code})."
            )
            if payload.get("errors"):
                message = f"{message} {payload['errors']}"
            raise CryoZetaError(message)

        job_id = str(payload.get("job_id") or "")
        if not job_id:
            raise CryoZetaError(
                "CryoZeta accepted the submission but returned no job ID."
            )
        return {
            "ok": True,
            "job_id": job_id,
            "job_url": self._url(f"/job/viewjob/{job_id}"),
            "message": self._message(payload, "CryoZeta job submitted successfully."),
        }

    def job_status(self, job_id: str) -> dict[str, Any]:
        job_id = self._job_id(job_id)
        response = self.session.get(
            self._url(f"/job/api/status/{job_id}/"),
            timeout=self.timeout,
        )
        payload = self._payload(response, "job status")
        if not response.ok or not payload.get("ok"):
            raise CryoZetaError(
                self._message(
                    payload, f"CryoZeta status request failed ({response.status_code})."
                )
            )
        result_url = self._absolute_url(payload.get("result_url"))
        return {
            "ok": True,
            "job_id": job_id,
            "status": payload.get("status", "Unknown"),
            "message": payload.get("message", ""),
            "result_ready": bool(payload.get("result_ready")),
            "result_url": result_url,
        }

    def result_metadata(self, job_id: str) -> dict[str, Any]:
        job_id = self._job_id(job_id)
        response = self.session.get(
            self._url(f"/job/api/result/{job_id}/"),
            timeout=self.timeout,
        )
        payload = self._payload(response, "result metadata")
        if not response.ok or not payload.get("ok"):
            raise CryoZetaError(
                self._message(
                    payload, f"CryoZeta result request failed ({response.status_code})."
                )
            )
        return {
            "ok": True,
            "job_id": job_id,
            "result_url": self._absolute_url(payload.get("result_url")),
            "message": payload.get("message", ""),
        }

    def download_result(self, job_id: str, output_path: Path) -> dict[str, Any]:
        metadata = self.result_metadata(job_id)
        result_url = metadata.get("result_url") or ""
        if not result_url:
            raise CryoZetaError("CryoZeta result is not ready for download.")
        if not self._same_origin(result_url):
            raise CryoZetaError("CryoZeta returned a result URL on an unexpected host.")

        response = self.session.get(result_url, timeout=self.timeout)
        if not response.ok:
            raise CryoZetaError(
                f"CryoZeta result download failed ({response.status_code})."
            )
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(response.content)
        return {
            "ok": True,
            "job_id": self._job_id(job_id),
            "local_path": str(output_path.resolve()),
            "bytes": len(response.content),
        }

    def _ensure_csrf_token(self) -> str:
        token = self.session.cookies.get("csrftoken")
        if not token:
            response = self.session.get(
                self._url("/users/loginpage"),
                timeout=self.timeout,
            )
            if not response.ok:
                raise CryoZetaError(
                    f"CryoZeta login page request failed ({response.status_code})."
                )
            token = self.session.cookies.get("csrftoken")
        if not token:
            raise CryoZetaError("CryoZeta server did not provide a CSRF token.")
        self.csrf_token = token
        return token

    def _csrf_headers(self) -> dict[str, str]:
        return {
            "Referer": self.base_url + "/",
            "X-CSRFToken": self.csrf_token,
        }

    def _url(self, path: str) -> str:
        return urljoin(self.base_url + "/", path.lstrip("/"))

    def _absolute_url(self, value: Any) -> str:
        if not value:
            return ""
        return urljoin(self.base_url + "/", str(value))

    def _same_origin(self, value: str) -> bool:
        expected = urlparse(self.base_url)
        actual = urlparse(value)
        return actual.scheme == expected.scheme and actual.netloc == expected.netloc

    @staticmethod
    def _payload(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise CryoZetaError(
                f"CryoZeta returned a non-JSON response during {operation}."
            ) from exc
        if not isinstance(payload, dict):
            raise CryoZetaError(
                f"CryoZeta returned an invalid JSON response during {operation}."
            )
        return payload

    @staticmethod
    def _message(payload: dict[str, Any], fallback: str) -> str:
        message = payload.get("message")
        return str(message).strip() if message else fallback

    @staticmethod
    def _input_file(path: Path, label: str) -> Path:
        resolved = Path(path).resolve()
        if not resolved.is_file():
            raise CryoZetaError(f"{label} file does not exist: {resolved}")
        return resolved

    @classmethod
    def _optional_input_file(cls, path: Path | None, label: str) -> Path | None:
        return cls._input_file(path, label) if path is not None else None

    @staticmethod
    def _job_id(job_id: str) -> str:
        normalized = str(job_id).strip()
        if not normalized or not JOB_ID_PATTERN.fullmatch(normalized):
            raise CryoZetaError("CryoZeta job ID contains invalid characters.")
        return normalized
