"""File-backed keyring backend for the stdio end-to-end test.

Selected in the server subprocess with
``PYTHON_KEYRING_BACKEND=e2e_keyring.FileKeyring`` so no operating-system
keychain is ever touched. Secrets persist to the JSON file named by
``E2E_KEYRING_FILE``, which lets the test prove the GitHub credential survives a
server restart. Test use only: it stores secrets in plain text on purpose.
"""

import json
import os

from keyring.backend import KeyringBackend
from keyring.errors import PasswordDeleteError


class FileKeyring(KeyringBackend):
    priority = 10

    @staticmethod
    def _path() -> str:
        return os.environ["E2E_KEYRING_FILE"]

    def _load(self) -> dict:
        try:
            with open(self._path(), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            return {}

    def _save(self, data: dict) -> None:
        tmp = self._path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(data, handle)
        os.replace(tmp, self._path())

    def get_password(self, service, username):
        return self._load().get(f"{service}\n{username}")

    def set_password(self, service, username, password):
        data = self._load()
        data[f"{service}\n{username}"] = password
        self._save(data)

    def delete_password(self, service, username):
        data = self._load()
        if f"{service}\n{username}" not in data:
            raise PasswordDeleteError("not found")
        del data[f"{service}\n{username}"]
        self._save(data)
