from __future__ import annotations

from typing import Any

import pytest
import requests


class FakeResponse:
    def __init__(self, payload: Any, status_code: int = 200):
        self.payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakeSession:
    def __init__(self, *, get_responses=None, post_responses=None):
        self.get_responses = list(get_responses or [])
        self.post_responses = list(post_responses or [])
        self.get_calls = []
        self.post_calls = []

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        return self.get_responses.pop(0)

    def post(self, url, **kwargs):
        self.post_calls.append((url, kwargs))
        return self.post_responses.pop(0)


@pytest.fixture
def geocoding_result():
    return {
        "results": [
            {
                "id": 2643743,
                "name": "London",
                "latitude": 51.5085,
                "longitude": -0.1257,
                "admin1": "England",
                "country": "United Kingdom",
                "country_code": "GB",
                "timezone": "Europe/London",
            },
            {
                "name": "London",
                "latitude": 42.9834,
                "longitude": -81.233,
                "admin1": "Ontario",
                "country": "Canada",
                "country_code": "CA",
                "timezone": "America/Toronto",
            },
        ]
    }
