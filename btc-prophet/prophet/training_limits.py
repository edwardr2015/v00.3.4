"""Training-size limits for paper-only challenger models, configurable without code changes.

The original release used 200 labels (logistic gate), 300 markets (ML challengers), 30 markets per
purged split, 100 examples for the logistic fit and a 12-hour retrain interval. Those limits kept
six of eight model families untrained for the whole of the observed runs. These defaults let the
models start learning early and retrain hourly. Small-sample models are flagged in their metadata
and remain shadow/paper only; promotion rules are unchanged.

Restore the original behaviour with, e.g.:
    PROPHET_MIN_TRAINING_MARKETS=300 PROPHET_MIN_SPLIT_MARKETS=30 \
    PROPHET_MIN_FIT_EXAMPLES=100 PROPHET_RETRAIN_SECONDS=43200
"""
import os

ORIGINAL = dict(learning_gate=200, challenger_gate=300, split=30, fit=100, retrain_seconds=43200)
SMALL_SAMPLE_BELOW = 300


def _int(name, default, low):
    try:
        return max(low, int(os.getenv(name, default)))
    except ValueError:
        return default


def min_training_markets():
    return _int("PROPHET_MIN_TRAINING_MARKETS", 30, 10)


def min_split_markets():
    return _int("PROPHET_MIN_SPLIT_MARKETS", 5, 2)


def min_fit_examples():
    return _int("PROPHET_MIN_FIT_EXAMPLES", 10, 4)


def retrain_seconds():
    return _int("PROPHET_RETRAIN_SECONDS", 3600, 300)


def describe(n):
    return dict(min_training_markets=min_training_markets(), min_split_markets=min_split_markets(),
                min_fit_examples=min_fit_examples(), retrain_seconds=retrain_seconds(),
                original=ORIGINAL, small_sample=n < SMALL_SAMPLE_BELOW, examples=n)
