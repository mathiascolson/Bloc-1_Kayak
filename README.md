# Projet Kayak — Recommandation de destinations et d'hôtels

## 1. Contexte

Kayak est un moteur de recherche de voyages permettant aux utilisateurs de comparer des destinations, des hôtels et des prix.  
Dans ce projet, l'objectif est de construire un pipeline de données permettant de recommander des destinations françaises à partir de critères météorologiques, puis d'associer à ces destinations des hôtels récupérés sur Booking.

Le projet répond à une problématique métier simple : identifier les destinations les plus favorables pour une période de voyage donnée, puis proposer les meilleurs hôtels disponibles dans ces zones.

## 2. Objectifs du projet

Le pipeline permet de :

- collecter les coordonnées géographiques de 35 destinations françaises ;
- récupérer les prévisions météo pour une période de voyage définie ;
- scraper les hôtels Booking associés à chaque destination ;
- stocker les fichiers bruts et nettoyés dans un bucket S3 ;
- charger les données nettoyées dans une base SQL ;
- construire un dataset final enrichi combinant météo, destinations et hôtels ;
- produire deux visualisations cartographiques :
  - Top 5 des destinations recommandées ;
  - meilleurs hôtels Booking dans les destinations recommandées.

## 3. Périmètre des données

Les données utilisées proviennent de trois sources principales :

| Source | Données collectées |
|---|---|
| Nominatim | Latitude et longitude des villes |
| OpenWeather One Call API 4.0 | Prévisions météo journalières |
| Booking.com | Informations hôtels, prix, notes, coordonnées GPS |

Les dates de voyage sont paramétrées dans le fichier `.env` avec :

```env
TRIP_CHECKIN=YYYY-MM-DD
TRIP_CHECKOUT=YYYY-MM-DD
```

Dans le projet, `TRIP_CHECKIN` est inclus et `TRIP_CHECKOUT` est exclu, comme dans une réservation hôtelière.

## 4. Structure générale du pipeline

Le pipeline suit les étapes suivantes :

1. Chargement de la configuration et des variables d'environnement.
2. Géolocalisation des 35 destinations françaises.
3. Récupération des prévisions météo sur la période de voyage.
4. Scraping Booking des hôtels disponibles pour chaque destination.
5. Export des fichiers vers S3.
6. Relecture des fichiers depuis S3 dans le notebook.
7. Nettoyage des données.
8. Agrégation météo par destination.
9. Calcul d'un score météo.
10. Chargement des tables nettoyées en base SQL.
11. Construction du dataset final enrichi.
12. Export conditionnel du dataset final vers S3.
13. Insertion conditionnelle du dataset final en SQL.
14. Visualisation cartographique des recommandations.

## 5. Fichiers principaux

| Fichier | Rôle |
|---|---|
| `Bloc-2_Project_Kayak_V2.ipynb` | Notebook principal du projet |
| `weather_geocoding_V4.py` | Géolocalisation et récupération météo |
| `scrape_booking_V3.py` | Scraping des hôtels Booking |
| `cities.py` | Liste centralisée des 35 destinations |
| `.env` | Variables d'environnement et secrets locaux |

## 6. Stockage S3

Les fichiers sont stockés dans le bucket S3 selon l'organisation suivante :

```text
raw/
  weather/
    weather_forecast_raw.csv
  booking/
    hotels_raw.csv

processed/
  weather/
    cities_geocoded.csv
  booking/
    hotels_clean.csv
  final/
    kayak_enriched_weather_hotels_<run_id>.csv
```

Le dataset final est historisé avec un nom contenant le `run_id`, afin d'éviter l'écrasement des extractions précédentes.

## 7. Base SQL

Les données nettoyées sont chargées dans une base SQL Neon.

Tables intermédiaires :

- `cities_clean`
- `weather_forecast_clean`
- `hotels_clean`
- `weather_city_scores`

Table finale :

- `kayak_enriched_weather_hotels`

La table finale est historisée par `run_id`.  
Une nouvelle insertion est réalisée uniquement si le `run_id` n'existe pas déjà en base.

## 8. Dataset final enrichi

Le dataset final combine les informations météo agrégées, les coordonnées des villes et les données hôtels.

Une ligne correspond à un hôtel enrichi avec les données météo de sa destination.

Colonnes importantes :

- `run_id`
- `trip_checkin`
- `trip_checkout`
- `weather_extracted_at_run`
- `hotel_extracted_at_run`
- `final_dataset_created_at`
- `city_name`
- `city_latitude`
- `city_longitude`
- `weather_score_10`
- `hotel_name`
- `hotel_score`
- `price_per_night`
- `hotel_latitude`
- `hotel_longitude`

## 9. Scoring météo

Un score météo est calculé pour classer les destinations.

Le score combine quatre critères :

| Critère | Pondération |
|---|---:|
| Pluie | 40 % |
| Température | 30 % |
| Couverture nuageuse | 20 % |
| Vent | 10 % |

Si la probabilité moyenne de pluie est disponible pour toutes les villes, elle est utilisée.  
Sinon, le score pluie est calculé à partir du volume total de pluie prévu sur la période.

Le score météo ne vise pas uniquement les destinations les plus chaudes. Il cherche à identifier les destinations offrant le meilleur compromis entre absence de pluie, température confortable, faible couverture nuageuse et vent modéré.

## 10. Visualisations

Le notebook produit deux cartes interactives avec Plotly :

1. **Top 5 destinations recommandées**  
   Carte des villes ayant obtenu les meilleurs scores météo.

2. **Meilleurs hôtels dans les destinations recommandées**  
   Carte des hôtels récupérés sur Booking dans les 5 destinations recommandées.  
   Le scraping étant limité aux 20 premiers hôtels par ville, la carte peut contenir jusqu'à 100 hôtels.

## 11. Exécution du projet

### 11.1. Préparer le fichier `.env`

Exemple de variables attendues :

```env
OPENWEATHER_API_KEY=your_openweather_api_key
AWS_ACCESS_KEY_ID=your_aws_access_key
AWS_SECRET_ACCESS_KEY=your_aws_secret_key
AWS_DEFAULT_REGION=eu-west-3
AWS_S3_BUCKET_NAME=your_bucket_name
NEON_DATABASE_URL=your_neon_database_url
CONTACT=your_email@example.com
TRIP_CHECKIN=2026-07-28
TRIP_CHECKOUT=2026-08-04
```

### 11.2. Lancer le pipeline

Le notebook lance les scripts externes :

```bash
python weather_geocoding_V4.py
```

et, depuis Windows via WSL si nécessaire :

```bash
python scrape_booking_V3.py
```

Les fichiers générés sont envoyés vers S3, puis relus depuis le notebook pour nettoyage, scoring, chargement SQL et visualisation.

## 12. Contrôles réalisés

Le notebook inclut plusieurs contrôles :

- vérification du nombre de villes ;
- vérification des fichiers relus depuis S3 ;
- contrôle du nombre de lignes par table SQL ;
- contrôle du `run_id` ;
- contrôle de cohérence entre les villes du Top 5 et les hôtels affichés ;
- vérification que le dataset final est bien disponible dans S3 et SQL.

## 13. Conclusion

Le projet met en place un pipeline complet de type Data Engineering :

- collecte de données externes ;
- stockage dans un data lake S3 ;
- nettoyage et transformation ;
- calcul d'indicateurs métier ;
- chargement dans une base SQL ;
- historisation des extractions ;
- production de visualisations interactives.

Les livrables principaux sont disponibles dans S3 et dans la base SQL, avec une traçabilité assurée par les dates de voyage, les dates d'extraction et le `run_id`.
