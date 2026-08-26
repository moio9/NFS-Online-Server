"""End-to-end HTTP tests for the responsive Carbon DLC store."""

from __future__ import annotations

from http.client import HTTPConnection
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
import unittest
from urllib.parse import urlencode

from carbon.core.config import Endpoint
from carbon.dlc import CarbonDLCInventory
from carbon.web.dlc_store import CarbonDLCStoreServer
from common.accounts import SQLiteAccountDatabase


class CarbonDLCStoreTests(unittest.TestCase):
    def _fixture(self, root: Path) -> tuple[SQLiteAccountDatabase, CarbonDLCInventory]:
        database = SQLiteAccountDatabase(root / "accounts.sqlite3", root / "users")
        database.create_account("Driver", "secret", persona="Driver")

        catalog = root / "catalog.json"
        catalog.write_text(
            json.dumps(
                {
                    "version": 1,
                    "groups": {
                        "base": {
                            "label": "Base Car",
                            "category": "cars",
                            "tokens": ["BASE"],
                        },
                        "bonus": {
                            "label": "Bonus Vinyl",
                            "category": "vinyls",
                            "tokens": ["BONUS"],
                        },
                    },
                    "presets": {"default_dlc": ["base"]},
                }
            ),
            encoding="utf-8",
        )
        assignments = root / "assignments.json"
        assignments.write_text(
            json.dumps(
                {
                    "version": 1,
                    "default": ["default_dlc"],
                    "accounts": {},
                    "personas": {},
                }
            ),
            encoding="utf-8",
        )
        return database, CarbonDLCInventory.from_paths(catalog, assignments)

    @staticmethod
    def _post(
        connection: HTTPConnection,
        path: str,
        fields: list[tuple[str, str]],
        *,
        cookie: str = "",
    ):
        payload = urlencode(fields).encode("utf-8")
        headers = {
            "Content-Type": "application/x-www-form-urlencoded",
            "Content-Length": str(len(payload)),
        }
        if cookie:
            headers["Cookie"] = cookie
        connection.request("POST", path, body=payload, headers=headers)
        response = connection.getresponse()
        body = response.read().decode("utf-8")
        return response, body

    def test_login_mobile_page_save_and_logout(self) -> None:
        with TemporaryDirectory() as temporary:
            database, inventory = self._fixture(Path(temporary))
            store = CarbonDLCStoreServer(
                Endpoint("127.0.0.1", 0),
                database,
                inventory,
                session_seconds=300,
                cookie_secure="never",
            )
            endpoint = store.start()
            connection = HTTPConnection(endpoint.host, endpoint.port, timeout=3)
            try:
                connection.request("GET", "/health")
                response = connection.getresponse()
                health = json.loads(response.read().decode("utf-8"))
                self.assertEqual(response.status, 200)
                self.assertTrue(health["ok"])
                self.assertEqual(health["groups"], 2)

                connection.request("GET", "/dlc")
                response = connection.getresponse()
                login_page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn('name="viewport"', login_page)
                self.assertIn("@media(max-width:600px)", login_page)
                self.assertIn("same account name and password", login_page)

                response, _ = self._post(
                    connection,
                    "/dlc/login",
                    [("account", "driver"), ("password", "secret")],
                )
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/dlc")
                set_cookie = response.getheader("Set-Cookie") or ""
                self.assertIn("HttpOnly", set_cookie)
                self.assertIn("SameSite=Lax", set_cookie)
                cookie = set_cookie.split(";", 1)[0]

                connection.request("GET", "/dlc", headers={"Cookie": cookie})
                response = connection.getresponse()
                page = response.read().decode("utf-8")
                self.assertEqual(response.status, 200)
                self.assertIn("Account: <strong>Driver</strong>", page)
                self.assertIn("ALL FREE", page)
                self.assertIn('value="base" checked', page)
                match = re.search(r'name="csrf" value="([^"]+)"', page)
                self.assertIsNotNone(match)
                csrf = match.group(1)

                response, _ = self._post(
                    connection,
                    "/dlc/save",
                    [("csrf", csrf), ("group", "bonus")],
                    cookie=cookie,
                )
                self.assertEqual(response.status, 303)
                self.assertEqual(response.getheader("Location"), "/dlc?saved=1")
                assert inventory.assignment_store is not None
                self.assertEqual(
                    inventory.assignment_store.selectors_for_account("Driver"),
                    ("bonus",),
                )

                response, body = self._post(
                    connection,
                    "/dlc/save",
                    [("csrf", "invalid"), ("group", "base")],
                    cookie=cookie,
                )
                self.assertEqual(response.status, 403)
                self.assertIn("request has expired", body)
                self.assertEqual(
                    inventory.assignment_store.selectors_for_account("Driver"),
                    ("bonus",),
                )

                response, _ = self._post(
                    connection,
                    "/dlc/logout",
                    [("csrf", csrf)],
                    cookie=cookie,
                )
                self.assertEqual(response.status, 303)
                self.assertIn("Max-Age=0", response.getheader("Set-Cookie") or "")
            finally:
                connection.close()
                store.stop()


if __name__ == "__main__":
    unittest.main()
