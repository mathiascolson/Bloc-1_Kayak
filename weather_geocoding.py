import os
import time
import requests
import pandas as pd
import boto3

from datetime import datetime, date, time as dt_time, timezone
from dotenv import load_dotenv

from cities import CITIES


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

GEOLOC_CSV = os.path.join(BASE_DIR, "cities_geocoded.csv")
WEATHER_CSV = os.path.join(BASE_DIR, "weather_forecast_raw.csv")

load_dotenv(override=True)

TRIP_CHECKIN = os.getenv("TRIP_CHECKIN")
TRIP_CHECKOUT = os.getenv("TRIP_CHECKOUT")

if not TRIP_CHECKIN or not TRIP_CHECKOUT:
    raise ValueError("TRIP_CHECKIN et TRIP_CHECKOUT doivent être définis dans le fichier .env")


def parse_trip_dates() -> tuple[date, date, int]:
    """
    Interprétation des dates :
      - TRIP_CHECKIN est inclus.
      - TRIP_CHECKOUT est exclu, comme pour une réservation hôtelière.
    Exemple :
      TRIP_CHECKIN=2026-09-01
      TRIP_CHECKOUT=2026-09-08
      => 7 jours analysés : 2026-09-01 à 2026-09-07.
    """
    try:
        checkin = datetime.strptime(TRIP_CHECKIN, "%Y-%m-%d").date()
        checkout = datetime.strptime(TRIP_CHECKOUT, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError("TRIP_CHECKIN et TRIP_CHECKOUT doivent être au format AAAA-MM-JJ.")

    if checkout <= checkin:
        raise ValueError("TRIP_CHECKOUT doit être strictement postérieur à TRIP_CHECKIN.")

    nb_days = (checkout - checkin).days

    if nb_days > 10:
        raise ValueError(
            "La plage météo demandée dépasse 10 jours. "
            "Pour ce projet, utiliser une plage de 7 jours : TRIP_CHECKOUT = TRIP_CHECKIN + 7."
        )

    return checkin, checkout, nb_days


def upload_file_to_s3(local_path: str, s3_key: str) -> None:
    bucket_name = os.getenv("AWS_S3_BUCKET_NAME")

    if not bucket_name:
        raise ValueError("AWS_S3_BUCKET_NAME manquant dans les variables d'environnement.")

    s3 = boto3.client("s3")
    s3.upload_file(local_path, bucket_name, s3_key)

    print(f"Upload S3 OK : s3://{bucket_name}/{s3_key}")


def geoloc_villes(cities: list[str], extracted_at: str) -> list[dict]:
    url = "https://nominatim.openstreetmap.org/search"

    contact = os.getenv("CONTACT", "your_email@example.com")
    headers = {
        "User-Agent": f"KayakProject/1.0 ({contact})"
    }

    geoloc = []

    for idx, city in enumerate(cities, start=1):
        params = {
            "q": city,
            "format": "json",
            "limit": 1,
            "countrycodes": "fr",
        }

        response = requests.get(url, params=params, headers=headers, timeout=20)

        if response.status_code != 200:
            print(f"Echec géolocalisation pour {city} | status={response.status_code}")
            continue

        data = response.json()

        if not data:
            print(f"Aucun résultat géolocalisation pour {city}")
            continue

        result = data[0]

        geoloc.append({
            "city_id": idx,
            "city_name": city,
            "latitude": float(result.get("lat")),
            "longitude": float(result.get("lon")),
            "extracted_at": extracted_at,
        })

        print(f"Géolocalisation OK : {city}")
        time.sleep(1.1)

    return geoloc


def unix_timestamp_utc(day: date) -> int:
    """
    One Call 4.0 timeline utilise un paramètre start en timestamp Unix.
    On part de minuit UTC pour couvrir proprement la journée demandée.
    """
    return int(datetime.combine(day, dt_time.min, tzinfo=timezone.utc).timestamp())


def get_nested(data: dict, keys: list[str]):
    current = data

    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)

    return current


def extract_temperature(day: dict) -> tuple[float | None, float | None, float | None]:
    """
    Normalise les formats possibles de température selon la structure retournée.
    """
    temp = day.get("temp") or day.get("temperature")

    if isinstance(temp, dict):
        temperature = (
            temp.get("day")
            or temp.get("mean")
            or temp.get("avg")
            or temp.get("afternoon")
        )
        temp_min = temp.get("min")
        temp_max = temp.get("max")
        return temperature, temp_min, temp_max

    temperature = temp
    temp_min = day.get("temp_min") or get_nested(day, ["temperature", "min"])
    temp_max = day.get("temp_max") or get_nested(day, ["temperature", "max"])

    return temperature, temp_min, temp_max


