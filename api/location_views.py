"""Country / State / City lookups for the website signup form.

Data comes from ``api/data/locations.json.gz`` — a compact snapshot of the
dr5hn countries-states-cities-database (ODbL-1.0). It is bundled so the
signup dropdowns never depend on a third-party API at runtime. Layout:

    [[iso2, name, phone_code, flag, [[state_code, state_name, [city, ...]], ...]], ...]

``country`` and ``state`` query params accept either the code (``IN`` /
``MH``) or the name (``India`` / ``Maharashtra``), case-insensitive, because
signup stores names while the dropdowns work with codes.
"""

from __future__ import annotations

import gzip
import json
from functools import lru_cache
from pathlib import Path

from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import AllowAny
from rest_framework.response import Response

from .schema import CityListDoc, CountryListDoc, DetailDoc, StateListDoc

_DATA_FILE = Path(__file__).resolve().parent / "data" / "locations.json.gz"
_CACHE_SECONDS = 60 * 60 * 24

_COUNTRY_PARAM = OpenApiParameter(
    "country", str, required=True, description="Country code or name, e.g. US / United States"
)
_STATE_PARAM = OpenApiParameter(
    "state", str, required=True, description="State code or name, e.g. CA / California"
)


@lru_cache(maxsize=1)
def _countries() -> list:
    with gzip.open(_DATA_FILE, "rt", encoding="utf-8") as fh:
        return json.load(fh)


def _find(rows: list, value: str):
    """Match a [code, name, ...] row by code first, then by name."""
    needle = (value or "").strip().casefold()
    if not needle:
        return None
    for row in rows:
        if row[0].casefold() == needle:
            return row
    for row in rows:
        if row[1].casefold() == needle:
            return row
    return None


def _country_json(row) -> dict:
    return {"code": row[0], "name": row[1], "phone_code": row[2], "flag": row[3]}


def _state_json(row) -> dict:
    return {"code": row[0], "name": row[1]}


def _cached(data: dict) -> Response:
    resp = Response(data)
    resp["Cache-Control"] = f"public, max-age={_CACHE_SECONDS}"
    return resp


def _error(message: str, code: int) -> Response:
    return Response({"detail": message}, status=code)


def _resolve_country(request):
    value = request.query_params.get("country", "")
    if not value.strip():
        return None, _error("Query param 'country' is required.", status.HTTP_400_BAD_REQUEST)
    country = _find(_countries(), value)
    if country is None:
        return None, _error(f"Country '{value}' not found.", status.HTTP_404_NOT_FOUND)
    return country, None


@extend_schema(summary="All countries", responses=CountryListDoc)
@api_view(["GET"])
@permission_classes([AllowAny])
def countries(request):
    """GET /api/locations/countries/ — all countries, sorted by name."""
    results = [_country_json(c) for c in _countries()]
    return _cached({"count": len(results), "results": results})


@extend_schema(
    summary="States of a country",
    parameters=[_COUNTRY_PARAM],
    responses={200: StateListDoc, 400: DetailDoc, 404: DetailDoc},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def states(request):
    """GET /api/locations/states/?country=IN — states of one country."""
    country, err = _resolve_country(request)
    if err:
        return err
    results = [_state_json(s) for s in country[4]]
    return _cached(
        {"country": _country_json(country), "count": len(results), "results": results}
    )


@extend_schema(
    summary="Cities of a state",
    parameters=[_COUNTRY_PARAM, _STATE_PARAM],
    responses={200: CityListDoc, 400: DetailDoc, 404: DetailDoc},
)
@api_view(["GET"])
@permission_classes([AllowAny])
def cities(request):
    """GET /api/locations/cities/?country=IN&state=MH — cities of one state."""
    country, err = _resolve_country(request)
    if err:
        return err
    value = request.query_params.get("state", "")
    if not value.strip():
        return _error("Query param 'state' is required.", status.HTTP_400_BAD_REQUEST)
    state = _find(country[4], value)
    if state is None:
        return _error(
            f"State '{value}' not found in {country[1]}.", status.HTTP_404_NOT_FOUND
        )
    results = [{"name": name} for name in state[2]]
    return _cached(
        {
            "country": _country_json(country),
            "state": _state_json(state),
            "count": len(results),
            "results": results,
        }
    )
