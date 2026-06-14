"""
scrape_booking.py  –  Playwright pur, sans Scrapy
v3 : extraction des coordonnées GPS depuis la page de chaque hôtel

Stratégie coordonnées (dans l'ordre, sans fallback geopy) :
  1. JSON-LD  <script type="application/ld+json">  → geo.latitude / geo.longitude
  2. Attributs data-*  sur l'élément map ou le conteneur hôtel
  3. Regex sur le HTML brut  (booking injecte parfois les coords en JS inline)
  Si aucune source ne donne de résultat → latitude/longitude restent vides.
"""

import sys, re, csv, time, random, os, logging, json
from datetime import datetime
from urllib.parse import urlencode

if sys.platform == "win32":
    import asyncio
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from playwright.sync_api import sync_playwright
import pandas as pd
import boto3
from dotenv import load_dotenv

load_dotenv()

from cities import CITIES
CITY_ID_MAP = {city: idx for idx, city in enumerate(CITIES, start=1)}

# ── Config ────────────────────────────────────────────────────────────────────
BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
RAW_CSV   = os.path.join(BASE_DIR, "hotels_raw.csv")
CLEAN_CSV = os.path.join(BASE_DIR, "hotels_clean.csv")
LOG_FILE  = os.path.join(BASE_DIR, "scrape.log")
DEBUG_DIR = os.path.join(BASE_DIR, "debug_pages")
os.makedirs(DEBUG_DIR, exist_ok=True)

CHECKIN = os.getenv("TRIP_CHECKIN")
CHECKOUT = os.getenv("TRIP_CHECKOUT")

if not CHECKIN or not CHECKOUT:
    raise ValueError("TRIP_CHECKIN et TRIP_CHECKOUT doivent être définis dans le fichier .env")

MAX_RETRIES = 3

FIELDNAMES = [
    "city_id", "city_name",
    "trip_checkin", "trip_checkout",
    "hotel_name", "url",
    "latitude", "longitude", "score", "score_count",
    "description", "price_per_night", "stars", "extracted_at"
]

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

# ── Helpers ───────────────────────────────────────────────────────────────────
def upload_file_to_s3(local_path: str, s3_key: str) -> None:
    bucket_name = os.getenv("AWS_S3_BUCKET_NAME")

    if not bucket_name:
        raise ValueError("AWS_S3_BUCKET_NAME manquant dans les variables d'environnement.")

    s3 = boto3.client("s3")
    s3.upload_file(local_path, bucket_name, s3_key)

    log.info(f"Upload S3 OK : s3://{bucket_name}/{s3_key}")

def city_id(name):
    return CITY_ID_MAP[name]

def booking_url(city, offset=0):
    params = {
        "ss": city, "lang": "fr", "sb": "1",
        "src_elem": "sb", "src": "searchresults",
        "checkin": CHECKIN, "checkout": CHECKOUT,
        "group_adults": "2", "no_rooms": "1", "group_children": "0",
        "nflt": "ht_id=204",   # 204 = Hôtels uniquement
        "order": "review_score_and_count",  # ← tri par meilleure note
        "offset": offset,                   # ← pagination
    }
    return f"https://www.booking.com/searchresults.fr.html?{urlencode(params)}"

def rand_sleep(a=1.5, b=3.5):
    time.sleep(random.uniform(a, b))

def save_debug(page, label, reason):
    slug = label.replace(" ", "_")
    ts   = datetime.now().strftime("%H%M%S")
    html_path = os.path.join(DEBUG_DIR, f"debug_{slug}_{ts}.html")
    png_path  = os.path.join(DEBUG_DIR, f"debug_{slug}_{ts}.png")
    try:
        with open(html_path, "w", encoding="utf-8") as f:
            f.write(page.content())
        page.screenshot(path=png_path, full_page=False)
        log.warning(f"  → Debug : {html_path}")
    except Exception as e:
        log.warning(f"  → Debug impossible : {e}")

def detect_block(page) -> str | None:
    url   = page.url
    title = page.title()
    if "searchresults" not in url and "hotel" not in url:
        return f"Redirigé → {url[:80]}"
    if "errorc_searchstring_not_found" in url:
        return "errorc_searchstring_not_found"
    if page.query_selector("iframe[src*='captcha']") or \
       page.query_selector("#px-captcha") or \
       page.query_selector("[class*='captcha']"):
        return "CAPTCHA détecté"
    if "challenge" in url or "Just a moment" in title:
        return f"Challenge anti-bot ({title!r})"
    return None

