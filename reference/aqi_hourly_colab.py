# 1. IMPORTS 
import asyncio, logging, httpx, pandas as pd, pytz
from datetime import datetime, timedelta
from google.colab import auth            # Colab login for BigQuery access
from google.cloud import bigquery as bq

auth.authenticate_user()                 # opens a Google sign-in prompt; no key file needed

# 2. CONFIG
class creds:                             # BigQuery target: project.dataset.table
    PROJECT_ID, DATASET_ID, TABLE_ID = "project-66f56fc9-9de6-4cfd-8cf", "air_quality", "aqi_yesterday_today"

logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
URL = "https://air-quality-api.open-meteo.com/v1/air-quality"   # free API, no key needed
IST = pytz.timezone("Asia/Kolkata")      # all dates/times are handled in Indian time

# Fields requested from the API (these become the table's pollutant columns)
V = ["pm2_5", "pm10", "nitrogen_dioxide", "sulphur_dioxide", "carbon_monoxide", "ozone", "us_aqi"]

# State -> (latitude, longitude). Fixed lookup table of ONE representative point per state/UT.
# The API needs a location to return data for. Pollution values are NOT stored here;
# they are fetched live from the API on every run.
ST = {"Andhra Pradesh": (16.51, 80.52), "Arunachal Pradesh": (27.08, 93.61), "Assam": (26.14, 91.79),
    "Bihar": (25.59, 85.14), "Chhattisgarh": (21.25, 81.63), "Goa": (15.50, 73.83), "Gujarat": (23.22, 72.65),
    "Haryana": (28.46, 77.03), "Himachal Pradesh": (31.10, 77.17), "Jharkhand": (23.34, 85.31),
    "Karnataka": (12.97, 77.59), "Kerala": (8.52, 76.94), "Madhya Pradesh": (23.26, 77.41),
    "Maharashtra": (19.08, 72.88), "Manipur": (24.82, 93.94), "Meghalaya": (25.57, 91.88),
    "Mizoram": (23.73, 92.72), "Nagaland": (25.67, 94.11), "Odisha": (20.30, 85.82), "Punjab": (30.90, 75.86),
    "Rajasthan": (26.91, 75.79), "Sikkim": (27.33, 88.61), "Tamil Nadu": (13.08, 80.27),
    "Telangana": (17.39, 78.49), "Tripura": (23.83, 91.29), "Uttar Pradesh": (26.85, 80.95),
    "Uttarakhand": (30.32, 78.03), "West Bengal": (22.57, 88.36), "Delhi": (28.61, 77.21),
    "Jammu and Kashmir": (34.08, 74.80), "Ladakh": (34.16, 77.58), "Puducherry": (11.93, 79.83),
    "Chandigarh": (30.73, 76.78), "Andaman and Nicobar Islands": (11.62, 92.73),
    "Lakshadweep": (10.57, 72.64), "Dadra and Nagar Haveli and Daman and Diu": (20.40, 72.83)}

# 3. EXTRACT (async API calls)
async def get(c, sem, st, lat, lon, d0, d1):
    """Fetch hourly data for ONE state. Retries up to 5 times on errors."""
    async with sem:                      # semaphore: limits how many requests run at once
        for i in range(5):
            try:
                r = await c.get(URL, params={"latitude": lat, "longitude": lon, "hourly": ",".join(V),
                    "start_date": d0, "end_date": d1, "timezone": "Asia/Kolkata"}, timeout=60)
                r.raise_for_status()     # raises an error on 4xx/5xx (e.g. 503) so we retry
                return st, r.json()      # return state name with its data
            except Exception as e:
                logging.warning(f"{st} try {i+1}: {e}")
                await asyncio.sleep(2 ** i)   # exponential backoff: wait 1s, 2s, 4s, 8s, 16s
    return st, {}                        # all retries failed: return empty so the run continues

