"""Compare capture-time signals with final outcomes and summarize repeated skips."""

SIGNALS = ("momentum", "market_odds", "reference_trend")


def bias_sql():
    return """WITH observed AS (
        SELECT a.slug,a.checkpoint,a.time,a.prediction_up AS momentum_up,a.direction,a.gap_bps,
               json_extract(b.payload,'$.bids[0].price') AS bid,
               json_extract(b.payload,'$.asks[0].price') AS ask,
               v.outcome
        FROM agreement_research a
        JOIN timing_checkpoint_books b USING(slug,checkpoint)
        LEFT JOIN verified_markets v ON v.slug=a.slug
        WHERE a.scenario='control' AND a.prediction_up IS NOT NULL AND a.gap_bps IS NOT NULL
    ), probabilities AS (
        SELECT *, (bid+ask)/2 AS odds_up,
               CASE WHEN momentum_up>.5 THEN 'UP' WHEN momentum_up<.5 THEN 'DOWN' END AS momentum,
               CASE WHEN bid+ask>1 THEN 'UP' WHEN bid+ask<1 THEN 'DOWN' END AS market_odds,
               CASE WHEN gap_bps>0 THEN 'UP' WHEN gap_bps<0 THEN 'DOWN' END AS reference_trend
        FROM observed WHERE bid>=0 AND ask<=1 AND bid<ask
    )
    SELECT *, (momentum=outcome) AS momentum_correct,
           (market_odds=outcome) AS market_odds_correct,
           (reference_trend=outcome) AS reference_trend_correct
    FROM probabilities"""


def bias_summary(db):
    rows = []
    for signal in SIGNALS:
        # Only score matched settled observations so each direction has the same denominator.
        for row in db.execute(f"""SELECT checkpoint,count(*) AS rounds,
                    sum({signal}_correct) AS correct,
                    sum({signal}=momentum) AS agreement_with_model
                FROM ({bias_sql()})
                WHERE outcome IS NOT NULL AND momentum IS NOT NULL
                  AND market_odds IS NOT NULL AND reference_trend IS NOT NULL
                GROUP BY checkpoint ORDER BY checkpoint"""):
            rows.append(dict(signal=signal, checkpoint=row["checkpoint"], rounds=row["rounds"],
                             correct=row["correct"], win_rate=row["correct"]/row["rounds"],
                             model_agreement=row["agreement_with_model"]))
    return rows


def skip_diagnostics(db):
    adaptive = [dict(row) for row in db.execute("""WITH recent AS (
            SELECT slug FROM adaptive_windows WHERE finalized IS NOT NULL ORDER BY end DESC LIMIT 20
        )
        SELECT account,reason,count(*) AS rounds
        FROM v2_skips JOIN recent USING(slug) WHERE checkpoint=0
        GROUP BY account,reason ORDER BY account,rounds DESC,reason""")]
    fixed = [dict(row) for row in db.execute("""WITH recent AS (
            SELECT slug FROM v2_decisions GROUP BY slug ORDER BY max(time) DESC LIMIT 20
        )
        SELECT c.family AS model,k.reason,count(*) AS attempts
        FROM lab_skips k JOIN recent USING(slug) JOIN lab_configs c ON c.id=k.config
        WHERE c.id LIKE 'timing/v2/%'
        GROUP BY c.family,k.reason ORDER BY c.family,attempts DESC,k.reason""")]
    completed = db.execute("SELECT min(count(*),20) FROM adaptive_windows WHERE finalized IS NOT NULL").fetchone()[0]
    return dict(adaptive=adaptive,fixed=fixed,adaptive_rounds=completed)