# ── Extraction GPS depuis une page hôtel ─────────────────────────────────────
def extract_coords_from_hotel_page(page) -> tuple[str, str, str]:  # ← str ajouté
    """
    Tente d'extraire latitude/longitude et description depuis la page hôtel.
    Retourne ("", "", "") si rien trouvé.
    Stratégie :
      1. JSON-LD schema.org  (LodgingBusiness > geo)
      2. Attributs data-map-lat / data-map-lng ou similaires
      3. Regex JS inline  (b_map_center_lat, booking_map_lat, etc.)
    """
    html = page.content()
    lat, lng = "", ""  # ← on initialise pour ne plus faire de return anticipé

    # ── 1. JSON-LD ────────────────────────────────────────────────────────────
    for script_el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(script_el.inner_text())
            # Peut être une liste ou un dict
            items = data if isinstance(data, list) else [data]
            for item in items:
                geo = item.get("geo", {})
                la  = geo.get("latitude") or geo.get("lat")
                lo  = geo.get("longitude") or geo.get("long") or geo.get("lng")
                if la and lo:
                    lat, lng = str(la), str(lo)
                    break
        except Exception:
            pass

    # ── 2. Attributs data-* ───────────────────────────────────────────────────
    if not lat:
        for selector in [
            "[data-map-lat]",
            "[data-latitude]",
            "[data-lat]",
            "#hotel_map_canvas",
            "[id*='map']",
        ]:
            el = page.query_selector(selector)
            if el:
                la = (el.get_attribute("data-map-lat") or
                       el.get_attribute("data-latitude") or
                       el.get_attribute("data-lat"))
                lo = (el.get_attribute("data-map-lng") or
                       el.get_attribute("data-map-long") or
                       el.get_attribute("data-longitude") or
                       el.get_attribute("data-lng") or
                       el.get_attribute("data-long"))
                if la and lo:
                    lat, lng = la.strip(), lo.strip()
                    break

    # ── 3. Regex JS inline ────────────────────────────────────────────────────
    if not lat:
        patterns = [
            r'b_map_center_lat["\s:=]+([0-9\-\.]+)',
            r'b_map_center_lon["\s:=]+([0-9\-\.]+)',
            r'"latitude"\s*:\s*([0-9\-\.]+)',
            r'"longitude"\s*:\s*([0-9\-\.]+)',
            r'booking_map_lat["\s:=]+([0-9\-\.]+)',
            r'booking_map_lon["\s:=]+([0-9\-\.]+)',
        ]
        lat_m = re.search(patterns[0], html) or re.search(patterns[2], html) or re.search(patterns[4], html)
        lng_m = re.search(patterns[1], html) or re.search(patterns[3], html) or re.search(patterns[5], html)
        if lat_m and lng_m:
            lat, lng = lat_m.group(1), lng_m.group(1)

    # ── 4. Description complète ───────────────────────────────────────────────
    description = ""

    # Tentative 1 : JSON-LD
    for script_el in page.query_selector_all('script[type="application/ld+json"]'):
        try:
            data = json.loads(script_el.inner_text())
            items = data if isinstance(data, list) else [data]
            for item in items:
                desc = item.get("description", "")
                if desc and len(desc) > 50:
                    description = desc.strip()
                    break
        except Exception:
            pass

    # Tentative 2 : sélecteurs HTML
    if not description:
        for selector in [
            '[data-testid="property-description"]',
            '#property_description_content',
            '.hp_desc_main_content',
            '[class*="description"]',
            '.bh-property-description',
        ]:
            el = page.query_selector(selector)
            if el:
                txt = el.inner_text().strip()
                if len(txt) > 50:
                    description = txt
                    break

    description = re.sub(r'\n{3,}', '\n\n', description).strip()

    return lat, lng, description

# ── Visite d'une page hôtel pour récupérer les coords ────────────────────────
def fetch_hotel_coords(page, hotel_url: str, hotel_name: str) -> tuple[str, str, str]:
    if not hotel_url:
        return "", "", ""
    try:
        page.goto(hotel_url, wait_until="domcontentloaded", timeout=30000)
        rand_sleep(1.5, 3.0)

        # Vérif blocage
        block = detect_block(page)
        if block:
            log.warning(f"    [BLOCAGE hôtel] {hotel_name[:40]} → {block}")
            return "", "", ""

        lat, lng, description = extract_coords_from_hotel_page(page)
        if lat and lng:
            log.info(f"    ✓ GPS {hotel_name[:40]:<40} → {lat}, {lng}")
        else:
            log.info(f"    ✗ GPS non trouvé : {hotel_name[:40]}")
        return lat, lng, description

    except Exception as e:
        log.warning(f"    [ERREUR hôtel] {hotel_name[:40]} → {e}")
        return "", "", ""

