#!/usr/bin/env python3
"""Generate the 3 bundled demo CSVs (deterministic, seed=42).

~250-420 rows each — enough for real GROUP BYs, small enough for git.
Run:  python3 scripts/make_demo_data.py
"""
import csv
import os
import random
from datetime import date, timedelta

R = random.Random(42)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(HERE, "data", "demo")
os.makedirs(OUT, exist_ok=True)


def write(name, header, rows):
    path = os.path.join(OUT, name)
    with open(path, "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"{name}: {len(rows)} rows")


# ---------------------------------------------------------------- startups
CITIES = [("Bangalore", 32), ("Mumbai", 20), ("Delhi NCR", 15), ("Hyderabad", 10),
          ("Pune", 8), ("Chennai", 7), ("Gurugram", 8)]
SECTORS = [("Fintech", 22), ("SaaS", 18), ("Edtech", 15), ("Ecommerce", 14),
           ("Healthtech", 12), ("AI/ML", 11), ("Logistics", 8)]
ROUNDS = [("Seed", 40), ("Series A", 28), ("Series B", 18), ("Series C", 9), ("Pre-Seed", 5)]
INVESTORS = ["Sequoia India", "Accel", "Blume Ventures", "Elevation Capital",
             "Peak XV", "Matrix Partners", "Kalaari Capital", "Titan Capital"]

def weighted(pairs):
    items = [p for p, w in pairs for _ in range(w)]
    return R.choice(items)

rows = []
seen = set()
for i in range(320):
    name = f"{weighted([('Zeta',3),('Nimbus',3),('Vertexa',2),('Kraft',2),('Chai',2),('Metro',2),('Fin',2),('Data',2),('Shop',2),('Pay',2)])}{R.choice(['labs','works','ai','fyi','kart','desk','flow','grid'])}{R.randint(1,99) if R.random()<0.5 else ''}"
    if name in seen:
        continue
    seen.add(name)
    base = {"Seed": (4, 25), "Pre-Seed": (1, 6), "Series A": (30, 120),
            "Series B": (120, 400), "Series C": (400, 1200)}
    rnd = weighted(ROUNDS)
    lo, hi = base[rnd]
    rows.append([name, weighted(CITIES), weighted(SECTORS), rnd,
                 round(R.uniform(lo, hi), 1),
                 (date(2023, 1, 1) + timedelta(days=R.randint(0, 729))).isoformat(),
                 R.choice(INVESTORS)])
write("indian_startup_funding.csv",
      ["startup_name", "city", "sector", "round", "amount_cr_inr",
       "announced_date", "lead_investor"], rows)

# ---------------------------------------------------------------- orders
CATS = [("Electronics", 22), ("Fashion", 20), ("Grocery", 16), ("Home", 14),
        ("Beauty", 12), ("Books", 10), ("Toys", 6)]
PAYS = ["UPI", "Credit Card", "Debit Card", "COD", "Net Banking"]
STATUS = [("Delivered", 78), ("Returned", 10), ("Cancelled", 8), ("Shipped", 4)]
rows = []
for i in range(1, 421):
    units = R.randint(1, 5)
    price = round(R.uniform(150, 9000), 0)
    disc = R.choice([0, 0, 5, 10, 15, 20, 30])
    revenue = round(units * price * (1 - disc / 100), 2)
    rows.append([f"ORD-{i:04d}",
                 (date(2024, 1, 1) + timedelta(days=R.randint(0, 364))).isoformat(),
                 weighted(CITIES), weighted(CATS), units, price, disc,
                 R.choice(PAYS), weighted(STATUS), revenue])
write("ecommerce_orders.csv",
      ["order_id", "order_date", "customer_city", "category", "units",
       "unit_price_inr", "discount_pct", "payment_method", "status",
       "revenue_inr"], rows)

# ---------------------------------------------------------------- IPL
TEAMS = ["Mumbai Indians", "Chennai Super Kings", "Royal Challengers Bengaluru",
         "Kolkata Knight Riders", "Delhi Capitals", "Rajasthan Royals",
         "Sunrisers Hyderabad", "Punjab Kings", "Gujarat Titans", "Lucknow Super Giants"]
VENUES = [("Wankhede Stadium", "Mumbai"), ("M. Chinnaswamy Stadium", "Bengaluru"),
          ("Eden Gardens", "Kolkata"), ("Arun Jaitley Stadium", "Delhi"),
          ("MA Chidambaram Stadium", "Chennai"), ("Narendra Modi Stadium", "Ahmedabad"),
          ("Rajiv Gandhi Intl. Stadium", "Hyderabad")]
POTS = ["V Kohli", "RG Sharma", "MS Dhoni", "SA Yadav", "RR Pant", "KL Rahul",
        "DA Warner", "SV Samson", "HH Pandya", "Mohammed Siraj", "AR Patel",
        "JC Buttler", "DP Conway", "RD Gaikwad"]

rows = []
mid = 1
for season in range(2022, 2025):
    n = 74 if season > 2022 else 60
    for _ in range(n):
        t1, t2 = R.sample(TEAMS, 2)
        winner = R.choice([t1, t2])
        venue, city = R.choice(VENUES)
        chased = R.random() < 0.55
        if chased:
            margin = f"{R.randint(4, 10)} wickets"
        else:
            margin = f"{R.randint(3, 85)} runs"
        toss = R.choice([t1, t2])
        rows.append([mid, season,
                     (date(season, 3, 22) + timedelta(days=R.randint(0, 55))).isoformat(),
                     t1, t2, venue, city, toss,
                     R.choice(["bat", "field"]), winner, margin,
                     R.choice(POTS)])
        mid += 1
write("ipl_matches.csv",
      ["match_id", "season", "date", "team1", "team2", "venue", "city",
       "toss_winner", "toss_decision", "winner", "win_margin",
       "player_of_match"], rows)

print("done ->", OUT)