async def extract():
    """Call the API for all states in parallel (max 3 at a time)."""
    now = datetime.now(IST)
    d0, d1 = (now - timedelta(days=1)).date().isoformat(), now.date().isoformat()  # yesterday -> today
    sem = asyncio.Semaphore(3)           # max 3 concurrent requests, to be polite to the free API
    async with httpx.AsyncClient() as c:
        # asyncio.gather runs all state requests concurrently and waits for all to finish
        res = await asyncio.gather(*[get(c, sem, s, la, lo, d0, d1) for s, (la, lo) in ST.items()])
    logging.info(f"{sum(1 for _, j in res if j.get('hourly'))}/{len(ST)} states fetched ({d0} to {d1})")
    return res

# 4. TRANSFORM (pandas)
def transform(res):
    # Build one DataFrame per state from the API's hourly arrays, tag it with the state, stack them
    df = pd.concat([pd.DataFrame(j["hourly"]).assign(state=s) for s, j in res if j.get("hourly")])
    df["ts"] = pd.to_datetime(df.pop("time"))           # 'time' string -> datetime column named 'ts'
    now = datetime.now(IST).replace(tzinfo=None)
    # The API also returns forecast hours for the rest of today, so drop future hours.
    # Also drop rows where every pollutant value is empty.
    df = df[df.ts <= now].dropna(subset=V, how="all")
    # RENAME: API field names -> short column names
    df = df.rename(columns={"pm2_5": "pm25", "nitrogen_dioxide": "no2", "sulphur_dioxide": "so2",
        "carbon_monoxide": "co", "ozone": "o3"})
    # NEW COLUMN 1: AQI category, bucketed from the US AQI value
    df["aqi_category"] = pd.cut(df.us_aqi, [-1, 50, 100, 150, 200, 300, 500],
        labels=["Good", "Moderate", "Unhealthy for Sensitive", "Unhealthy", "Very Unhealthy", "Hazardous"]).astype(str)
    # NEW COLUMN 2: when this row was loaded
    df["ingested_at_ist"] = now
    return df.reset_index(drop=True)

# 5. BIGQUERY TABLE SCHEMA 
SC = [bq.SchemaField(n, t) for n, t in [("state", "STRING"), ("ts", "DATETIME"), ("pm25", "FLOAT"),
    ("pm10", "FLOAT"), ("no2", "FLOAT"), ("so2", "FLOAT"), ("co", "FLOAT"), ("o3", "FLOAT"),
    ("us_aqi", "FLOAT"), ("aqi_category", "STRING"), ("ingested_at_ist", "DATETIME")]]

# 6. LOAD (staging -> main -> drop staging)
def load(df):
    c = bq.Client(project=creds.PROJECT_ID)
    c.create_dataset(f"{creds.PROJECT_ID}.{creds.DATASET_ID}", exists_ok=True)   # create dataset if missing
    m = f"{creds.PROJECT_ID}.{creds.DATASET_ID}.{creds.TABLE_ID}"; s = m + "_staging"
    c.create_table(bq.Table(m, schema=SC), exists_ok=True)       # create main table if missing
    # Step 1: load this run's rows into the staging table (WRITE_TRUNCATE = overwrite it each run)
    cfg = bq.LoadJobConfig(schema=SC, write_disposition="WRITE_TRUNCATE")
    c.load_table_from_dataframe(df[[f.name for f in SC]], s, job_config=cfg).result()
    # Step 2: insert into main only the rows not already there.
    # (state, ts) identifies a row, so re-running never creates duplicates.
    c.query(f"""INSERT INTO `{m}` SELECT s.* FROM `{s}` s WHERE NOT EXISTS (
        SELECT 1 FROM `{m}` t WHERE t.state=s.state AND t.ts=s.ts)""").result()
    # Step 3: delete the staging table
    c.delete_table(s, not_found_ok=True)
    logging.info("Main updated, staging deleted.")

# 7. RUN 
async def main():
    res = await extract()                          # E: fetch from API
    if any(j for _, j in res): load(transform(res))   # T + L, only if at least one state returned data

await main()    # Colab supports top-level await; in a .py script use asyncio.run(main())
