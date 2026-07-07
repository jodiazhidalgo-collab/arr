from pathlib import Path
from typing import Any, Dict, List, Optional

import requests


class QbitLikeClient:
    def __init__(self, base_url: str, username: str, password: str, label: str):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.label = label
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": "arr-orchestrator/0.1"})

    def login(self) -> None:
        response = self.session.post(
            f"{self.base_url}/api/v2/auth/login",
            data={"username": self.username, "password": self.password},
            timeout=15,
        )
        response.raise_for_status()
        if response.text.strip() not in ("", "Ok.", "Ok"):
            raise RuntimeError(f"{self.label}: respuesta de login inesperada: {response.text!r}")

    def _request(self, method: str, path: str, **kwargs: Any) -> requests.Response:
        response = self.session.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
        if response.status_code in (401, 403):
            self.login()
            response = self.session.request(method, f"{self.base_url}{path}", timeout=30, **kwargs)
        response.raise_for_status()
        return response

    def version(self) -> str:
        self.login()
        return self._request("GET", "/api/v2/app/version").text.strip()

    def torrents(self, torrent_filter: str = "all") -> List[Dict[str, Any]]:
        self.login()
        response = self._request(
            "GET", "/api/v2/torrents/info", params={"filter": torrent_filter}
        )
        payload = response.json()
        return payload if isinstance(payload, list) else []

    def torrent(self, infohash: str) -> Optional[Dict[str, Any]]:
        self.login()
        response = self._request(
            "GET", "/api/v2/torrents/info", params={"hashes": infohash}
        )
        payload = response.json()
        return payload[0] if isinstance(payload, list) and payload else None

    def add_torrent(self, path: Path, fields: Dict[str, str]) -> None:
        self.login()
        with path.open("rb") as handle:
            response = self._request(
                "POST",
                "/api/v2/torrents/add",
                data=fields,
                files={"torrents": (path.name, handle, "application/x-bittorrent")},
            )
        if response.text.strip() not in ("", "Ok.", "Ok"):
            raise RuntimeError(f"{self.label}: alta rechazada: {response.text!r}")

    def delete(self, infohash: str, delete_files: bool = False) -> None:
        self.login()
        self._request(
            "POST",
            "/api/v2/torrents/delete",
            data={"hashes": infohash, "deleteFiles": str(delete_files).lower()},
        )

    def set_category(self, infohash: str, category: str) -> None:
        self.login()
        self._request(
            "POST",
            "/api/v2/torrents/setCategory",
            data={"hashes": infohash, "category": category},
        )

    def set_location(self, infohash: str, location: str) -> None:
        self.login()
        self._request(
            "POST",
            "/api/v2/torrents/setLocation",
            data={"hashes": infohash, "location": location},
        )