# ── Scraping d'une ville ───────────────────────────────────────────────────────
def scrape_city(page, city_name) -> list[dict]:
    url = booking_url(city_name)
    log.info(f"{'─'*50}")
    log.info(f"Ville : {city_name}")

    for attempt in range(1, MAX_RETRIES + 1):
        log.info(f"  Tentative {attempt}/{MAX_RETRIES}")

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=40000)
            rand_sleep(2, 5)
        except Exception as e:
            log.error(f"  [TIMEOUT] {e}")
            if attempt == MAX_RETRIES:
                save_debug(page, city_name, "timeout")
            rand_sleep(5, 10)
            continue

        log.info(f"  URL : {page.url[:100]}")
        log.info(f"  Titre : {page.title()!r}")

        block = detect_block(page)
        if block:
            log.warning(f"  [BLOCAGE] {block}")
            save_debug(page, city_name, "blocage")
            rand_sleep(8, 15)
            continue

        try:
            page.click('[id="onetrust-accept-btn-handler"]', timeout=5000)
            rand_sleep(1, 2)
        except Exception:
            pass

        try:
            page.wait_for_selector('[data-testid="property-card"]', timeout=20000)
        except Exception as e:
            log.warning(f"  [PAS DE CARTES] {e}")
            save_debug(page, city_name, "no_cards")
            rand_sleep(8, 15)
            continue

        for _ in range(4):
            page.evaluate("window.scrollBy(0, window.innerHeight * 0.75)")
            rand_sleep(0.7, 1.4)

        cards = page.query_selector_all('[data-testid="property-card"]')
        log.info(f"  [OK] {len(cards)} cartes")

        extraction_ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        hotels = []
        for card in cards[:20]:
            h = {
                "city_id": city_id(city_name),
                 "city_name": city_name,
                 "trip_checkin": CHECKIN,
                 "trip_checkout": CHECKOUT,
                 "latitude": "",
                 "longitude": "",
                 "extracted_at": extraction_ts
            }

            el = card.query_selector('[data-testid="title"]')
            h["hotel_name"] = el.inner_text().strip() if el else ""

            el = card.query_selector('[data-testid="title-link"]')
            raw = el.get_attribute("href") if el else ""
            h["url"] = raw.split("?")[0] if raw else ""

            # Le score chiffré est dans le texte "Avec une note de 8,0"
            # porté par le div [data-testid="review-score"].
            # On extrait le nombre avec une regex.
            score_raw = ""
            review_block = card.query_selector('[data-testid="review-score"]')
            if review_block:
                txt = review_block.inner_text().strip()
                m = re.search(r"(\d+[,\.]\d+|\d+)", txt)
                if m:
                    score_raw = m.group(1)
            h["score"] = score_raw

            score_count_el = card.query_selector('[data-testid="review-score"] > div > div:last-child')
            h["score_count"] = score_count_el.inner_text().strip() if score_count_el else ""

            el = card.query_selector('[data-testid="price-and-discounted-price"]')
            raw = el.inner_text().strip() if el else ""
            h["price_per_night"] = re.sub(r"[^\d]", "", raw)

            el = card.query_selector('[data-testid="property-card-unit-configuration"]')
            h["description"] = el.inner_text().strip() if el else ""

            # Étoiles : l'aria-label "2 sur 5" est sur le div PARENT de rating-stars.
            # On remonte via :has() ou on cible directement le div[role="button"] englobant.
            stars_val = ""
            el = card.query_selector('[data-testid="rating-stars"]')
            if el:
                # Récupère le parent qui porte l'aria-label "X sur 5"
                parent = el.evaluate_handle("el => el.parentElement")
                if parent:
                    label = parent.get_attribute("aria-label") or ""
                    m = re.search(r"(\d+)\s+sur", label)
                    if m:
                        stars_val = m.group(1)
            h["stars"] = stars_val

            hotels.append(h)

        # ── Visite des pages hôtel pour les coordonnées ───────────────────────
        log.info(f"  Récupération GPS pour {len(hotels)} hôtels...")
        for h in hotels:
            lat, lng, desc = fetch_hotel_coords(page, h["url"], h["hotel_name"])
            h["latitude"]  = lat
            h["longitude"] = lng
            h["description"] = desc
            rand_sleep(1.0, 2.5)

        gps_ok = sum(1 for h in hotels if h["latitude"])
        log.info(f"  GPS récupérés : {gps_ok}/{len(hotels)}")

        # Retour sur la page de résultats (pour la prochaine ville)
        # Pas nécessaire car scrape_city refait un goto à chaque appel
        return hotels

    log.error(f"  [ÉCHEC DÉFINITIF] {city_name}")
    return []

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    log.info("=" * 60)
    log.info(f"Démarrage scraping v3 — {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log.info(f"Villes : {len(CITIES)}  |  Dates : {CHECKIN} → {CHECKOUT}")
    log.info("=" * 60)

    all_hotels = []
    city_stats = {}

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationEnabled",
                "--disable-dev-shm-usage",
                "--disable-gpu",
                "--window-size=1366,768",
            ]
        )
        context = browser.new_context(
            viewport={"width": 1366, "height": 768},
            locale="fr-FR",
            timezone_id="Europe/Paris",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            extra_http_headers={
                "Accept-Language": "fr-FR,fr;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
                "Sec-Fetch-Dest": "document",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Site": "none",
                "Sec-Fetch-User": "?1",
                "DNT": "1",
            }
        )
        context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {get: () => undefined});
            window.chrome = { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
            Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3,4,5] });
            Object.defineProperty(navigator, 'languages', { get: () => ['fr-FR','fr','en-US','en'] });
            const _origQuery = window.navigator.permissions.query;
            window.navigator.permissions.query = (p) =>
                p.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : _origQuery(p);
        """)

        page = context.new_page()

        # Pré-chauffe
        log.info("Pré-chauffe accueil Booking...")
        try:
            page.goto("https://www.booking.com/index.fr.html",
                      wait_until="domcontentloaded", timeout=30000)
            rand_sleep(3, 6)
            try:
                page.click('[id="onetrust-accept-btn-handler"]', timeout=5000)
                rand_sleep(1, 2)
            except Exception:
                pass
            log.info(f"  Accueil OK : {page.title()!r}")
        except Exception as e:
            log.warning(f"  Pré-chauffe échouée : {e}")

        for i, city in enumerate(CITIES):
            hotels = scrape_city(page, city)
            if hotels:
                all_hotels.extend(hotels)
                gps_count = sum(1 for h in hotels if h["latitude"])
                city_stats[city] = f"OK ({len(hotels)} hôtels, {gps_count} GPS)"
            else:
                city_stats[city] = "ÉCHEC"

            if (i + 1) % 5 == 0:
                pause = random.uniform(15, 25)
                log.info(f"Pause longue ({pause:.0f}s)...")
                time.sleep(pause)
            else:
                rand_sleep(4, 8)

        browser.close()

    # Rapport
    log.info("=" * 60)
    log.info("RAPPORT FINAL")
    log.info("=" * 60)
    ok  = sum(1 for v in city_stats.values() if v.startswith("OK"))
    ko  = sum(1 for v in city_stats.values() if v == "ÉCHEC")
    log.info(f"Villes OK : {ok}/{len(CITIES)}  |  Échecs : {ko}")
    for city, status in city_stats.items():
        log.info(f"  {'✓' if status.startswith('OK') else '✗'} {city:<35} {status}")

    total_gps = sum(1 for h in all_hotels if h["latitude"])
    log.info(f"\nTotal hôtels : {len(all_hotels)}  |  Avec GPS : {total_gps}")

    # Export brut
    with open(RAW_CSV, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(all_hotels)
    log.info(f"CSV brut  : {RAW_CSV}")
    upload_file_to_s3(
        RAW_CSV,
        f"raw/booking/hotels_raw.csv"
    )

    # Nettoyage
    if all_hotels:
        df = pd.read_csv(RAW_CSV)
        df = df.dropna(subset=["hotel_name"])
        df = df[df["hotel_name"].str.strip() != ""]
        df["score"] = pd.to_numeric(
            df["score"].astype(str).str.replace(",", "."), errors="coerce"
        )

        df["price_per_night"] = pd.to_numeric(df["price_per_night"], errors="coerce")
        df["latitude"]  = pd.to_numeric(df["latitude"],  errors="coerce")
        df["longitude"] = pd.to_numeric(df["longitude"], errors="coerce")
        df.to_csv(CLEAN_CSV, index=False, encoding="utf-8")
        log.info(f"CSV propre : {CLEAN_CSV}")
        upload_file_to_s3(
            CLEAN_CSV,
            f"processed/booking/hotels_clean.csv"
        )
        print(df[["city_name", "hotel_name", "latitude", "longitude", "score", "price_per_night"]].head(10).to_string())
    else:
        log.error("Aucun hôtel collecté.")

if __name__ == "__main__":
    main()