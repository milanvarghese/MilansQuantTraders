"""Weather forecast client: NOAA for US cities, Open-Meteo for international.
Supports multi-model ensemble (ECMWF + GFS + ICON) for probability estimation.

Research shows multi-model ensembles consistently outperform single-model
because they sample model structural uncertainty (Gneiting et al., 2005).
ECMWF is #1 globally; combining with GFS and ICON gives 120+ members.
"""

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests

from config import NOAA_BASE_URL, NOAA_USER_AGENT, OPENMETEO_URL, CITIES, PROXIES

logger = logging.getLogger(__name__)

ENSEMBLE_API_URL = "https://ensemble-api.open-meteo.com/v1/ensemble"

# Models to fetch, in priority order. ECMWF IFS (51 members) is best,
# GFS (31 members) and ICON (40 members) add diversity.
ENSEMBLE_MODELS = [
    {"name": "ecmwf_ifs025", "param": "ecmwf_ifs025", "members": 51},
    {"name": "gfs025", "param": "gfs_seamless", "members": 31},
    {"name": "icon_global", "param": "icon_seamless", "members": 40},
]


class WeatherClient:
    """Fetches temperature forecasts from NOAA (US) or Open-Meteo (worldwide).
    Also provides multi-model ensemble data for probability estimation."""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": NOAA_USER_AGENT})
        if PROXIES:
            self.session.proxies.update(PROXIES)
        self._grid_cache: dict[str, str] = {}

    # --- NOAA (US cities) ---

    def _get_noaa_forecast_url(self, city: str) -> Optional[str]:
        if city in self._grid_cache:
            return self._grid_cache[city]

        city_info = CITIES.get(city)
        if not city_info:
            return None

        try:
            resp = self.session.get(
                f"{NOAA_BASE_URL}/points/{city_info['lat']},{city_info['lon']}",
                timeout=15,
            )
            resp.raise_for_status()
            url = resp.json()["properties"]["forecastHourly"]
            self._grid_cache[city] = url
            return url
        except Exception as e:
            logger.error(f"Failed to get NOAA grid for {city}: {e}")
            return None

    def _get_noaa_high(self, city: str, target_date: str) -> Optional[float]:
        """Get NOAA forecast high in °C for a US city."""
        url = self._get_noaa_forecast_url(city)
        if not url:
            return None

        for attempt in range(3):
            try:
                resp = self.session.get(url, timeout=15)
                resp.raise_for_status()
                periods = resp.json()["properties"]["periods"]

                max_temp_f = None
                for period in periods:
                    if period["startTime"][:10] == target_date:
                        temp = period["temperature"]
                        if max_temp_f is None or temp > max_temp_f:
                            max_temp_f = temp

                if max_temp_f is not None:
                    max_temp_c = (max_temp_f - 32) * 5 / 9
                    return round(max_temp_c, 1)
                return None
            except Exception as e:
                logger.warning(f"NOAA fetch attempt {attempt+1} failed for {city}: {e}")
                time.sleep(2 ** attempt)

        self._grid_cache.pop(city, None)
        return None

    # --- Open-Meteo (worldwide, single forecast) ---

    def _get_openmeteo_high(self, city: str, target_date: str) -> Optional[float]:
        """Get Open-Meteo forecast high in °C for any city."""
        city_info = CITIES.get(city)
        if not city_info:
            return None

        try:
            resp = self.session.get(
                OPENMETEO_URL,
                params={
                    "latitude": city_info["lat"],
                    "longitude": city_info["lon"],
                    "daily": "temperature_2m_max",
                    "timezone": "auto",
                    "forecast_days": 3,
                },
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            dates = data["daily"]["time"]
            temps = data["daily"]["temperature_2m_max"]

            for i, d in enumerate(dates):
                if d == target_date:
                    return round(temps[i], 1)

            return None
        except Exception as e:
            logger.error(f"Open-Meteo fetch failed for {city}: {e}")
            return None

    # --- Multi-Model Ensemble (ECMWF + GFS + ICON) ---

    def _fetch_single_model_ensemble(
        self, city_info: dict, model_param: str, target_date: str
    ) -> list[float]:
        """Fetch ensemble members for a single model. Returns list of temps."""
        try:
            resp = self.session.get(
                ENSEMBLE_API_URL,
                params={
                    "latitude": city_info["lat"],
                    "longitude": city_info["lon"],
                    "daily": "temperature_2m_max",
                    "models": model_param,
                    "timezone": "auto",
                    "forecast_days": 3,
                },
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()
            daily = data.get("daily", {})
            dates = daily.get("time", [])

            # Find target date index
            date_idx = None
            for i, d in enumerate(dates):
                if d == target_date:
                    date_idx = i
                    break

            if date_idx is None:
                return []

            # Collect all ensemble member values for this date
            members = []

            # Control run
            control = daily.get("temperature_2m_max")
            if control and date_idx < len(control) and control[date_idx] is not None:
                members.append(control[date_idx])

            # Perturbation members (up to 50 for ECMWF, 30 for GFS, 39 for ICON)
            for m in range(1, 51):
                key = f"temperature_2m_max_member{m:02d}"
                vals = daily.get(key)
                if vals and date_idx < len(vals) and vals[date_idx] is not None:
                    members.append(vals[date_idx])

            return members

        except Exception as e:
            logger.warning(f"Ensemble fetch failed for {model_param}: {e}")
            return []

    def get_ensemble_highs(self, city: str, target_date: Optional[str] = None) -> Optional[list[float]]:
        """Get multi-model ensemble high temperature forecasts in °C.

        Fetches ECMWF (51 members) + GFS (31 members) + ICON (40 members)
        and pools them into a single 120+ member super-ensemble.
        Falls back to fewer models if some fail.

        Returns list of temperature values, or None on failure.
        """
        if target_date is None:
            target_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

        city_info = CITIES.get(city)
        if not city_info:
            return None

        all_members = []
        models_used = []

        for model in ENSEMBLE_MODELS:
            members = self._fetch_single_model_ensemble(
                city_info, model["param"], target_date
            )
            if members:
                all_members.extend(members)
                models_used.append(f"{model['name']}({len(members)})")
            time.sleep(0.2)  # Rate limiting

        if len(all_members) < 10:
            logger.warning(f"Only {len(all_members)} total ensemble members for {city}")
            return None

        logger.info(
            f"{city} multi-model ensemble for {target_date}: "
            f"{len(all_members)} members [{', '.join(models_used)}] | "
            f"mean={sum(all_members)/len(all_members):.1f}°C "
            f"spread={max(all_members)-min(all_members):.1f}°C"
        )
        return all_members

    # --- Public API ---

    def get_forecast_high_celsius(self, city: str, target_date: Optional[str] = None) -> Optional[float]:
        """Get the forecasted daily high temperature in °C.

        Uses NOAA for US cities, Open-Meteo for international.
        """
        if target_date is None:
            target_date = (datetime.now(timezone.utc).date() + timedelta(days=1)).isoformat()

        city_info = CITIES.get(city)
        if not city_info:
            logger.error(f"Unknown city: {city}")
            return None

        source = city_info.get("source", "openmeteo")

        if source == "noaa":
            temp = self._get_noaa_high(city, target_date)
        else:
            temp = self._get_openmeteo_high(city, target_date)

        if temp is not None:
            logger.info(f"{city} forecast high for {target_date}: {temp}°C (via {source})")
        else:
            logger.warning(f"No forecast for {city} on {target_date}")

        return temp

    def get_all_city_highs(self, target_date: Optional[str] = None) -> dict[str, float]:
        """Get forecast highs (°C) for all configured cities."""
        results = {}
        for city in CITIES:
            high = self.get_forecast_high_celsius(city, target_date)
            if high is not None:
                results[city] = high
            time.sleep(0.3)
        return results
