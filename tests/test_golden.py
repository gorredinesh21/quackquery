"""Golden SQL tests: 10 (question, expected-rows) pairs against the bundled
seeded demo CSVs, LLM mocked to the rule-based generator. Proves executor +
guard + catalog end-to-end — the model is not under test here.

Expected values are deterministic: data/demo/*.csv is generated with
random.Random(42) (scripts/make_demo_data.py) and committed.
"""
import pytest


@pytest.mark.parametrize(("dataset", "question", "expected", "check"), [
    ("indian_startup_funding", "top 5 sectors by total funding amount",
     [("Fintech", 6222.7), ("Edtech", 5884.7), ("Healthtech", 5762.6),
      ("SaaS", 5566.4), ("Ecommerce", 5132.4)], "rows"),
    ("indian_startup_funding", "which city has the highest total funding",
     [("Bangalore", 10878.7)], "rows"),
    ("indian_startup_funding", "how many startups per round",
     [("Seed", 86), ("Series A", 63), ("Series B", 35), ("Series C", 24),
      ("Pre-Seed", 9)], "rows"),
    ("indian_startup_funding", "average funding amount by city",
     [("Gurugram", 299.62)], "first"),
    ("ecommerce_orders", "how many orders per payment method",
     [("COD", 95), ("UPI", 91), ("Net Banking", 89), ("Debit Card", 79),
      ("Credit Card", 66)], "rows"),
    ("ecommerce_orders", "average revenue by category",
     [("Books", 13404.73), ("Fashion", 12789.86)], "first2"),
    ("ecommerce_orders", "top 3 categories by total revenue",
     [("Fashion", 1099928.05), ("Grocery", 911407.55),
      ("Electronics", 774276.05)], "rows"),
    ("ecommerce_orders", "total revenue",
     [(5011437.25,)], "single"),
    ("ipl_matches", "which team won the most matches",
     [("Delhi Capitals", 30)], "rows"),
    ("ipl_matches", "how many matches per season",
     [(2024, 74), (2023, 74), (2022, 60)], "rows"),
])
def test_golden(client, demos, dataset, question, expected, check):
    r = client.post("/api/query", json={
        "dataset_id": demos[dataset], "question": question})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["attempts"] == 1
    if check == "rows":
        got = [(row[0], round(float(row[1]), 2)) for row in body["rows"]]
        assert got == expected
    elif check == "single":
        assert len(body["rows"]) == 1
        assert round(float(body["rows"][0][0]), 2) == expected[0][0]
    elif check == "first":
        row = body["rows"][0]
        assert row[0] == expected[0][0]
        assert row[1] == pytest.approx(expected[0][1], abs=0.01)
    elif check == "first2":
        got = [(row[0], round(float(row[1]), 2)) for row in body["rows"][:2]]
        assert got == expected


def test_golden_concurrent_no_cross_dataset_bleed(client, demos):
    """20 interleaved pipeline runs across 3 datasets: the cursor-local
    `data` TEMP view must never leak across datasets."""
    from concurrent.futures import ThreadPoolExecutor

    from app import pipeline

    jobs = []
    plan = [
        (demos["ecommerce_orders"], "how many orders per payment method",
         {"COD", "UPI", "Net Banking", "Debit Card", "Credit Card"}),
        (demos["indian_startup_funding"], "how many startups per round",
         {"Seed", "Series A", "Series B", "Series C", "Pre-Seed"}),
        (demos["ipl_matches"], "how many matches per season", {2024, 2023, 2022}),
    ]
    for i in range(20):
        jobs.append(plan[i % 3] + (f"q{i}",))

    def run(job):
        ds_id, question, dims, salt = job
        res = pipeline.run_pipeline(ds_id, question + " " + salt, lambda e: None)
        return {row[0] for row in res["rows"]}, dims

    with ThreadPoolExecutor(max_workers=8) as ex:
        for got, want in ex.map(run, jobs):
            assert got == want, f"cross-dataset bleed: {got} != {want}"
