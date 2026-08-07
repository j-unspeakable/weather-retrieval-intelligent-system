"""FastAPI dependencies."""

from collections.abc import Iterator
from typing import Annotated

from fastapi import Depends

from app.services.weather_client import WeatherClient


def get_weather_client() -> Iterator[WeatherClient]:
    client = WeatherClient()
    try:
        yield client
    finally:
        client.close()


WeatherClientDependency = Annotated[WeatherClient, Depends(get_weather_client)]