def extract_weather_fields(day: dict) -> dict:
    weather_items = day.get("weather") or []

    if isinstance(weather_items, list) and weather_items:
        weather_main = weather_items[0].get("main")
        weather_description = weather_items[0].get("description")
        weather_icon = weather_items[0].get("icon")
    elif isinstance(weather_items, dict):
        weather_main = weather_items.get("main")
        weather_description = weather_items.get("description")
        weather_icon = weather_items.get("icon")
    else:
        weather_main = None
        weather_description = None
        weather_icon = None

    return {
        "weather_main": weather_main,
        "weather_description": weather_description,
        "weather_icon": weather_icon,
    }


def climat_villes(geoloc: list[dict], extracted_at: str) -> list[dict]:
    """
    Récupère la météo quotidienne via OpenWeather One Call API 4.0.
    """
    checkin_date, checkout_date, nb_days = parse_trip_dates()

    url = "https://api.openweathermap.org/data/4.0/onecall/timeline/1day"

    api_key = os.getenv("OPENWEATHER_API_KEY")
    if not api_key:
        raise ValueError("OPENWEATHER_API_KEY manquante dans les variables d'environnement.")

    climat = []

    for row in geoloc:
        params = {
            "lat": row["latitude"],
            "lon": row["longitude"],
            "start": unix_timestamp_utc(checkin_date),
            "cnt": nb_days,
            "units": "metric",
            "lang": "fr",
            "appid": api_key,
        }

        response = requests.get(url, params=params, timeout=30)

        if response.status_code != 200:
            print(
                f"Echec météo One Call 4.0 pour {row['city_name']} "
                f"| status={response.status_code} | response={response.text[:300]}"
            )
            continue

        data = response.json()
        daily_rows = data.get("data", [])

        if not daily_rows:
            print(f"Aucune donnée météo retournée pour {row['city_name']}")
            continue

        for idx, day in enumerate(daily_rows, start=1):
            dt_value = day.get("dt")
            if dt_value is None:
                continue

            forecast_date = datetime.fromtimestamp(dt_value, tz=timezone.utc).date()

            if forecast_date < checkin_date or forecast_date >= checkout_date:
                continue

            temperature, temp_min, temp_max = extract_temperature(day)
            weather_fields = extract_weather_fields(day)

            climat.append({
                "city_id": row["city_id"],
                "city_name": row["city_name"],
                "latitude": row["latitude"],
                "longitude": row["longitude"],

                "trip_checkin": TRIP_CHECKIN,
                "trip_checkout": TRIP_CHECKOUT,

                "forecast_id": idx,
                "forecast_date": forecast_date.strftime("%Y-%m-%d"),
                "forecast_datetime": datetime.fromtimestamp(dt_value, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),

                "temperature": temperature,
                "temp_min": temp_min,
                "temp_max": temp_max,
                "humidity": day.get("humidity"),
                "clouds": day.get("clouds"),
                "rain_probability": day.get("pop"),
                "rain_volume": day.get("rain"),
                "snow_volume": day.get("snow"),
                "wind_speed": day.get("wind_speed") or day.get("windSpeed"),
                "wind_deg": day.get("wind_deg") or day.get("windDeg"),
                "uvi": day.get("uvi"),

                "weather_main": weather_fields["weather_main"],
                "weather_description": weather_fields["weather_description"],
                "weather_icon": weather_fields["weather_icon"],

                "api_source": "openweather_one_call_4_0_timeline_1day",
                "extracted_at": extracted_at,
            })

        print(f"Météo One Call 4.0 OK : {row['city_name']}")
        time.sleep(1.1)

    return climat


def main():
    extracted_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    checkin_date, checkout_date, nb_days = parse_trip_dates()
    print(f"Plage météo demandée : {checkin_date} -> {checkout_date} ({nb_days} jours, checkout exclu)")

    geoloc = geoloc_villes(CITIES, extracted_at)
    geoloc_df = pd.DataFrame(geoloc)
    geoloc_df.to_csv(GEOLOC_CSV, index=False, encoding="utf-8")
    print(f"Export géolocalisation : {GEOLOC_CSV}")
    upload_file_to_s3(
        GEOLOC_CSV,
        f"processed/weather/cities_geocoded.csv"
    )

    weather = climat_villes(geoloc, extracted_at)
    weather_df = pd.DataFrame(weather)
    weather_df.to_csv(WEATHER_CSV, index=False, encoding="utf-8")
    print(f"Export météo : {WEATHER_CSV}")
    upload_file_to_s3(
        WEATHER_CSV,
        f"raw/weather/weather_forecast_raw.csv"
    )


if __name__ == "__main__":
    main()
