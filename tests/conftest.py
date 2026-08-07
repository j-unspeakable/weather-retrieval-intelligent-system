from collections.abc import Callable
import os

import pytest


os.environ["APP_ENV"] = "test"

from app.models import WeatherDocument


@pytest.fixture
def document_factory() -> Callable[..., WeatherDocument]:
    def make_document(
        document_id: str = "forecast:one",
        source_type: str = "forecast",
        location: str = "Chicago, IL",
    ) -> WeatherDocument:
        return WeatherDocument(
            id=document_id,
            location=location,
            latitude=41.8781,
            longitude=-87.6298,
            source_type=source_type,
            headline="Tonight",
            narrative_text="Clear, with a low around 60.",
            issued_at="2026-08-07T12:00:00+00:00",
            effective_at="2026-08-07T19:00:00-05:00",
            payload={"name": "Tonight"},
        )

    return make_document
